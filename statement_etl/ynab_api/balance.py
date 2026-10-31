"""
ph-statement-import - check a statement's balances against the YNAB account.

Reusable for every e-wallet / bank: it only needs the statement's balance (a
Decimal, e.g. from parsers.gcash.statement_balances()) and the YNAB plan /
account. Nothing here is GCash-specific.

How it works:
    1. Read      - get the account's current balance from YNAB    (get_account_balance)
    2. New?      - a newly created account has only one
                   transaction: payee "Starting Balance",
                   category "Inflow: Ready to Assign"              (find_new_account_starting_balance)
    3. Compare   - before the upload, the YNAB balance should
                   equal the statement's starting balance          (check_starting_balance)
    4. Set       - on a new account, set that Starting Balance
                   transaction to the statement's starting
                   balance, just before the upload                 (set_new_account_starting_balance)
    5. Message   - text for a match, a mismatch or a new account   (balance_check_message,
                                                                    new_account_message)

A mismatch is only a warning: the upload can still go ahead. It usually means
YNAB already has transactions after the statement period, or is missing some
from before it.

The check after the upload (YNAB balance vs. the statement's ending balance)
is ynab_api.client.check_ending_balance(). It returns the same result shape, so
balance_check_message() works for both.

Used by ph-statement-import: pipeline.py (command line) and app.py
(web page).

Requirements:
    pip install ynab
"""

from datetime import date
from decimal import Decimal

import ynab
from statement_etl.ynab_api.client import ynab_client

# What YNAB puts on the one transaction of a newly created account
STARTING_BALANCE_PAYEE = "Starting Balance"
STARTING_BALANCE_CATEGORY = "Inflow: Ready to Assign"

# get_transactions_by_account() only returns the last year unless given a
# since_date; this reaches back far enough to see an account's whole history
ALL_HISTORY = date(1970, 1, 1)


# =============================================================================
# READ - the account balance and transactions in YNAB
# =============================================================================


def get_account_balance(plan_id, account_id, access_token=None):
    """AccountsApi.get_account_by_id(): the account's current balance in YNAB.

    Includes cleared and uncleared transactions, approved or not (the same
    "Working Balance" YNAB shows). Returned in currency units as a Decimal:
    5924560 milliunits -> Decimal("5924.56").

    access_token is the web app's token; None uses $BEARER_TOKEN.
    Raises ynab.ApiException (or a subclass) if YNAB rejects the request.
    """
    with ynab_client(access_token) as api_client:
        account = (
            ynab.AccountsApi(api_client)
            .get_account_by_id(plan_id, account_id)
            .data.account
        )
    return Decimal(account.balance) / 1000


def _account_transactions(api_client, plan_id, account_id, since_date=None):
    """TransactionsApi.get_transactions_by_account(), without deleted ones."""
    transactions = (
        ynab.TransactionsApi(api_client)
        .get_transactions_by_account(plan_id, account_id, since_date=since_date)
        .data.transactions
    )
    return [t for t in transactions if not t.deleted]


def _is_starting_balance(transaction):
    return (
        transaction.payee_name == STARTING_BALANCE_PAYEE
        and transaction.category_name == STARTING_BALANCE_CATEGORY
    )


def find_new_account_starting_balance(plan_id, account_id, access_token=None):
    """Is the account newly created? Returns its Starting Balance transaction if
    so, None otherwise.

    New means the account has exactly one transaction, with payee
    "Starting Balance" and category "Inflow: Ready to Assign".
    """
    with ynab_client(access_token) as api_client:
        # Quick look at the last year first: most accounts are ruled out here
        recent = _account_transactions(api_client, plan_id, account_id)
        if len(recent) != 1 or not _is_starting_balance(recent[0]):
            return None
        # Make sure there's nothing older than a year either
        everything = _account_transactions(api_client, plan_id, account_id, ALL_HISTORY)
    return everything[0] if len(everything) == 1 else None


# =============================================================================
# COMPARE - statement balance vs. YNAB balance (before the upload)
# =============================================================================


def compare_balance(
    kind,
    statement_balance,
    ynab_before,
    ynab_after,
    new_account=None,
    already_in_ynab=0,
    to_upload=None,
):
    """Result of one balance check, used by both checks.

    Returns {"kind", "statement", "ynab_before", "ynab_after", "difference",
    "matches", "new_account"}:
      kind         "starting" or "ending"
      ynab_before  YNAB balance before the upload (None if not known)
      ynab_after   YNAB balance after the upload (for the starting check:
                   what it will be, ynab_before + the statement's transactions)
      difference   YNAB - statement: ynab_before for "starting", ynab_after
                   for "ending" (+ means YNAB has more)
      new_account  starting check only: {"current": Decimal} when the account
                   is newly created; its Starting Balance is set on upload,
                   so the balances will match
      already_in_ynab, to_upload
                   starting check only: how many of the statement's
                   transactions YNAB already has (skipped on upload) and how
                   many will really be added
    """
    compared = ynab_before if kind == "starting" else ynab_after
    difference = compared - statement_balance
    return {
        "kind": kind,
        "statement": statement_balance,
        "ynab_before": ynab_before,
        "ynab_after": ynab_after,
        "difference": difference,
        "matches": difference == 0 or new_account is not None,
        "new_account": new_account,
        "already_in_ynab": already_in_ynab,
        "to_upload": to_upload,
    }


