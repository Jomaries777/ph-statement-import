"""
ph-statement-import - send transformed transactions to YNAB.

Reusable for every e-wallet / bank, after parsing and transforming:
    find_already_imported() - before the upload, find the transactions YNAB
                              already has (same import_id on the account);
                              YNAB skips them, so only the rest are added
    import_transactions()  - create the transactions in YNAB
    check_ending_balance() - after the upload, compare the YNAB account balance
                             with the statement's ending balance (a warning
                             only; any adjustment is up to the user)

The check before the upload (starting balance) is in ynab_api/balance.py.

Requirements:
    pip install ynab

Environment:
    BEARER_TOKEN - YNAB personal access token (CLI, and the web app's default;
                   with OAuth the web app passes its token as access_token)
"""

import os
from datetime import date
from decimal import Decimal

import ynab
from dotenv import load_dotenv  # type: ignore

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


def ynab_client(access_token=None):
    """SDK ApiClient for the given token, or $BEARER_TOKEN from .env.

    Use it in a `with` block:  with ynab_client(token) as api_client: ...
    """
    configuration = ynab.Configuration(
        host="https://api.ynab.com/v1",
        access_token=access_token or os.environ["BEARER_TOKEN"],
    )
    return ynab.ApiClient(configuration)


def find_already_imported(transactions, account_id, plan_id, access_token=None):
    """Before the upload: which of these transactions are already in YNAB?

    YNAB skips a transaction whose import_id is already on the account (e.g.
    the same statement uploaded before), so only the others will be added.
    Looks at the account's transactions from the earliest date in the batch
    (TransactionsApi.get_transactions_by_account).

    transactions - list of dicts from transform.transform()

    Returns the set of import_ids already in the account (empty if none).
    Raises ynab.ApiException if YNAB rejects the request.
    """
    if not transactions:
        return set()
    ours = {t["import_id"] for t in transactions}
    since = min(date.fromisoformat(t["date"]) for t in transactions)
    with ynab_client(access_token) as api_client:
        existing = (
            ynab.TransactionsApi(api_client)
            .get_transactions_by_account(plan_id, account_id, since_date=since)
            .data.transactions
        )
    return {t.import_id for t in existing if t.import_id in ours and not t.deleted}


def import_transactions(transactions, account_id, plan_id, access_token=None):
    """Create transactions in YNAB.

    transactions - list of dicts from transform.transform()
                   (date, payee_name, import_id, amount, memo)
    account_id   - the YNAB account to add them to (your GCash account)
    plan_id      - YNAB plan (budget) id that contains that account
    access_token - token from the web app; None uses $BEARER_TOKEN

    Returns the SaveTransactionsResponseData (created ids + duplicate_import_ids).
    """
    # Convert parser rows into YNAB NewTransaction objects
    new_transactions = [
        ynab.NewTransaction.from_dict(
            {
                "account_id": account_id,
                "date": t["date"],
                "amount": t["amount"],
                "payee_name": t["payee_name"][:200],
                "memo": t.get("memo"),
                "import_id": t["import_id"],
                # e.g. "red" on balance-correction rows so they stand out
                "flag_color": t.get("flag_color"),
                "cleared": "cleared",
                # Left unapproved so each one is reviewed and approved in YNAB
                "approved": False,
            }
        )
        for t in transactions
    ]
    data = ynab.PostTransactionsWrapper(transactions=new_transactions)

    # Enter a context with an instance of the API client
    with ynab_client(access_token) as api_client:
        api_instance = ynab.TransactionsApi(api_client)
        # Create multiple transactions. YNAB skips any import_id it has already seen,
        # so re-running the same statement does not create duplicates.
        api_response = api_instance.create_transaction(plan_id, data)

    return api_response.data


def check_ending_balance(
    ending_balance, account_id, plan_id, access_token=None, ynab_before=None
):
    """After the upload: does the YNAB account balance equal the statement's
    ending balance?

    ending_balance - Decimal, e.g. from parsers.gcash.statement_balances()
    ynab_before    - YNAB balance read before the upload, shown in the warning
                     as "YNAB balance BEFORE upload" (None to leave it out)

    A mismatch is only reported, never fixed: how to adjust it in YNAB is up to
    the user. Returns the same shape as ynab_api.balance.compare_balance():
    {"kind": "ending", "statement", "ynab_before", "ynab_after", "difference",
    "matches", "new_account"}, so ynab_api.balance.balance_check_message() can
    describe it.

    Reads the balance here rather than via ynab_api.balance.get_account_balance(),
    because ynab_api.balance imports this module (ynab_client).
    """
    with ynab_client(access_token) as api_client:
        account = (
            ynab.AccountsApi(api_client)
            .get_account_by_id(plan_id, account_id)
            .data.account
        )
    ynab_after = Decimal(account.balance) / 1000  # milliunits -> currency units
    difference = ynab_after - ending_balance
    return {
        "kind": "ending",
        "statement": ending_balance,
        "ynab_before": ynab_before,
        "ynab_after": ynab_after,
        "difference": difference,
        "matches": difference == 0,
        "new_account": None,
    }
