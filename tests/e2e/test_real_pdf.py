"""
A generated statement PDF (make_pdf.py) through real pdfplumber and
parsers/gcash.py, with nothing mocked: catches layout and word-position
regressions that the fake-word tests can't.
"""

import os
import tempfile
import unittest
from decimal import Decimal

import tests  # noqa: F401 - the safety net: nothing reaches YNAB or the keyring
from statement_etl.parsers import gcash
from tests.e2e import make_pdf

COLUMNS = list(gcash.COLUMNS)


class RealPdfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.balanced_pdf = os.path.join(cls.tmp.name, "balanced.pdf")
        cls.gap_pdf = os.path.join(cls.tmp.name, "with_gap.pdf")
        make_pdf.balanced(cls.balanced_pdf)
        make_pdf.with_gap(cls.gap_pdf)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_balanced_rows_match_the_statement(self):
        rows = gcash.parse_transactions(self.balanced_pdf, "any-password")
        expected = [
            {
                "Date and Time": r.date_time,
                "Description": r.description.replace("\n", " "),
                "Reference No.": r.reference,
                "Debit": r.debit,
                "Credit": r.credit,
                "Balance": r.balance,
            }
            for r in make_pdf.BALANCED_ROWS
        ]
        self.assertEqual(rows, expected)
        self.assertEqual(list(rows[0]), COLUMNS)

    def test_wrapped_description_is_joined(self):
        rows = gcash.parse_transactions(self.balanced_pdf)
        self.assertEqual(
            rows[1]["Description"],
            "Payment to Sample Coffee Shop, Merchant Transaction Number: TEST0000001",
        )
        self.assertEqual(gcash.gcash_payee(rows[1]["Description"]), "Sample Coffee Shop")

    def test_balanced_totals_and_balances(self):
        rows = gcash.parse_transactions(self.balanced_pdf)
        self.assertEqual(
            gcash.summary(rows),
            "Total debit 500.00 · Total credit 750.00 · matches statement ✓",
        )
        self.assertEqual(
            gcash.statement_balances(rows),
            {"starting": Decimal("1000.00"), "ending": Decimal("1250.00")},
        )
        self.assertIsNone(gcash.balance_warning(rows))
        self.assertEqual(gcash.balance_corrections(rows), [])

    def test_gap_adds_correction_rows(self):
        rows = gcash.parse_transactions(self.gap_pdf)
        corrections = gcash.balance_corrections(rows)
        self.assertEqual(
            [(c["description"], c["amount"]) for c in corrections],
            [
                ("STARTING BALANCE CORRECTION", Decimal("100.00")),
                ("Refund from Sample Coffee Shop", Decimal("50.00")),
                ("MISSING TRANSACTIONS IN GCASH STATEMENT", Decimal("-50.00")),
            ],
        )
        self.assertEqual(corrections[0]["reference"], "S9990000000011")
        self.assertEqual(corrections[1]["reference"], "R9990000000013")
        self.assertRegex(corrections[2]["reference"], r"^M0000\d{9}$")

        # Correction rows sit next to the statement row they belong to
        self.assertEqual(
            [r["Reference No."] for r in rows][:5],
            ["S9990000000011", "9990000000011", "9990000000012", "R9990000000013", "9990000000013"],
        )
        self.assertEqual(sum(not gcash.is_correction_row(r) for r in rows), 4)

    def test_gap_totals_and_balances(self):
        rows = gcash.parse_transactions(self.gap_pdf)
        # Totals count the statement's own rows only
        self.assertEqual(
            gcash.summary(rows),
            "Total debit 300.00 · Total credit 600.00 · matches statement ✓",
        )
        # The correction rows make the running balance add up again
        self.assertEqual(
            gcash.statement_balances(rows),
            {"starting": Decimal("1000.00"), "ending": Decimal("1400.00")},
        )
        self.assertIn("3 correction row(s) added, total +100.00", gcash.balance_warning(rows))

    def test_wrong_total_stops_parsing(self):
        # A row the parser can't read (no reference number) makes the totals differ
        path = os.path.join(self.tmp.name, "unreadable_row.pdf")
        rows = list(make_pdf.BALANCED_ROWS)
        rows[2] = rows[2]._replace(reference="")
        make_pdf.make_statement(path, rows, make_pdf.BALANCED_STARTING, make_pdf.BALANCED_ENDING)
        with self.assertRaisesRegex(
            ValueError, "parsed total credit 500.00 != statement total 750.00"
        ):
            gcash.parse_transactions(path)


if __name__ == "__main__":
    unittest.main()