def check_starting_balance(
    starting_balance,
    upload_total,
    account_id,
    plan_id,
    access_token=None,
    already_in_ynab=0,
    to_upload=None,
):
    """Before the upload: does the YNAB account balance equal the statement's
    starting balance?

    starting_balance - Decimal, the statement's starting balance
    upload_total     - Decimal, sum of the transactions that will really be
                       added, i.e. not already in YNAB (for "YNAB balance
                       AFTER upload")
    already_in_ynab, to_upload
                     - counts from ynab_api.client.find_already_imported(), for
                       the "X transactions already in YNAB" line

    Read only: a new account is reported in "new_account", and its Starting
    Balance is changed later by set_new_account_starting_balance().
    Raises ynab.ApiException if YNAB can't be read.
    """
    ynab_before = get_account_balance(plan_id, account_id, access_token)
    starting_txn = find_new_account_starting_balance(plan_id, account_id, access_token)
    if starting_txn is not None:
        # The Starting Balance will be replaced by the statement's
        new_account = {"current": Decimal(starting_txn.amount) / 1000}
        ynab_after = starting_balance + upload_total
    else:
        new_account = None
        ynab_after = ynab_before + upload_total
    return compare_balance(
        "starting",
        starting_balance,
        ynab_before,
        ynab_after,
        new_account,
        already_in_ynab,
        to_upload,
    )


# =============================================================================
# SET - a new account's Starting Balance (just before the upload)
# =============================================================================


def set_new_account_starting_balance(
    starting_balance, account_id, plan_id, access_token=None
):
    """If the account is newly created, set its Starting Balance transaction to
    the statement's starting balance (TransactionsApi.update_transaction).

    Checks again right before changing anything, so an account that got other
    transactions in the meantime is left alone.

    Returns {"from": Decimal, "to": Decimal} for a new account (unchanged if it
    already matched), None if the account isn't new.
    """
    starting_txn = find_new_account_starting_balance(plan_id, account_id, access_token)
    if starting_txn is None:
        return None

    current = Decimal(starting_txn.amount) / 1000
    if current != starting_balance:
        # Only the amount changes; payee, category and date stay as YNAB made them
        update = ynab.PutTransactionWrapper(
            transaction=ynab.ExistingTransaction(amount=int(starting_balance * 1000))
        )
        with ynab_client(access_token) as api_client:
            ynab.TransactionsApi(api_client).update_transaction(
                plan_id, starting_txn.id, update
            )
    return {"from": current, "to": starting_balance}


# =============================================================================
# MESSAGE - text for the command line and the web page
# =============================================================================


def balance_check_message(check):
    """Text for a check from check_starting_balance() or
    ynab_api.client.check_ending_balance(). A mismatch, or transactions already in
    YNAB, gives several lines:

        3 transactions already in YNAB. Only 22 will be uploaded.
        Starting balance does not match YNAB. You can still import.
        Statement starting balance: 7,873.54
        YNAB balance BEFORE upload: -2,598.98
        YNAB balance AFTER upload: -5,197.96

    "AFTER upload" counts only the transactions that will really be added.
    """
    kind = check["kind"]
    new_account = check.get("new_account")
    already = check.get("already_in_ynab") or 0
    if new_account:
        if new_account["current"] == check["statement"]:
            return (
                "New YNAB account: its Starting Balance already equals the "
                f"statement starting balance ({check['statement']:,.2f})."
            )
        return (
            f"New YNAB account: its Starting Balance ({new_account['current']:,.2f}) "
            f"will be set to the statement starting balance "
            f"({check['statement']:,.2f}) when you upload."
        )
    if check["matches"] and not already:
        return f"{kind.capitalize()} balance matches YNAB: {check['statement']:,.2f}"

    lines = []
    if already:
        lines.append(
            f"{already} transactions already in YNAB. "
            f"Only {check['to_upload']} will be uploaded."
        )
    if check["matches"]:
        lines.append("Starting balance matches YNAB.")
    elif kind == "starting":
        lines.append("Starting balance does not match YNAB. You can still import.")
    else:
        lines.append(
            "Ending balance does not match YNAB. Make any adjustment in YNAB "
            "as you see fit."
        )
    lines.append(f"Statement {kind} balance: {check['statement']:,.2f}")
    if check["ynab_before"] is not None:
        lines.append(f"YNAB balance BEFORE upload: {check['ynab_before']:,.2f}")
    if check["ynab_after"] is not None:
        lines.append(f"YNAB balance AFTER upload: {check['ynab_after']:,.2f}")
    return "\n".join(lines)


def new_account_message(result):
    """Text for the result of set_new_account_starting_balance()."""
    if result["from"] == result["to"]:
        return (
            "New YNAB account: its Starting Balance already equals the statement "
            f"starting balance ({result['to']:,.2f})."
        )
    return (
        f"New YNAB account: Starting Balance set from {result['from']:,.2f} to "
        f"{result['to']:,.2f} (the statement starting balance)."
    )
