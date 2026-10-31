"""
Tests for ynab_api/balance.py: reading the YNAB balance, spotting a new
account, comparing balances and the messages for the page and command line.

Run from the project folder:  python -m unittest discover tests

YNAB is never called: the SDK's API classes are mocked. All data here is made up.
"""

import unittest
from decimal import Decimal
from unittest import mock

import ynab

from statement_etl.ynab_api import balance
from tests.fakes import ACCOUNT_ID, PLAN_ID, fake_client, response, ynab_account, ynab_txn

STARTING_TXN = ynab_txn(
    id="start-1", payee_name="Starting Balance", category_name="Inflow: Ready to Assign", amount=0
)
OTHER_TXN = ynab_txn(id="other-1", payee_name="Test Shop", category_name="Groceries", amount=-1000)


class MockedYnabTest(unittest.TestCase):
    """Patches balance.ynab_client and the SDK's AccountsApi / TransactionsApi."""

    def setUp(self):
        self.api_client = fake_client()
        self.ynab_client = self.patch(balance, "ynab_client", return_value=self.api_client)
        self.accounts = self.patch(ynab, "AccountsApi").return_value
        self.transactions = self.patch(ynab, "TransactionsApi").return_value

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def account_history(self, recent, everything=None):
        """get_transactions_by_account returns `recent`, then `everything`."""
        self.transactions.get_transactions_by_account.side_effect = [
            response(transactions=recent),
            response(transactions=everything if everything is not None else recent),
        ]


class GetAccountBalanceTest(MockedYnabTest):
    def test_milliunits_to_decimal(self):
        self.accounts.get_account_by_id.return_value = response(
            account=ynab_account(balance=5924560)
        )
        self.assertEqual(
            balance.get_account_balance(PLAN_ID, ACCOUNT_ID, "token"), Decimal("5924.56")
        )
        self.accounts.get_account_by_id.assert_called_once_with(PLAN_ID, ACCOUNT_ID)
        self.ynab_client.assert_called_once_with("token")


class FindNewAccountTest(MockedYnabTest):
    def find(self):
        return balance.find_new_account_starting_balance(PLAN_ID, ACCOUNT_ID, "token")

    def test_is_starting_balance(self):
        self.assertTrue(balance._is_starting_balance(STARTING_TXN))
        self.assertFalse(balance._is_starting_balance(OTHER_TXN))
        self.assertFalse(
            balance._is_starting_balance(
                ynab_txn(payee_name="Starting Balance", category_name="Groceries")
            )
        )

    def test_new_account(self):
        self.account_history([STARTING_TXN])
        self.assertIs(self.find(), STARTING_TXN)
        calls = self.transactions.get_transactions_by_account.call_args_list
        self.assertEqual(calls[0], mock.call(PLAN_ID, ACCOUNT_ID, since_date=None))
        self.assertEqual(calls[1], mock.call(PLAN_ID, ACCOUNT_ID, since_date=balance.ALL_HISTORY))

    def test_deleted_transactions_dont_count(self):
        deleted = ynab_txn(payee_name="Test Shop", deleted=True)
        self.account_history([STARTING_TXN, deleted], [deleted, STARTING_TXN])
        self.assertIs(self.find(), STARTING_TXN)

    def test_not_new(self):
        cases = {
            "empty": [],
            "two transactions": [STARTING_TXN, OTHER_TXN],
            "not a starting balance": [OTHER_TXN],
        }
        for name, recent in cases.items():
            with self.subTest(name):
                self.transactions.get_transactions_by_account.reset_mock()
                self.account_history(recent)
                self.assertIsNone(self.find())
                # ruled out from the last year alone
                self.assertEqual(self.transactions.get_transactions_by_account.call_count, 1)

    def test_older_history(self):
        self.account_history([STARTING_TXN], [OTHER_TXN, STARTING_TXN])
        self.assertIsNone(self.find())


class CompareBalanceTest(unittest.TestCase):
    def test_starting_compares_the_balance_before(self):
        check = balance.compare_balance("starting", Decimal("500"), Decimal("400"), Decimal("900"))
        self.assertEqual(
            check,
            {
                "kind": "starting",
                "statement": Decimal("500"),
                "ynab_before": Decimal("400"),
                "ynab_after": Decimal("900"),
                "difference": Decimal("-100"),
                "matches": False,
                "new_account": None,
                "already_in_ynab": 0,
                "to_upload": None,
            },
        )

    def test_ending_compares_the_balance_after(self):
        check = balance.compare_balance("ending", Decimal("900"), Decimal("400"), Decimal("900"))
        self.assertEqual(check["difference"], Decimal("0"))
        self.assertTrue(check["matches"])

    def test_new_account_always_matches(self):
        check = balance.compare_balance(
            "starting",
            Decimal("500"),
            Decimal("0"),
            Decimal("900"),
            {"current": Decimal("0")},
            3,
            22,
        )
        self.assertTrue(check["matches"])
        self.assertEqual((check["already_in_ynab"], check["to_upload"]), (3, 22))


