"""
Tests for convert.py: parsed rows -> neutral Transactions, via the source's
own converter, and the CLI that prints them as YNAB JSON.

Run from the project folder:  python -m unittest discover tests

All data here is made up.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from statement_etl import convert
from statement_etl.parsers import gcash
from statement_etl.ynab_api.format import make_import_id
from tests.fakes import gcash_row

PAYMENT = gcash_row(
    "2026-10-05 08:15 AM",
    "Payment to Test Shop, Merchant Transaction Number: X1",
    "1000000000002",
    debit="249.00",
    balance="1,750.25",
)
RECEIVED = gcash_row(
    "2026-10-01 09:00 PM",
    "Received GCash from Test Sender",
    "1000000000001",
    credit="1,000.00",
    balance="1,999.25",
)


class RowsToTransactionsTest(unittest.TestCase):
    def test_uses_the_sources_converter(self):
        self.assertEqual(
            convert.rows_to_transactions([PAYMENT, RECEIVED], "gcash"),
            gcash.to_transactions([PAYMENT, RECEIVED]),
        )
        self.assertEqual(convert.rows_to_transactions([], "gcash"), [])

    def test_unknown_source(self):
        with self.assertRaisesRegex(ValueError, r"Unknown source 'nope'. Known sources: gcash"):
            convert.rows_to_transactions([PAYMENT], "nope")


class MainTest(unittest.TestCase):
    def run_main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.argv", ["convert", *args]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            convert.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_prints_and_writes_ynab_json(self):
        # The same YNAB JSON the old transform CLI printed, import_ids included
        expected = [
            {
                "date": "2026-10-05",
                "payee_name": "Test Shop",
                "import_id": make_import_id("gcash", "1000000000002"),
                "amount": -249000,
                "memo": "GCash Ref: 1000000000002",
                "flag_color": None,
            },
            {
                "date": "2026-10-01",
                "payee_name": "Received GCash from Test Sender",
                "import_id": make_import_id("gcash", "1000000000001"),
                "amount": 1000000,
                "memo": "GCash Ref: 1000000000001",
                "flag_color": None,
            },
        ]
        with tempfile.TemporaryDirectory() as folder:
            rows_path = os.path.join(folder, "rows.json")
            out_path = os.path.join(folder, "ynab.json")
            with open(rows_path, "w") as f:
                json.dump([PAYMENT, RECEIVED], f)

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
