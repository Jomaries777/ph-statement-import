"""
An in-memory YNAB for the browser tests: stateful fakes of the SDK's UserApi,
PlansApi, AccountsApi and TransactionsApi.

FakeYnab.patch() swaps them in for ynab.UserApi / ynab.PlansApi / ... with
mock.patch, so pipeline.py, ynab_api/* and web/app.py all run for real; only
the HTTP calls are replaced. It also makes web/token_store.py report "no
secure store", so the real Keychain is never read or written.

Seeded with one plan and two accounts (all made up):
    Test Wallet      - existing account, balance 1,000.00 (the starting
                       balance of make_pdf.balanced())
    New Test Wallet  - newly created: only a "Starting Balance" transaction of
                       250.00 in "Inflow: Ready to Assign"

TOKEN is the only token YNAB accepts; any other raises
ynab.exceptions.UnauthorizedException, like a revoked token.
"""

import contextlib
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

import ynab

from statement_etl.web import token_store

TOKEN = "test-token"

PLAN_ID = "00000000-0000-4000-8000-000000000001"
PLAN_NAME = "Test Plan"
EXISTING_ACCOUNT_ID = "00000000-0000-4000-8000-000000000002"
EXISTING_ACCOUNT_NAME = "Test Wallet"
NEW_ACCOUNT_ID = "00000000-0000-4000-8000-000000000003"
NEW_ACCOUNT_NAME = "New Test Wallet"
USER_ID = "00000000-0000-4000-8000-000000000009"

STARTING_BALANCE_PAYEE = "Starting Balance"
STARTING_BALANCE_CATEGORY = "Inflow: Ready to Assign"

# Philippine peso, as YNAB returns it in a plan's currency_format
CURRENCY_FORMAT = {
    "iso_code": "PHP",
    "example_format": "123,456.78",
    "decimal_digits": 2,
    "decimal_separator": ".",
    "symbol_first": True,
    "group_separator": ",",
    "currency_symbol": "₱",
    "display_symbol": True,
}


def _error_body(id_, name, detail):
    return f'{{"error": {{"id": "{id_}", "name": "{name}", "detail": "{detail}"}}}}'


def _unauthorized():
    return ynab.exceptions.UnauthorizedException(
        status=401,
        reason="Unauthorized",
        body=_error_body("401", "unauthorized", "Unauthorized"),
    )


def _not_found():
    return ynab.exceptions.NotFoundException(
        status=404,
        reason="Not Found",
        body=_error_body("404.2", "resource_not_found", "Resource not found"),
    )


def _response(**data):
    """An SDK response: everything the code reads is under .data."""
    return SimpleNamespace(data=SimpleNamespace(**data))


class FakeYnab:
    """YNAB's state: accounts and their transactions. reset() re-seeds it."""

    def __init__(self):
        self.reset()

    def reset(self):
        today = date.today()
        self.accounts = {
            EXISTING_ACCOUNT_ID: {"name": EXISTING_ACCOUNT_NAME, "transactions": []},
            NEW_ACCOUNT_ID: {"name": NEW_ACCOUNT_NAME, "transactions": []},
        }
        # Existing account: two transactions, older than a year, 1,000.00 in total
        self._add(
            EXISTING_ACCOUNT_ID,
            date(2024, 1, 1),
            STARTING_BALANCE_PAYEE,
            400000,
            category_name=STARTING_BALANCE_CATEGORY,
        )
        self._add(EXISTING_ACCOUNT_ID, date(2024, 6, 1), "Sample Transfer In", 600000)
        # New account: only the Starting Balance YNAB makes when it's created
        self._add(
            NEW_ACCOUNT_ID,
            today,
            STARTING_BALANCE_PAYEE,
            250000,
            category_name=STARTING_BALANCE_CATEGORY,
        )
        self.calls = []  # (method, args) of every API call, for assertions

    # ---- state helpers (also used by the tests) -----------------------------

    def _add(
        self,
        account_id,
        var_date,
        payee_name,
        amount,
        category_name=None,
        memo=None,
        import_id=None,
        flag_color=None,
    ):
        transaction = SimpleNamespace(
            id=str(uuid.uuid4()),
            account_id=account_id,
            var_date=var_date,
            payee_name=payee_name,
            category_name=category_name,
            memo=memo,
            amount=amount,
            import_id=import_id,
            flag_color=flag_color,
            cleared=ynab.TransactionClearedStatus.CLEARED,
            approved=import_id is None,
            deleted=False,
        )
        self.accounts[account_id]["transactions"].append(transaction)
        return transaction

    def transactions(self, account_id):
        """The account's transactions that aren't deleted."""
        return [t for t in self.accounts[account_id]["transactions"] if not t.deleted]

    def balance(self, account_id):
        """The account's balance in milliunits."""
        return sum(t.amount for t in self.transactions(account_id))

    def _account(self, plan_id, account_id):
        if str(plan_id) != PLAN_ID or str(account_id) not in self.accounts:
            raise _not_found()
        return self.accounts[str(account_id)]

    def _check(self, api_client, method, *args):
        if api_client.configuration.access_token != TOKEN:
            raise _unauthorized()
        self.calls.append((method, args))

    # ---- fake SDK APIs -------------------------------------------------------

    def patch(self):
        """Context manager: install the fakes on the ynab module and turn off
        the OS secure store."""
        stack = contextlib.ExitStack()
        for name, cls in (
            ("UserApi", _UserApi),
            ("PlansApi", _PlansApi),
            ("AccountsApi", _AccountsApi),
            ("TransactionsApi", _TransactionsApi),
        ):
            stack.enter_context(
                mock.patch.object(ynab, name, lambda api_client, cls=cls: cls(self, api_client))
            )
        stack.enter_context(
            mock.patch.multiple(
                token_store,
                available=lambda: False,
                store_name=lambda: None,
                load_token=lambda: None,
                save_token=lambda token: False,
                delete_token=lambda: None,
            )
        )
        return stack


