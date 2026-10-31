"""
Tests for ynab_api/accounts.py: the user, plan and account lookups the web
page's dropdowns use, and the command line that lists them.

Run from the project folder:  python -m unittest discover tests

YNAB is never called: the SDK's API classes are mocked. All data here is made up.
"""

import contextlib
import io
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

import ynab

from statement_etl.ynab_api import accounts
from tests.fakes import ACCOUNT_ID, PLAN_ID, fake_client, response, ynab_account


class AccountsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(accounts, "ynab_client", return_value=fake_client())
        self.ynab_client = patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, name):
        patcher = mock.patch.object(ynab, name)
        self.addCleanup(patcher.stop)
        return patcher.start().return_value

    def test_get_user(self):
        user_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        self.api("UserApi").get_user.return_value = response(user=SimpleNamespace(id=user_id))
        self.assertEqual(accounts.get_user("token"), "33333333-3333-3333-3333-333333333333")
        self.ynab_client.assert_called_once_with("token")

    def test_get_plans(self):
        currency = SimpleNamespace(to_dict=lambda: {"iso_code": "PHP", "currency_symbol": "₱"})
        self.api("PlansApi").get_plans.return_value = response(plans=[
            SimpleNamespace(id=uuid.UUID(PLAN_ID), name="Test Plan", currency_format=currency),
            SimpleNamespace(id="plan-2", name="No Format", currency_format=None),
        ])
        self.assertEqual(accounts.get_plans("token"), [
            {"id": PLAN_ID, "name": "Test Plan", "currency_format": {"iso_code": "PHP", "currency_symbol": "₱"}},
            {"id": "plan-2", "name": "No Format", "currency_format": None},
        ])

    def test_get_accounts_lists_open_accounts(self):
        api = self.api("AccountsApi")
        api.get_accounts.return_value = response(accounts=[
            ynab_account(id=uuid.UUID(ACCOUNT_ID), name="Test Wallet", balance_formatted="₱1,249.50"),
            ynab_account(id="account-2", name="No Type", type=None),
            ynab_account(id="account-3", name="Deleted", deleted=True),
            ynab_account(id="account-4", name="Closed", closed=True),
        ])
        self.assertEqual(accounts.get_accounts(PLAN_ID, "token"), [
            {"id": ACCOUNT_ID, "name": "Test Wallet", "type": "checking", "balance": "₱1,249.50"},
            {"id": "account-2", "name": "No Type", "type": "", "balance": "₱0.00"},
        ])
        api.get_accounts.assert_called_once_with(PLAN_ID)
        self.ynab_client.assert_called_once_with("token")


class MainTest(unittest.TestCase):
    def test_lists_plans_and_accounts(self):
        plans = [{"id": "plan-1", "name": "Test Plan", "currency_format": None}]
        wallet = [{"id": "account-1", "name": "Test Wallet", "type": "checking", "balance": "₱0.00"}]
        stdout = io.StringIO()
        with mock.patch.object(accounts, "get_plans", return_value=plans), \
             mock.patch.object(accounts, "get_accounts", return_value=wallet) as get_accounts, \
             contextlib.redirect_stdout(stdout):
            accounts.main()
        get_accounts.assert_called_once_with("plan-1")
        self.assertEqual(stdout.getvalue(), (
            "PLAN  Test Plan\n"
            "      --plan-id plan-1\n"
            "      ACCOUNT  " + "Test Wallet".ljust(30) + " account-1\n"
            "\n"
        ))


if __name__ == "__main__":
    unittest.main()
