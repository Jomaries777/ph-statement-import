"""
Tests for transform.py: parsed rows -> YNAB transactions.

Run from the project folder:  python -m unittest discover tests

All data here is made up.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from statement_etl import transform
from tests.fakes import gcash_row

PAYMENT = gcash_row("2026-10-20 06:50 AM", "Payment to Test Shop, Merchant Transaction Number: X1",
                    "1000000000002", debit="599.00", balance="4,407.56")
RECEIVED = gcash_row("2026-10-01 09:00 PM", "Received GCash from Test Sender", "1000000000001",
                     credit="1,000.00", balance="5,006.56")


class AmountsTest(unittest.TestCase):
    def test_to_decimal(self):
        self.assertEqual(transform.to_decimal("1,234.50"), Decimal("1234.50"))
        self.assertEqual(transform.to_decimal(""), Decimal(0))

    def test_to_milliunits(self):
        self.assertEqual(transform.to_milliunits(Decimal("-599.00")), -599000)
        self.assertEqual(transform.to_milliunits(Decimal("1234.56")), 1234560)
        self.assertEqual(transform.to_milliunits(Decimal("0.01")), 10)
        self.assertEqual(transform.to_milliunits(Decimal(0)), 0)


class ImportIdTest(unittest.TestCase):
    def test_format(self):
        import_id = transform.make_import_id("gcash", "1000000000002")
        self.assertTrue(import_id.startswith("ETLPH:"))
        self.assertEqual(len(import_id), 36)  # YNAB's limit
        self.assertRegex(import_id[len("ETLPH:"):], r"^[0-9a-f]{30}$")

    def test_stable(self):
        self.assertEqual(
            transform.make_import_id("gcash", "1000000000002"),
            transform.make_import_id("gcash", "1000000000002"),
        )

    def test_differs_by_source_and_id(self):
        ids = {
            transform.make_import_id("gcash", "1000000000002"),
            transform.make_import_id("gcash", "1000000000003"),
            transform.make_import_id("maya", "1000000000002"),
        }
        self.assertEqual(len(ids), 3)


class YnabTransactionTest(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(
            transform.ynab_transaction(datetime(2026, 10, 2, 18, 50), "Test Shop", Decimal("-12.34"), "ID",
                                       memo="note", flag_color="red"),
            {"date": "2026-10-02", "payee_name": "Test Shop", "import_id": "ID", "amount": -12340,
             "memo": "note", "flag_color": "red"},
        )

    def test_defaults_and_plain_date(self):
        t = transform.ynab_transaction(date(2026, 1, 5), "Test", Decimal("1"), "ID")
        self.assertEqual(t["date"], "2026-01-05")
        self.assertIsNone(t["memo"])
        self.assertIsNone(t["flag_color"])


class GCashToYnabTest(unittest.TestCase):
    def test_money_out(self):
        self.assertEqual(transform.gcash_to_ynab(PAYMENT), {
            "date": "2026-10-20",
            "payee_name": "Test Shop",
            "import_id": transform.make_import_id("gcash", "1000000000002"),
            "amount": -599000,
            "memo": "GCash Ref: 1000000000002",
            "flag_color": None,
        })

    def test_money_in(self):
        t = transform.gcash_to_ynab(RECEIVED)
        self.assertEqual(t["amount"], 1000000)
        self.assertEqual(t["payee_name"], "Received GCash from Test Sender")
        self.assertEqual(t["date"], "2026-10-01")

    def test_correction_rows_are_flagged_red(self):
        row = gcash_row("2026-10-02 10:00 AM", "Refund from Test Shop", "R1000000000002", credit="50.00")
        t = transform.gcash_to_ynab(row)
        self.assertEqual(t["flag_color"], "red")
        self.assertEqual(t["memo"], "GCash Ref: R1000000000002")
        self.assertEqual(t["import_id"], transform.make_import_id("gcash", "R1000000000002"))

    def test_import_id_only_depends_on_the_reference(self):
        changed = {**PAYMENT, "Debit": "600.00", "Description": "Payment to Other Shop", "Date and Time": "2026-10-21 07:00 AM"}
        self.assertEqual(transform.gcash_to_ynab(changed)["import_id"], transform.gcash_to_ynab(PAYMENT)["import_id"])


class TransformTest(unittest.TestCase):
    def test_converts_every_row(self):
        self.assertEqual(
            transform.transform([PAYMENT, RECEIVED], "gcash"),
            [transform.gcash_to_ynab(PAYMENT), transform.gcash_to_ynab(RECEIVED)],
        )
        self.assertEqual(transform.transform([], "gcash"), [])

    def test_unknown_source(self):
        with self.assertRaisesRegex(ValueError, r"Unknown source 'nope'. Known sources: gcash"):
            transform.transform([PAYMENT], "nope")


class MainTest(unittest.TestCase):
    def run_main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["transform", *args]), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            transform.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_prints_and_writes_json(self):
        with tempfile.TemporaryDirectory() as folder:
            rows_path = os.path.join(folder, "rows.json")
            out_path = os.path.join(folder, "ynab.json")
            with open(rows_path, "w") as f:
                json.dump([PAYMENT, RECEIVED], f)
            expected = transform.transform([PAYMENT, RECEIVED], "gcash")

            out, _ = self.run_main(rows_path, "--source", "gcash")
            self.assertEqual(json.loads(out), expected)

            out, err = self.run_main(rows_path, "--source", "gcash", "-o", out_path)
            with open(out_path) as f:
                self.assertEqual(json.load(f), expected)
            self.assertEqual(out, "")
            self.assertEqual(err, f"Wrote 2 transactions to {out_path}\n")

    def test_source_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            self.run_main("rows.json")
        self.assertEqual(exit.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