class _Api:
    def __init__(self, ynab_state, api_client):
        self.ynab = ynab_state
        self.api_client = api_client


class _UserApi(_Api):
    def get_user(self):
        self.ynab._check(self.api_client, "get_user")
        return _response(user=SimpleNamespace(id=uuid.UUID(USER_ID)))


class _CurrencyFormat:
    def to_dict(self):
        return dict(CURRENCY_FORMAT)


class _PlansApi(_Api):
    def get_plans(self, *args, **kwargs):
        self.ynab._check(self.api_client, "get_plans")
        plan = SimpleNamespace(
            id=uuid.UUID(PLAN_ID), name=PLAN_NAME, currency_format=_CurrencyFormat()
        )
        return _response(plans=[plan])


class _AccountsApi(_Api):
    def _account(self, account_id):
        balance = self.ynab.balance(account_id)
        return SimpleNamespace(
            id=uuid.UUID(account_id),
            name=self.ynab.accounts[account_id]["name"],
            type=ynab.AccountType.CASH,
            balance=balance,
            balance_formatted=f"{'-' if balance < 0 else ''}₱{abs(balance) / 1000:,.2f}",
            closed=False,
            deleted=False,
        )

    def get_accounts(self, plan_id, *args, **kwargs):
        self.ynab._check(self.api_client, "get_accounts", plan_id)
        if str(plan_id) != PLAN_ID:
            raise _not_found()
        return _response(accounts=[self._account(a) for a in self.ynab.accounts])

    def get_account_by_id(self, plan_id, account_id):
        self.ynab._check(self.api_client, "get_account_by_id", plan_id, account_id)
        self.ynab._account(plan_id, account_id)
        return _response(account=self._account(str(account_id)))


class _TransactionsApi(_Api):
    def get_transactions_by_account(self, plan_id, account_id, since_date=None, **kwargs):
        """Like YNAB: without since_date, only the last year."""
        self.ynab._check(
            self.api_client, "get_transactions_by_account", plan_id, account_id, since_date
        )
        account = self.ynab._account(plan_id, account_id)
        since = since_date or date.today() - timedelta(days=365)
        return _response(transactions=[t for t in account["transactions"] if t.var_date >= since])

    def create_transaction(self, plan_id, data):
        """Create data.transactions (a PostTransactionsWrapper). Like YNAB, an
        import_id the account already has is skipped and reported in
        duplicate_import_ids."""
        self.ynab._check(self.api_client, "create_transaction", plan_id)
        created, duplicates = [], []
        for new in data.transactions:
            account_id = str(new.account_id)
            self.ynab._account(plan_id, account_id)
            seen = {t.import_id for t in self.ynab.transactions(account_id)}
            if new.import_id and new.import_id in seen:
                duplicates.append(new.import_id)
                continue
            transaction = self.ynab._add(
                account_id,
                new.var_date,
                new.payee_name,
                new.amount,
                memo=new.memo,
                import_id=new.import_id,
                flag_color=new.flag_color,
            )
            transaction.approved = bool(new.approved)
            created.append(transaction)
        return _response(
            transaction_ids=[t.id for t in created],
            transactions=created,
            duplicate_import_ids=duplicates,
        )

    def update_transaction(self, plan_id, transaction_id, data):
        """Change a transaction's amount (a PutTransactionWrapper), as
        set_new_account_starting_balance() does."""
        self.ynab._check(self.api_client, "update_transaction", plan_id, transaction_id)
        if str(plan_id) != PLAN_ID:
            raise _not_found()
        for account_id in self.ynab.accounts:
            for t in self.ynab.transactions(account_id):
                if t.id == str(transaction_id):
                    if data.transaction.amount is not None:
                        t.amount = data.transaction.amount
                    return _response(transaction=t)
        raise _not_found()
