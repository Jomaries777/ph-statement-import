"""
Tests for parsers/bpi.py: reading the BPI savings table from page words, the
year added to each date, wrapped lines, the four totals/balance checks,
to_transactions(), and the CLI.

Run from the project folder:  python -m unittest discover tests

The PDF is faked: pages return word positions like pdfplumber does (see
tests/fakes.py), so no real statement is needed. All data here is made up.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

from statement_etl.parsers import bpi
from statement_etl.parsers.common import StatementError, group_lines
from statement_etl.sources import SOURCES
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import make_import_id, to_ynab
from tests.fakes import FakePage, FakePDF, right_word, word, words_from

# =============================================================================
# BPI TABLE LAYOUT - x positions of each column on a fake BPI page, close to
# a real statement's
# =============================================================================

DATE_X = 36  # "Jul 27"
DESCRIPTION_X = 65  # Description is left-aligned here, left of its label
REF_X = 199  # under the "REF" label (200-215)
DETAILS_X = 225  # Details start right of the Ref code
DEBIT_X1 = 431  # right edge of debit amounts (the "CREDIT" label starts at 445)
CREDIT_X1 = 501
BALANCE_X1 = 574


def bpi_header(top=159.0):
    return (
        [word("DATE", 36, top), word("DESCRIPTION", 97, top), word("REF", 200, top)]
        + [word("DETAILS", 266, top), word("DEBIT", 376, top), word("AMOUNT", 402, top)]
        + [word("CREDIT", 445, top), word("AMOUNT", 477, top), word("BALANCE", 522, top)]
    )


def bpi_line(top, month_day, description, ref="", details="", debit="", credit="", balance=""):
    """One transaction line, laid out like BPI's."""
    words = words_from(month_day, DATE_X, top) + words_from(description, DESCRIPTION_X, top)
    if ref:
        words.append(word(ref, REF_X, top))
    words += words_from(details, DETAILS_X, top)
    if debit:
        words.append(right_word(debit, DEBIT_X1, top))
    if credit:
        words.append(right_word(credit, CREDIT_X1, top))
    return words + [right_word(balance, BALANCE_X1, top)]


def summary_line(top, label, amount, x1=BALANCE_X1):
    return words_from(label, DESCRIPTION_X, top) + [right_word(amount, x1, top)]


def period_line(period="JUL 14, 2026 - OCT 14, 2026", top=132.0):
    return words_from(f"PERIOD COVERED {period}", 36, top) + [word("NO:0000-0000-00", 518, top)]


def statement_pages(total_debit="1,249.50", total_credit="5,000.00", ending="5,250.50"):
    """A made-up two-page statement: the table on page 1, then a page with
    nothing to read (BPI's last page is an image)."""
    page1 = FakePage(
        words_from("SAMPLE SAVINGS ACCOUNT - PHP", 400, 84.0)
        + words_from("Page 1 of 2", 518, 119.0)
        + period_line()
        + bpi_header()
        + summary_line(190.0, "BEGINNING BALANCE", "1,500.00")
        + bpi_line(
            207.0,
            "Jul 27",
            "InstaPay Transfer",
            "",
            "TRANSFER TO OTHER BANK",
            "1,000.00",
            "",
            "500.00",
        )
        + bpi_line(
            215.0,
            "Aug 03",
            "Bills Payment",
            "0010",
            "TO: SAMPLE CARD.....XXXXX12345",
            "249.50",
            "",
            "250.50",
        )
        + bpi_line(
            223.0, "Aug 14", "Sample Payroll", "708", "SAMPLE CO", "", "5,000.00", "5,250.50"
        )
        + summary_line(460.0, "BALANCE THIS STATEMENT", ending)
        + summary_line(477.0, "TOTAL DEBIT", total_debit, DEBIT_X1)
        + summary_line(485.0, "TOTAL CREDIT", total_credit, CREDIT_X1)
        + words_from("# OF CHECKS", DESCRIPTION_X, 492.0)
    )
    return [page1, FakePage([])]


def bpi_row(day, description, ref, details, debit, credit, balance):
    return {
        "Date": day,
        "Description": description,
        "Ref": ref,
        "Details": details,
        "Debit Amount": debit,
        "Credit Amount": credit,
        "Balance": balance,
    }


STATEMENT_ROWS = [
    bpi_row(
        "Jul 27, 2026", "InstaPay Transfer", "", "TRANSFER TO OTHER BANK", "1000.00", "", "500.00"
    ),
    bpi_row(
        "Aug 03, 2026",
        "Bills Payment",
        "0010",
        "TO: SAMPLE CARD.....XXXXX12345",
        "249.50",
        "",
        "250.50",
    ),
    bpi_row("Aug 14, 2026", "Sample Payroll", "708", "SAMPLE CO", "", "5000.00", "5250.50"),
]


