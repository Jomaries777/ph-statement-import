"""
Tests for ynab_api/client.py: the SDK client, the duplicate check, the upload
and the ending balance check.

Run from the project folder:  python -m unittest discover tests

YNAB is never called: the SDK's API classes are mocked. All data here is made up.
"""

import unittest
import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

import ynab

from statement_etl.ynab_api import client
from tests.fakes import ACCOUNT_ID, PLAN_ID, fake_client, response, ynab_account, ynab_txn

TRANSACTIONS = [
    {
        "date": "2026-10-03",
        "payee_name": "Test Shop",
        "import_id": "ETLPH:aaa",
        "amount": -250500,
        "memo": "GCash Ref: 1000000000002",
        "flag_color": None,
    },
    {
        "date": "2026-10-01",
        "payee_name": "Refund from Test Shop",
        "import_id": "ETLPH:bbb",
        "amount": 50000,
        "memo": "GCash Ref: R1000000000002",
        "flag_color": "red",
    },
]


class MockedYnabTest(unittest.TestCase):
    """Patches client.ynab_client and one SDK API class."""

    API = None

    def setUp(self):
        self.api_client = fake_client()
        patcher = mock.patch.object(client, "ynab_client", return_value=self.api_client)
        self.ynab_client = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ynab, self.API)
        self.api = patcher.start().return_value
        self.addCleanup(patcher.stop)


class YnabClientTest(unittest.TestCase):
    def test_uses_the_given_token(self):
        with mock.patch.dict("os.environ", {"BEARER_TOKEN": "env-token"}):
            api_client = client.ynab_client("page-token")
        self.assertEqual(api_client.configuration.access_token, "page-token")
        self.assertEqual(api_client.configuration.host, "https://api.ynab.com/v1")

    def test_falls_back_to_bearer_token(self):
        with mock.patch.dict("os.environ", {"BEARER_TOKEN": "env-token"}):
            self.assertEqual(client.ynab_client().configuration.access_token, "env-token")

    def test_no_token_at_all(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(KeyError):
            client.ynab_client()


class FindAlreadyImportedTest(MockedYnabTest):
    API = "TransactionsApi"

    def test_nothing_to_check(self):
        self.assertEqual(client.find_already_imported([], ACCOUNT_ID, PLAN_ID), set())
        self.ynab_client.assert_not_called()

    def test_finds_our_import_ids(self):
        self.api.get_transactions_by_account.return_value = response(
            transactions=[
                ynab_txn(import_id="ETLPH:aaa"),
                ynab_txn(
                    import_id="ETLPH:bbb", deleted=True
                ),  # deleted in YNAB: will be added again
                ynab_txn(import_id="ETLPH:other"),  # from another statement
                ynab_txn(import_id=None),  # entered by hand
            ]
        )
        self.assertEqual(
            client.find_already_imported(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token"), {"ETLPH:aaa"}
        )
        self.ynab_client.assert_called_once_with("token")
        # from the earliest date in the batch
        self.api.get_transactions_by_account.assert_called_once_with(
            PLAN_ID, ACCOUNT_ID, since_date=date(2026, 10, 1)
        )


class ImportTransactionsTest(MockedYnabTest):
    API = "TransactionsApi"

    def test_creates_unapproved_cleared_transactions(self):
        self.api.create_transaction.return_value = response(
            transactions=["created"], duplicate_import_ids=[]
        )
        long_payee = {**TRANSACTIONS[0], "payee_name": "P" * 250}
        no_optional = {k: v for k, v in TRANSACTIONS[1].items() if k not in ("memo", "flag_color")}

        result = client.import_transactions([long_payee, no_optional], ACCOUNT_ID, PLAN_ID, "token")

        self.assertEqual(result.transactions, ["created"])
        self.ynab_client.assert_called_once_with("token")
        plan_id, wrapper = self.api.create_transaction.call_args.args
        self.assertEqual(plan_id, PLAN_ID)
        first, second = wrapper.transactions
        self.assertIsInstance(first, ynab.NewTransaction)
        self.assertEqual(first.account_id, uuid.UUID(ACCOUNT_ID))
        self.assertEqual(first.var_date, date(2026, 10, 3))
        self.assertEqual(first.amount, -250500)
        self.assertEqual(first.payee_name, "P" * 200)  # YNAB's limit
        self.assertEqual(first.memo, "GCash Ref: 1000000000002")
        self.assertEqual(first.import_id, "ETLPH:aaa")
        self.assertIsNone(first.flag_color)
        self.assertEqual(first.cleared.value, "cleared")
        self.assertIs(first.approved, False)
        self.assertIsNone(second.memo)
        self.assertIsNone(second.flag_color)

    def test_flag_color(self):
        self.api.create_transaction.return_value = response(transactions=[])
        client.import_transactions(TRANSACTIONS, ACCOUNT_ID, PLAN_ID)
        wrapper = self.api.create_transaction.call_args.args[1]
        self.assertEqual(wrapper.transactions[1].flag_color.value, "red")


class CheckEndingBalanceTest(MockedYnabTest):
    API = "AccountsApi"

    def check(self, balance, ending="1249.50", ynab_before=Decimal("500.00")):
        self.api.get_account_by_id.return_value = response(account=ynab_account(balance=balance))
        return client.check_ending_balance(
            Decimal(ending), ACCOUNT_ID, PLAN_ID, "token", ynab_before
        )

    def test_matches(self):
        self.assertEqual(
            self.check(1249500),
            {
                "kind": "ending",
                "statement": Decimal("1249.50"),
                "ynab_before": Decimal("500.00"),
                "ynab_after": Decimal("1249.5"),
                "difference": Decimal("0"),
                "matches": True,
                "new_account": None,
            },
        )
        self.api.get_account_by_id.assert_called_once_with(PLAN_ID, ACCOUNT_ID)
        self.ynab_client.assert_called_once_with("token")

    def test_mismatch(self):
        check = self.check(1300000, ynab_before=None)
        self.assertEqual(check["difference"], Decimal("50.50"))
        self.assertFalse(check["matches"])
        self.assertIsNone(check["ynab_before"])

    def test_negative_balance(self):
        self.assertEqual(self.check(-2598980, ending="-2598.98")["ynab_after"], Decimal("-2598.98"))


if __name__ == "__main__":
    unittest.main()
