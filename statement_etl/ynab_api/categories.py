"""
ph-statement-import - YNAB categories, and the transactions that don't have one yet.

Used by categorize.py, which asks Gemini to pick each transaction's category:
    get_categories()                 - the categories a transaction can be given
    get_uncategorized_transactions() - transactions with no category yet
    uncategorized_created()          - the ones an import just created with no category
    set_categories()                 - give transactions their category, in one call

Requirements:
    pip install ynab

Environment:
    BEARER_TOKEN - YNAB personal access token (None as access_token uses it)
"""

from decimal import Decimal

import ynab

from statement_etl.ynab_api.client import ynab_client

# Where income goes. It's one of YNAB's internal categories, but the only one a
# transaction should be given: the others ("Uncategorized", ...) YNAB manages itself.
READY_TO_ASSIGN = "Inflow: Ready to Assign"

# YNAB moves money into these by itself when a credit card is used
CREDIT_CARD_PAYMENTS_GROUP = "Credit Card Payments"

# YNAB's name for "no category yet", which some transactions carry with an id
UNCATEGORIZED = "Uncategorized"


def get_categories(plan_id, access_token=None) -> list[dict]:
    """CategoriesApi.get_categories(): the categories a transaction can be given.

    Returns [{"id", "name", "group"}], e.g.
    [{"id": "c1", "name": "Groceries", "group": "Everyday"}, ...].

    Hidden and deleted categories, credit card payment categories and YNAB's
    internal ones are left out, except "Inflow: Ready to Assign" for income.
    Raises ynab.ApiException if YNAB rejects the request.
    """
    with ynab_client(access_token) as api_client:
        groups = ynab.CategoriesApi(api_client).get_categories(plan_id).data.category_groups

    categories = []
    for group in groups:
        if group.deleted or group.hidden or group.name == CREDIT_CARD_PAYMENTS_GROUP:
            continue
        for category in group.categories:
            if category.deleted or category.hidden:
                continue
            # Only the category's own flag: YNAB marks every group internal, even yours
            if category.internal and category.name != READY_TO_ASSIGN:
                continue
            categories.append({"id": str(category.id), "name": category.name, "group": group.name})
    return categories


def get_uncategorized_transactions(
    plan_id, account_id=None, since_date=None, access_token=None
) -> list[dict]:
    """TransactionsApi: the transactions YNAB lists as uncategorized.

    account_id - only this account's (None: every account in the plan)
    since_date - a date; None uses YNAB's default, the last year

    Returns [{"id", "date", "account", "payee", "memo", "amount"}], amount in
    pesos as a Decimal: -249000 milliunits -> Decimal("-249").

    Transfers between accounts and split transactions are left out: a
    transfer needs no category, and a split's categories are on its parts.
    Raises ynab.ApiException if YNAB rejects the request.
    """
    with ynab_client(access_token) as api_client:
        api = ynab.TransactionsApi(api_client)
        if account_id:
            response = api.get_transactions_by_account(
                plan_id, account_id, since_date=since_date, type="uncategorized"
            )
        else:
            response = api.get_transactions(plan_id, since_date=since_date, type="uncategorized")

    return [
        transaction_dict(t)
        for t in response.data.transactions
        if not (t.deleted or t.transfer_account_id or t.subtransactions)
    ]


def uncategorized_created(created) -> list[dict]:
    """The transactions an import just created that still need a category.

    created - YNAB TransactionDetails, e.g. pipeline.ImportResult.created
    Returns them as transaction_dict()s. Left out, like in
    get_uncategorized_transactions(): ones YNAB already gave a category (a
    payee rule), transfers and splits; also the rows flagged red, which are
    balance corrections (e.g. MISSING TRANSACTIONS), not real purchases.
    """
    return [
        transaction_dict(t)
        for t in created
        if (t.category_id is None or t.category_name == UNCATEGORIZED)
        and not (t.deleted or t.transfer_account_id or t.subtransactions or t.flag_color)
    ]


def transaction_dict(t) -> dict:
    """What categorize.py needs of a YNAB transaction:
    {"id", "date", "account", "payee", "memo", "amount"}, amount in pesos as a
    Decimal: -249000 milliunits -> Decimal("-249")."""
    return {
        "id": t.id,
        "date": str(t.var_date),
        "account": t.account_name,
        "payee": t.payee_name or "",
        "memo": t.memo or "",
        "amount": Decimal(t.amount) / 1000,
    }


def set_categories(plan_id, category_ids, access_token=None) -> int:
    """TransactionsApi.update_transactions(): give each transaction its category.

    category_ids - {transaction id: category id}, e.g. {"t1": "c1"}

    Only the category changes: an unapproved transaction stays unapproved, so
    it's still reviewed in YNAB. Returns how many transactions YNAB updated.
    Raises ynab.ApiException if YNAB rejects the request.
    """
    if not category_ids:
        return 0
    data = ynab.PatchTransactionsWrapper(
        transactions=[
            ynab.SaveTransactionWithIdOrImportId(id=transaction_id, category_id=category_id)
            for transaction_id, category_id in category_ids.items()
        ]
    )
    with ynab_client(access_token) as api_client:
        response = ynab.TransactionsApi(api_client).update_transactions(plan_id, data)
    return len(response.data.transactions or [])