# =============================================================================
# PAGE LAYOUT
# =============================================================================


class ReadSummaryTest(unittest.TestCase):
    def test_reads_the_period_balances_and_totals(self):
        summary = bpi.read_summary(group_lines(statement_pages()[0].words))
        self.assertEqual(
            summary,
            {
                "start": date(2026, 7, 14),
                "end": date(2026, 10, 14),
                "beginning": Decimal("1500.00"),
                "ending": Decimal("5250.50"),
                "debit": Decimal("1249.50"),
                "credit": Decimal("5000.00"),
            },
        )

    def test_labels_printed_without_spaces(self):
        # pdfplumber may join BPI's closely printed words: "BEGINNINGBALANCE"
        lines = group_lines(
            words_from("PERIODCOVEREDJUL 14,2026-OCT14,2026", 36, 10.0)
            + summary_line(20.0, "BEGINNINGBALANCE", "1,500.00")
            + summary_line(30.0, "BALANCE THISSTATEMENT", "5,250.50")
        )
        summary = bpi.read_summary(lines)
        self.assertEqual(
            (summary["start"], summary["end"], summary["beginning"], summary["ending"]),
            (date(2026, 7, 14), date(2026, 10, 14), Decimal("1500.00"), Decimal("5250.50")),
        )


class FindColumnsTest(unittest.TestCase):
    def test_finds_the_header(self):
        columns = bpi.find_columns(group_lines(statement_pages()[0].words))
        self.assertEqual(
            columns, {"header_top": 159.0, "ref_x0": 200.0, "ref_x1": 215.0, "credit_x0": 445.0}
        )

    def test_a_page_without_a_table(self):
        self.assertIsNone(bpi.find_columns(group_lines(words_from("Page 2 of 2", 518, 119.0))))


