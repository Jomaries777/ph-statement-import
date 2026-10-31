"""
Tests for ynab_api/format.py: neutral Transactions -> YNAB transaction dicts.

Run from the project folder:  python -m unittest discover tests

All data here is made up.
"""

import unittest
from datetime import date, datetime
from decimal import Decimal

from statement_etl.transaction import Transaction
from statement_etl.ynab_api import format as ynab_format


class AmountsTest(unittest.TestCase):
    def test_to_milliunits(self):
        self.assertEqual(ynab_format.to_milliunits(Decimal("-249.00")), -249000)
        self.assertEqual(ynab_format.to_milliunits(Decimal("1234.56")), 1234560)
        self.assertEqual(ynab_format.to_milliunits(Decimal("0.01")), 10)
        self.assertEqual(ynab_format.to_milliunits(Decimal(0)), 0)


class ImportIdTest(unittest.TestCase):
    def test_format(self):
        import_id = ynab_format.make_import_id("gcash", "1000000000002")
        self.assertTrue(import_id.startswith("ETLPH:"))
        self.assertEqual(len(import_id), 36)  # YNAB's limit
        self.assertRegex(import_id[len("ETLPH:") :], r"^[0-9a-f]{30}$")

    def test_stable(self):
        self.assertEqual(
            ynab_format.make_import_id("gcash", "1000000000002"),
            ynab_format.make_import_id("gcash", "1000000000002"),
        )

    def test_pinned_value(self):
        # Users' YNAB accounts already hold ids made this way. If this changes,
        # re-importing a statement would duplicate every transaction.
        self.assertEqual(
            ynab_format.make_import_id("gcash", "1000000000002"),
            "ETLPH:707f7b0d95f2bb43995f608f704d67",
        )

    def test_differs_by_source_and_id(self):
        ids = {
            ynab_format.make_import_id("gcash", "1000000000002"),
            ynab_format.make_import_id("gcash", "1000000000003"),
            ynab_format.make_import_id("maya", "1000000000002"),
        }
        self.assertEqual(len(ids), 3)


class YnabTransactionTest(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(
            ynab_format.ynab_transaction(
                datetime(2026, 10, 2, 18, 50),
                "Test Shop",
                Decimal("-12.34"),
                "ID",
                memo="note",
                flag_color="red",
            ),
            {
                "date": "2026-10-02",
                "payee_name": "Test Shop",
                "import_id": "ID",
                "amount": -12340,
                "memo": "note",
                "flag_color": "red",
            },
        )

    def test_defaults_and_plain_date(self):
        t = ynab_format.ynab_transaction(date(2026, 1, 5), "Test", Decimal("1"), "ID")
        self.assertEqual(t["date"], "2026-01-05")
        self.assertIsNone(t["memo"])
        self.assertIsNone(t["flag_color"])


class ToYnabTest(unittest.TestCase):
    def test_shape(self):
        t = Transaction(
            source="gcash",
            key="1000000000002",
            date=datetime(2026, 10, 5, 8, 15),
            payee="Test Shop",
            amount=Decimal("-249.00"),
            memo="GCash Ref: 1000000000002",
        )
        self.assertEqual(
            ynab_format.to_ynab(t),
            {
                "date": "2026-10-05",
                "payee_name": "Test Shop",
                "import_id": "ETLPH:707f7b0d95f2bb43995f608f704d67",
                "amount": -249000,
                "memo": "GCash Ref: 1000000000002",
                "flag_color": None,
            },
        )

    def test_flagged_is_red(self):
        t = Transaction("gcash", "R1", date(2026, 10, 2), "Test", Decimal("50"), flagged=True)
        self.assertEqual(ynab_format.to_ynab(t)["flag_color"], "red")
        self.assertIsNone(ynab_format.to_ynab(t)["memo"])

    def test_import_id_comes_from_source_and_key(self):
        t = Transaction("maya", "K1", date(2026, 10, 2), "Test", Decimal("1"))
        self.assertEqual(
            ynab_format.to_ynab(t)["import_id"], ynab_format.make_import_id("maya", "K1")
        )


if __name__ == "__main__":
    unittest.main()
