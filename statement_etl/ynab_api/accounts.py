"""
ph-statement-import - list YNAB plans (budgets) and their accounts with
ids, to find YNAB_ACCOUNT_ID and --plan-id for pipeline.py.

get_user(), get_plans() and get_accounts() are also used by app.py for the
web page's connection check and Plan / Account dropdowns.

Environment:
    BEARER_TOKEN - YNAB personal access token (CLI, and the web app's default;
                   with OAuth the web app passes its token as access_token)
"""

import ynab
from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.ynab_api.client import ynab_client

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


def get_user(access_token=None):
    """UserApi.get_user(): confirms the token works. Returns the YNAB user id."""
    with ynab_client(access_token) as api_client:
        return str(ynab.UserApi(api_client).get_user().data.user.id)


def get_plans(access_token=None):
    """PlansApi.get_plans(): [{id, name, currency_format}]."""
    with ynab_client(access_token) as api_client:
        plans = ynab.PlansApi(api_client).get_plans().data.plans
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "currency_format": p.currency_format.to_dict() if p.currency_format else None,
        }
        for p in plans
    ]


def get_accounts(plan_id, access_token=None):
    """AccountsApi.get_accounts(plan_id): open accounts as [{id, name, type, balance}]."""
    with ynab_client(access_token) as api_client:
        accounts = ynab.AccountsApi(api_client).get_accounts(plan_id).data.accounts
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "type": a.type.value if a.type else "",
            "balance": a.balance_formatted,
        }
        for a in accounts
        if not (a.deleted or a.closed)
    ]


def main():
    for plan in get_plans():
        print(f"PLAN  {plan['name']}\n      --plan-id {plan['id']}")
        for account in get_accounts(plan["id"]):
            print(f"      ACCOUNT  {account['name']:<30} {account['id']}")
        print()


if __name__ == "__main__":
    main()