# =============================================================================
# PARSING
# =============================================================================


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages):
        with mock.patch.object(bpi, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = bpi.parse_transactions("bpi.pdf")
        opened.assert_called_once_with("bpi.pdf", "")  # BPI's PDF has no password
        return rows

    def test_reads_every_row_and_column(self):
        self.assertEqual(self.parse(statement_pages()), STATEMENT_ROWS)

    def test_reads_words_with_bpis_smaller_gap(self):
        page = mock.Mock()
        page.extract_words.return_value = []
        bpi.page_lines(page)
        page.extract_words.assert_called_once_with(x_tolerance=bpi.WORD_GAP)

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        with mock.patch.object(bpi, "open_pdf", return_value=FakePDF(statement_pages())):
            bpi.parse_transactions("bpi.pdf", on_page=on_page)
        self.assertEqual(on_page.call_args_list, [mock.call(1, 2), mock.call(2, 2)])

    def test_a_period_across_new_year(self):
        page = FakePage(
            period_line("DEC 14, 2026 - JAN 14, 2027")
            + bpi_header()
            + bpi_line(207.0, "Dec 30", "INTEREST EARNED", credit="1.00", balance="101.00")
            + bpi_line(215.0, "Jan 05", "TAX WITHHELD", debit="0.20", balance="100.80")
        )
        self.assertEqual([r["Date"] for r in self.parse([page])], ["Dec 30, 2026", "Jan 05, 2027"])

    def test_wrapped_text_joins_the_row_above(self):
        page = FakePage(
            period_line()
            + bpi_header()
            + bpi_line(
                207.0, "Jul 27", "Bills Payment", "0010", "TO: SAMPLE", "100.00", "", "900.00"
            )
            + words_from("ELECTRIC CO", DETAILS_X, 215.0)
            + bpi_line(223.0, "Jul 28", "ATM Withdrawal", "291", "", "200.00", "", "700.00")
        )
        first, second = self.parse([page])
        self.assertEqual(first["Details"], "TO: SAMPLE ELECTRIC CO")
        self.assertEqual(second["Details"], "")

    def test_a_row_without_a_ref(self):
        [row] = self.parse(
            [
                FakePage(
                    period_line()
                    + bpi_header()
                    + bpi_line(207.0, "Sep 30", "INTEREST EARNED", credit="2.85", balance="2.85")
                )
            ]
        )
        self.assertEqual(
            (row["Description"], row["Ref"], row["Details"]), ("INTEREST EARNED", "", "")
        )

    def test_no_period_is_an_error(self):
        page = FakePage(
            bpi_header() + bpi_line(207.0, "Jul 27", "Sample", debit="1.00", balance="9.00")
        )
        with self.assertRaisesRegex(StatementError, "PERIOD COVERED"):
            self.parse([page])

    def test_checks(self):
        cases = {
            "total debit": (
                statement_pages(total_debit="1,000.00"),
                "parsed total debit 1,249.50 != statement total 1,000.00",
            ),
            "total credit": (
                statement_pages(total_credit="4,000.00"),
                "parsed total credit 5,000.00 != statement total 4,000.00",
            ),
            "ending balance": (
                statement_pages(ending="5,000.00"),
                "ending balance check failed: beginning 1,500.00 - debits 1,249.50 "
                r"\+ credits 5,000.00 = 5,250.50 != statement ending 5,000.00",
            ),
        }
        for name, (pages, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(StatementError, message):
                self.parse(pages)

    def test_running_balance_check(self):
        rows = [dict(STATEMENT_ROWS[0]), dict(STATEMENT_ROWS[1], Balance="300.00")]
        with self.assertRaisesRegex(
            StatementError,
            "running balance check failed on Aug 03, 2026: expected 250.50, got 300.00",
        ):
            bpi.check_totals(rows, {"beginning": Decimal("1500.00")})


class BalancesAndSummaryTest(unittest.TestCase):
    def test_statement_balances(self):
        self.assertEqual(
            bpi.statement_balances(STATEMENT_ROWS),
            {"starting": Decimal("1500.00"), "ending": Decimal("5250.50")},
        )
        self.assertEqual(bpi.statement_balances([]), {})

    def test_summary(self):
        self.assertEqual(
            bpi.summary(STATEMENT_ROWS),
            "Total debit 1,249.50 · Total credit 5,000.00 · matches statement ✓",
        )


# =============================================================================
# CONVERTING
# =============================================================================


class ToTransactionsTest(unittest.TestCase):
    def test_row_to_transaction_and_to_ynab(self):
        txn = bpi.to_transactions(STATEMENT_ROWS)[1]
        key = "Aug 03, 2026|249.50||250.50|0"
        self.assertEqual(
            txn,
            Transaction(
                source="bpi",
                key=key,
                date=date(2026, 8, 3),
                payee="SAMPLE CARD",
                amount=Decimal("-249.50"),
                memo=txn.memo,
                flagged=False,
            ),
        )
        self.assertRegex(txn.memo, r"^BPI Ref: [0-9a-f]{30}$")
        ynab = to_ynab(txn)
        self.assertEqual(
            (ynab["date"], ynab["amount"], ynab["import_id"]),
            ("2026-08-03", -249500, make_import_id("bpi", key)),
        )

    def test_signs_credit_positive_debit_negative(self):
        amounts = [t.amount for t in bpi.to_transactions(STATEMENT_ROWS)]
        self.assertEqual(amounts, [Decimal("-1000.00"), Decimal("-249.50"), Decimal("5000.00")])

    def test_payee_rules(self):
        cases = {
            ("Bills Payment", "TO: SAMPLE CARD.....XXXXX12345"): "SAMPLE CARD",
            ("Bills Payment", "TO: SAMPLE-CO .....XXXXX12345"): "SAMPLE-CO",
            ("3448 EOL/MOBILE BNKG PAYMENT", "TRANSFER TO OTHER BANK"): "EOL/MOBILE BNKG PAYMENT",
            ("InstaPay Transfer", "TRANSFER TO OTHER BANK"): "InstaPay Transfer",
            ("ATM Withdrawal-SAMPLE", ""): "ATM Withdrawal-SAMPLE",
        }
        for (description, details), payee in cases.items():
            with self.subTest(description):
                row = bpi_row("Jul 27, 2026", description, "", details, "1.00", "", "0.00")
                self.assertEqual(bpi.bpi_payee(row), payee)

    def test_same_looking_rows_get_different_stable_keys(self):
        row = STATEMENT_ROWS[0]
        first, second = bpi.to_transactions([row, row])
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(bpi.to_transactions([row, row]), [first, second])


class SourceTest(unittest.TestCase):
    def test_upload_only_no_auto_import(self):
        # BPI doesn't email statements, so /auto-import doesn't list it
        source = SOURCES["bpi"]
        self.assertIsNone(source.email_query)
        self.assertEqual(source.columns, bpi.COLUMNS)


# =============================================================================
# CLI
# =============================================================================


class MainTest(unittest.TestCase):
    def test_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "bpi.json")
            stderr = io.StringIO()
            with (
                mock.patch.object(bpi, "open_pdf", return_value=FakePDF(statement_pages())),
                mock.patch("sys.argv", ["bpi", "bpi.pdf", "-o", out]),
                mock.patch.dict(os.environ, {"BPI_PDF_PASSWORD": ""}),
                contextlib.redirect_stderr(stderr),
            ):
                bpi.main()
            with open(out) as f:
                self.assertEqual(json.load(f), STATEMENT_ROWS)
        self.assertIn("Wrote 3 transactions", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
