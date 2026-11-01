"""
Tests for ynab_api/categories.py: which categories can be picked, which
transactions need one, and saving the picks.

Run from the project folder:  python -m unittest discover tests

YNAB is never called: the SDK's API classes are mocked. All data here is made up.
"""

import json
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import ynab

from statement_etl.ynab_api import categories
from tests.fakes import ACCOUNT_ID, PLAN_ID, fake_client, response, ynab_txn

# YNAB category ids are UUIDs, and the SDK checks that they are
GROCERIES_ID = "33333333-3333-3333-3333-333333333333"
TRANSPORT_ID = "44444444-4444-4444-4444-444444444444"


def category(name, **fields):
    return SimpleNamespace(
        **{"id": f"id-{name}", "name": name, "hidden": False, "deleted": False, "internal": False}
        | fields
    )


def group(name, cats, **fields):
    # YNAB really does mark every group internal, even the user's own
    return SimpleNamespace(
        **{"name": name, "hidden": False, "deleted": False, "internal": True, "categories": cats}
        | fields
    )


def uncategorized(**fields):
    """A transaction as get_transactions(type="uncategorized") returns it."""
    defaults = {"account_name": "Test Wallet", "transfer_account_id": None, "subtransactions": []}
    return ynab_txn(**{**defaults, **fields})


class MockedYnabTest(unittest.TestCase):
    """Patches categories.ynab_client and the SDK's CategoriesApi / TransactionsApi."""

    def setUp(self):
        self.api_client = fake_client()
        self.ynab_client = self.patch(categories, "ynab_client", return_value=self.api_client)
        self.categories_api = self.patch(ynab, "CategoriesApi").return_value
        self.transactions_api = self.patch(ynab, "TransactionsApi").return_value

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


class GetCategoriesTest(MockedYnabTest):
    def test_keeps_only_categories_a_transaction_can_be_given(self):
        self.categories_api.get_categories.return_value = response(
            category_groups=[
                group(
                    "Internal Master Category",
                    [
                        category("Inflow: Ready to Assign", internal=True),
                        category("Uncategorized", internal=True),
                    ],
                ),
                group("Credit Card Payments", [category("Test Card")]),
                group(
                    "Everyday",
                    [
                        category("Groceries"),
                        category("Old Hobby", hidden=True),
                        category("Gone", deleted=True),
                    ],
                ),
                group("Hidden Categories", [category("Parked")], hidden=True),
            ]
        )

        result = categories.get_categories(PLAN_ID, "page-token")

        self.assertEqual(
            result,
            [
                {
                    "id": "id-Inflow: Ready to Assign",
                    "name": "Inflow: Ready to Assign",
                    "group": "Internal Master Category",
                },
                {"id": "id-Groceries", "name": "Groceries", "group": "Everyday"},
            ],
        )
        self.ynab_client.assert_called_once_with("page-token")
        self.categories_api.get_categories.assert_called_once_with(PLAN_ID)


class GetUncategorizedTransactionsTest(MockedYnabTest):
    def test_returns_plain_transactions_in_pesos(self):
        self.transactions_api.get_transactions.return_value = response(
            transactions=[
                uncategorized(id="t1", payee_name="Test Shop", memo="Ref 1", amount=-249500),
                uncategorized(id="t2", payee_name=None, memo=None, amount=1000),
            ]
        )

        result = categories.get_uncategorized_transactions(PLAN_ID, since_date=date(2026, 10, 1))

        self.assertEqual(
            result,
            [
                {
                    "id": "t1",
                    "date": "2026-10-01",
                    "account": "Test Wallet",
                    "payee": "Test Shop",
                    "memo": "Ref 1",
                    "amount": Decimal("-249.5"),
                },
                {
                    "id": "t2",
                    "date": "2026-10-01",
                    "account": "Test Wallet",
                    "payee": "",
                    "memo": "",
                    "amount": Decimal("1"),
                },
            ],
        )
        self.transactions_api.get_transactions.assert_called_once_with(
            PLAN_ID, since_date=date(2026, 10, 1), type="uncategorized"
        )

    def test_leaves_out_transfers_splits_and_deleted(self):
        self.transactions_api.get_transactions.return_value = response(
            transactions=[
                uncategorized(id="keep"),
                uncategorized(id="transfer", transfer_account_id="other-account"),
                uncategorized(id="split", subtransactions=[SimpleNamespace(id="part")]),
                uncategorized(id="deleted", deleted=True),
            ]
        )

        result = categories.get_uncategorized_transactions(PLAN_ID)

        self.assertEqual([t["id"] for t in result], ["keep"])

    def test_one_account_only(self):
        self.transactions_api.get_transactions_by_account.return_value = response(transactions=[])

        categories.get_uncategorized_transactions(PLAN_ID, ACCOUNT_ID)

        self.transactions_api.get_transactions_by_account.assert_called_once_with(
            PLAN_ID, ACCOUNT_ID, since_date=None, type="uncategorized"
        )
        self.transactions_api.get_transactions.assert_not_called()


class UncategorizedCreatedTest(unittest.TestCase):
    def test_keeps_only_real_transactions_with_no_category(self):
        created = [
            ynab_txn(id="keep", payee_name="Test Shop", amount=-249500),
            # Some come back with YNAB's own "Uncategorized" category
            ynab_txn(id="keep-too", category_id="c-internal", category_name="Uncategorized"),
            ynab_txn(id="payee-rule", category_id="c1", category_name="Groceries"),
            ynab_txn(id="transfer", transfer_account_id="other-account"),
            ynab_txn(id="split", subtransactions=[SimpleNamespace(id="part")]),
            ynab_txn(id="correction", flag_color=SimpleNamespace(value="red")),
            ynab_txn(id="deleted", deleted=True),
        ]

        result = categories.uncategorized_created(created)

        self.assertEqual([t["id"] for t in result], ["keep", "keep-too"])
        self.assertEqual(
            result[0],
            {
                "id": "keep",
                "date": "2026-10-01",
                "account": "Test Wallet",
                "payee": "Test Shop",
                "memo": "",
                "amount": Decimal("-249.5"),
            },
        )


class SetCategoriesTest(MockedYnabTest):
    def test_sends_only_the_id_and_category(self):
        self.transactions_api.update_transactions.return_value = response(
            transactions=[ynab_txn(id="t1"), ynab_txn(id="t2")]
        )

        updated = categories.set_categories(
            PLAN_ID, {"t1": GROCERIES_ID, "t2": TRANSPORT_ID}, "page-token"
        )

        self.assertEqual(updated, 2)
        plan_id, data = self.transactions_api.update_transactions.call_args.args
        self.assertEqual(plan_id, PLAN_ID)
        # Nothing else, so approval, payee, memo and amount stay as they were
        self.assertEqual(
            json.loads(data.to_json()),
            {
                "transactions": [
                    {"id": "t1", "category_id": GROCERIES_ID},
                    {"id": "t2", "category_id": TRANSPORT_ID},
                ]
            },
        )

    def test_nothing_to_save_makes_no_call(self):
        self.assertEqual(categories.set_categories(PLAN_ID, {}), 0)
        self.ynab_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
