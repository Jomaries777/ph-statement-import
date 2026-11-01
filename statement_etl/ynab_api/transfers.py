"""
ph-statement-import - import statement rows as YNAB transfers.

statement_etl/transfers.py decides which rows are transfers (the user's rules);
this module makes YNAB show them as transfers.

In YNAB a transfer is a transaction whose payee is the other account's
"transfer payee" (Account.transfer_payee_id). YNAB then adds the other side to
that account by itself, with no import_id. So when the other wallet's
statement is imported later, its matching row is already there:

    1. GCash statement:  "Transfer to 0917123"  -500.00  -> sent as a transfer to GoTyme;
                         YNAB adds +500.00 "Transfer : GCash" to the GoTyme account
    2. GoTyme statement: "InstaPay from GCash"  +500.00  -> already in YNAB (step 1),
                         so it isn't sent again

Step 2 needs a rule for the GoTyme side too; without one, the row is imported
as an ordinary deposit.

A row whose statement doesn't say which account it's with (GoTyme Everyday's
"Transfer to GoalSave", with several GoalSave accounts) has a tuple of them as
its destination. It can only take step 2: it's linked to the transfer one of
them already made (same amount, within MATCH_DAYS), or, if none did, imported
as an ordinary row. So the GoalSave statement is imported first.

    transfer_payee_ids() - {account id: that account's transfer payee id}
    apply_transfers()    - point transfer rows at their account's transfer
                           payee, and find the ones already in YNAB
"""

from datetime import date, timedelta

import ynab

from statement_etl.ynab_api.client import ynab_client

# The other side of a transfer can be dated a day or two apart on the two statements
MATCH_DAYS = 3


def transfer_payee_ids(plan_id, access_token=None) -> dict[str, str]:
    """{account id: its transfer payee id} for the plan's open accounts."""
    with ynab_client(access_token) as api_client:
        accounts = ynab.AccountsApi(api_client).get_accounts(plan_id).data.accounts
    return {
        str(a.id): str(a.transfer_payee_id)
        for a in accounts
        if a.transfer_payee_id and not (a.deleted or a.closed)
    }


def _other_sides(account_id, plan_id, since: date, access_token) -> list:
    """The account's transfers YNAB added for imports into other accounts: they
    have a transfer_account_id and no import_id."""
    with ynab_client(access_token) as api_client:
        existing = (
            ynab.TransactionsApi(api_client)
            .get_transactions_by_account(plan_id, account_id, since_date=since)
            .data.transactions
        )
    return [t for t in existing if t.transfer_account_id and not t.import_id and not t.deleted]


def _take_other_side(other_sides: list, transaction: dict, to_accounts: tuple):
    """Remove and return the other side of `transaction` (same amount, from
    one of the destination accounts, dated within MATCH_DAYS), or None.
    Removed, so two equal rows don't both match one."""
    day = date.fromisoformat(transaction["date"])
    for other in other_sides:
        if (
            str(other.transfer_account_id) in to_accounts
            and other.amount == transaction["amount"]
            and abs((other.var_date - day).days) <= MATCH_DAYS
        ):
            other_sides.remove(other)
            return other
    return None


def apply_transfers(
    transactions, transfers, account_id, plan_id, access_token=None
) -> tuple[list[dict], set[str]]:
    """Turn the rows in `transfers` into YNAB transfers.

    transactions - dicts from ynab_api.format.to_ynab(), one per statement row
    transfers    - {row index: destination YNAB account id}
                   (statement_etl.transfers.find_transfers()). A tuple of ids
                   (empty: any account) links the row only to a transfer one
                   of them already made; otherwise it stays an ordinary row.

    Returns (transactions, already_in_ynab):
        transactions    - a copy; each transfer row gets "payee_id", the
                          destination's transfer payee (YNAB uses it instead
                          of payee_name)
        already_in_ynab - import_ids of transfer rows whose other side is
                          already in this account (see the module docstring).
                          Don't upload them: they'd be counted twice.

    A destination that isn't an open account in this plan leaves its row an
    ordinary transaction. With no transfers, YNAB isn't called.
    Raises ynab.ApiException if YNAB rejects a request.
    """
    if not transfers:
        return list(transactions), set()
    payee_ids = transfer_payee_ids(plan_id, access_token)
    destinations = {}  # {row index: the account id, or a tuple of them}
    for index, to in transfers.items():
        if isinstance(to, tuple):
            candidates = tuple(a for a in to or payee_ids if a in payee_ids and a != account_id)
            if candidates:
                destinations[index] = candidates
        elif to in payee_ids and to != account_id:
            destinations[index] = to
    if not destinations:
        return list(transactions), set()

    earliest = min(date.fromisoformat(transactions[i]["date"]) for i in destinations)
    other_sides = _other_sides(
        account_id, plan_id, earliest - timedelta(days=MATCH_DAYS), access_token
    )
    result, already_in_ynab = [], set()
    for index, transaction in enumerate(transactions):
        to = destinations.get(index)
        if to is None:
            result.append(transaction)
        elif isinstance(to, tuple):  # one of these accounts, if it made the transfer
            other = _take_other_side(other_sides, transaction, to)
            if other is None:
                result.append(transaction)
            else:
                to_account = str(other.transfer_account_id)
                result.append({**transaction, "payee_id": payee_ids[to_account]})
                already_in_ynab.add(transaction["import_id"])
        else:
            result.append({**transaction, "payee_id": payee_ids[to]})
            if _take_other_side(other_sides, transaction, (to,)):
                already_in_ynab.add(transaction["import_id"])
    return result, already_in_ynab


def unlinked(transfers, transactions, already_in_ynab) -> list[int]:
    """The rows with a tuple destination (see apply_transfers()) that no
    transfer was found for: they're imported as ordinary rows.

    unlinked({3: ("acc-22222", "acc-11111")}, transactions, set()) -> [3]
    """
    return sorted(
        index
        for index, to in (transfers or {}).items()
        if isinstance(to, tuple) and transactions[index]["import_id"] not in already_in_ynab
    )