class CheckStartingBalanceTest(unittest.TestCase):
    def check(self, starting_txn):
        with (
            mock.patch.object(balance, "get_account_balance", return_value=Decimal("100")) as get,
            mock.patch.object(
                balance, "find_new_account_starting_balance", return_value=starting_txn
            ) as find,
        ):
            check = balance.check_starting_balance(
                Decimal("500"),
                Decimal("749.50"),
                ACCOUNT_ID,
                PLAN_ID,
                "token",
                already_in_ynab=1,
                to_upload=2,
            )
        get.assert_called_once_with(PLAN_ID, ACCOUNT_ID, "token")
        find.assert_called_once_with(PLAN_ID, ACCOUNT_ID, "token")
        return check

    def test_existing_account(self):
        check = self.check(None)
        self.assertEqual(check["ynab_before"], Decimal("100"))
        self.assertEqual(check["ynab_after"], Decimal("849.50"))
        self.assertEqual(check["difference"], Decimal("-400"))
        self.assertFalse(check["matches"])
        self.assertIsNone(check["new_account"])
        self.assertEqual((check["already_in_ynab"], check["to_upload"]), (1, 2))

    def test_new_account(self):
        check = self.check(ynab_txn(amount=100000))
        self.assertEqual(check["new_account"], {"current": Decimal("100")})
        self.assertEqual(check["ynab_after"], Decimal("1249.50"))  # statement start + upload
        self.assertTrue(check["matches"])


class SetNewAccountStartingBalanceTest(MockedYnabTest):
    def set_balance(self, starting_txn, starting="500.25"):
        with mock.patch.object(
            balance, "find_new_account_starting_balance", return_value=starting_txn
        ) as find:
            result = balance.set_new_account_starting_balance(
                Decimal(starting), ACCOUNT_ID, PLAN_ID, "token"
            )
        find.assert_called_once_with(PLAN_ID, ACCOUNT_ID, "token")
        return result

    def test_not_a_new_account(self):
        self.assertIsNone(self.set_balance(None))
        self.transactions.update_transaction.assert_not_called()

    def test_already_equal(self):
        result = self.set_balance(ynab_txn(id="start-1", amount=500250))
        self.assertEqual(result, {"from": Decimal("500.25"), "to": Decimal("500.25")})
        self.transactions.update_transaction.assert_not_called()

    def test_sets_the_amount(self):
        result = self.set_balance(ynab_txn(id="start-1", amount=0))
        self.assertEqual(result, {"from": Decimal("0"), "to": Decimal("500.25")})
        plan_id, txn_id, wrapper = self.transactions.update_transaction.call_args.args
        self.assertEqual((plan_id, txn_id), (PLAN_ID, "start-1"))
        self.assertEqual(wrapper.transaction.amount, 500250)
        self.assertIsNone(wrapper.transaction.payee_name)  # only the amount changes
        self.ynab_client.assert_called_once_with("token")


class MessagesTest(unittest.TestCase):
    def starting(self, statement, before, after, new_account=None, already=0, to_upload=None):
        return balance.compare_balance(
            "starting",
            Decimal(statement),
            Decimal(before),
            Decimal(after),
            new_account,
            already,
            to_upload,
        )

    def test_new_account_already_equal(self):
        check = self.starting("500", "500", "900", {"current": Decimal("500")})
        self.assertEqual(
            balance.balance_check_message(check),
            "New YNAB account: its Starting Balance already equals"
            " the statement starting balance (500.00).",
        )

    def test_new_account_to_be_set(self):
        check = self.starting("1500", "0", "900", {"current": Decimal("0")})
        self.assertEqual(
            balance.balance_check_message(check),
            "New YNAB account: its Starting Balance (0.00) will be set to the statement"
            " starting balance (1,500.00) when you upload.",
        )

    def test_match(self):
        self.assertEqual(
            balance.balance_check_message(self.starting("500", "500", "900")),
            "Starting balance matches YNAB: 500.00",
        )
        ending = balance.compare_balance("ending", Decimal("1249.5"), None, Decimal("1249.5"))
        self.assertEqual(
            balance.balance_check_message(ending), "Ending balance matches YNAB: 1,249.50"
        )

    def test_match_with_transactions_already_in_ynab(self):
        check = self.starting("500", "500", "1000", already=3, to_upload=22)
        self.assertEqual(
            balance.balance_check_message(check),
            "\n".join(
                [
                    "3 transactions already in YNAB. Only 22 will be uploaded.",
                    "Starting balance matches YNAB.",
                    "Statement starting balance: 500.00",
                    "YNAB balance BEFORE upload: 500.00",
                    "YNAB balance AFTER upload: 1,000.00",
                ]
            ),
        )

    def test_starting_mismatch(self):
        check = self.starting("7873.54", "-2598.98", "-5197.96")
        self.assertEqual(
            balance.balance_check_message(check),
            "\n".join(
                [
                    "Starting balance does not match YNAB. You can still import.",
                    "Statement starting balance: 7,873.54",
                    "YNAB balance BEFORE upload: -2,598.98",
                    "YNAB balance AFTER upload: -5,197.96",
                ]
            ),
        )

    def test_ending_mismatch_without_balance_before(self):
        # same shape as ynab_api.client.check_ending_balance(): no already_in_ynab key
        check = {
            "kind": "ending",
            "statement": Decimal("1249.5"),
            "ynab_before": None,
            "ynab_after": Decimal("1300"),
            "difference": Decimal("50.5"),
            "matches": False,
            "new_account": None,
        }
        self.assertEqual(
            balance.balance_check_message(check),
            "\n".join(
                [
                    "Ending balance does not match YNAB."
                    " Make any adjustment in YNAB as you see fit.",
                    "Statement ending balance: 1,249.50",
                    "YNAB balance AFTER upload: 1,300.00",
                ]
            ),
        )

    def test_new_account_message(self):
        self.assertEqual(
            balance.new_account_message({"from": Decimal("500"), "to": Decimal("500")}),
            "New YNAB account: its Starting Balance already equals"
            " the statement starting balance (500.00).",
        )
        self.assertEqual(
            balance.new_account_message({"from": Decimal("0"), "to": Decimal("1500")}),
            "New YNAB account: Starting Balance set from 0.00 to 1,500.00"
            " (the statement starting balance).",
        )


if __name__ == "__main__":
    unittest.main()
