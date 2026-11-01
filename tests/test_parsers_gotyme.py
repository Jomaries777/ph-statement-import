"""
Tests for parsers/gotyme.py: reading the GoTyme Everyday Deposit table from
page words, wrapped Details lines, the four totals/balance checks, and the CLI.

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

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from statement_etl.parsers import gotyme
from statement_etl.parsers.common import StatementError, group_lines
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import make_import_id, to_ynab
from tests.fakes import FakePage, FakePDF, right_word, word, words_from

# =============================================================================
# GOTYME TABLE LAYOUT - x positions of each column on a fake GoTyme page
# =============================================================================

DATE_X = 18.8
DETAILS_X = 71.2
CREDITS_X1 = 352.6
DEBITS_X1 = 388.2
BALANCE_X1 = 440.3


def gotyme_header(top=205.4):
    """The table header of a GoTyme page ("Running" sits ~4 pt above the rest)."""
    return (
        [right_word("Running", BALANCE_X1, top - 3.9)]
        + [
            word("Date", DATE_X, top),
            word("Details", DETAILS_X, top),
            right_word("Credits", CREDITS_X1, top),
            right_word("Debits", DEBITS_X1, top),
        ]
        + [right_word("Balance", BALANCE_X1, top + 3.9)]
    )


def gotyme_summary_box(
    period="01 Oct 2026 - 31 Oct 2026",
    opening="5,000.00",
    total_credit="1,000.00",
    total_debit="349.00",
    closing="5,651.00",
):
    """The summary box on page 1 of a GoTyme statement."""
    return (
        words_from("Summary", 273.8, 126.0)
        + words_from(f"Opening balance PHP {opening}", 273.8, 136.0)
        + words_from(f"Total Credit PHP {total_credit}", 297.8, 146.0)
        + words_from(f"Total Debit PHP {total_debit}", 297.8, 156.0)
        + words_from(f"Closing balance PHP {closing}", 273.8, 166.0)
        + words_from(f"Period: {period}", 15.0, 173.0)
    )


def gotyme_line(top, date_str, details="", credits="0.00", debits="0.00", balance="0.00"):
    """One GoTyme transaction anchor line (date, optional inline details, 3 amounts)."""
    day, month, year = date_str.split()
    words = [word(day, DATE_X, top), word(month, 28.0, top), word(year, 40.0, top)]
    if details:
        words += words_from(details, DETAILS_X, top)
    words += [
        right_word(credits, CREDITS_X1, top),
        right_word(debits, DEBITS_X1, top),
        right_word(balance, BALANCE_X1, top),
    ]
    return words


def gotyme_wrapped_line(top, date_str, line_above, line_below, credits, debits, balance):
    """A wrapped Details row: first line ~4 pt above the amounts, second ~4 pt below."""
    return (
        words_from(line_above, DETAILS_X, top - 3.9)
        + gotyme_line(top, date_str, "", credits=credits, debits=debits, balance=balance)
        + words_from(line_below, DETAILS_X, top + 3.9)
    )


def gotyme_footer(page_num=1, total_pages=2):
    """Footer lines printed below the transaction table on each page."""
    return (
        words_from("All figures are in PHP.", 15.0, 547.0)
        + words_from("Document ID: DOC0000001", 15.0, 559.5)
        + words_from("Request Date: 31 Oct 2026", 15.0, 566.3)
        + words_from(f"Page {page_num} of {total_pages}", 415.5, 566.7)
    )


def gotyme_row(date_str, details, credits="0.00", debits="0.00", balance="0.00"):
    """A parsed GoTyme row dict, as gotyme.parse_transactions() returns it."""
    return {
        "Date": date_str,
        "Details": details,
        "Credits": credits,
        "Debits": debits,
        "Running Balance": balance,
    }


def statement_pages(
    opening="5,000.00",
    total_credit="1,000.00",
    total_debit="349.00",
    closing="5,651.00",
    last_balance="5,651.00",
):
    """Fake 3-page GoTyme PDF: page 1 summary + rows, page 2 repeated header, page 3 no table."""
    page1 = FakePage(
        words_from("22nd Floor, GBF Center 1, Bridgetowne", 305.4, 21.2)
        + words_from("Statement of Account", 15.0, 77.3)
        + gotyme_summary_box(
            opening=opening,
            total_credit=total_credit,
            total_debit=total_debit,
            closing=closing,
        )
        + gotyme_header(205.4)
        + gotyme_line(
            226.9,
            "05 Oct 2026",
            "Card payment at SHOPEE PH, Card •••• 1234",
            credits="0.00",
            debits="249.00",
            balance="4,751.00",
        )
        + gotyme_wrapped_line(
            250.0,
            "06 Oct 2026",
            "Transfer from JUAN DELA CRUZ BPI, Account No.",
            "•••••••1234",
            credits="1,000.00",
            debits="0.00",
            balance="5,751.00",
        )
        + gotyme_footer(1, 3)
    )
    page2 = FakePage(
        words_from("Statement of Account", 15.0, 77.3)
        + gotyme_header(111.5)
        + gotyme_line(
            132.3,
            "07 Oct 2026",
            "Qr Payment",
            credits="0.00",
            debits="100.00",
            balance=last_balance,
        )
        + gotyme_footer(2, 3)
    )
    # Page 3 has only legal/contact notes and no transaction table
    page3 = FakePage(
        words_from("Statement of Account", 15.0, 77.3)
        + words_from("Got questions about your statement?", 15.0, 132.9)
        + gotyme_footer(3, 3)
    )
    return [page1, page2, page3]


# =============================================================================
# PAGE LAYOUT & SUMMARY
# =============================================================================


class ReadSummaryTest(unittest.TestCase):
    def test_reads_summary_box_fields(self):
        page = FakePage(gotyme_summary_box())
        self.assertEqual(
            gotyme._read_summary(page),
            {
                "period": "01 Oct 2026 - 31 Oct 2026",
                "opening": Decimal("5000.00"),
                "credit": Decimal("1000.00"),
                "debit": Decimal("349.00"),
                "closing": Decimal("5651.00"),
            },
        )


class FindColumnsTest(unittest.TestCase):
    def test_header_with_two_line_running_balance(self):
        page = FakePage(words_from("Statement of Account", 15.0, 77.3) + gotyme_header(205.4))
        cols = gotyme._find_columns(page)
        self.assertIsNotNone(cols)
        self.assertAlmostEqual(cols["header_top"], 209.3)
        self.assertAlmostEqual(cols["date_x0"], DATE_X)
        self.assertAlmostEqual(cols["details_x0"], DETAILS_X)
        self.assertAlmostEqual(cols["credits_x1"], CREDITS_X1)
        self.assertAlmostEqual(cols["debits_x1"], DEBITS_X1)
        self.assertAlmostEqual(cols["balance_x1"], BALANCE_X1)

    def test_no_header_returns_none(self):
        page = FakePage(words_from("Got questions about your statement?", 15.0, 132.9))
        self.assertIsNone(gotyme._find_columns(page))
        self.assertIsNone(gotyme._find_columns([]))

    def test_missing_balance_label_returns_none(self):
        words = [w for w in gotyme_header(205.4) if w["text"] != "Balance"]
        self.assertIsNone(gotyme._find_columns(group_lines(words)))


# =============================================================================
# PARSING & CHECKS
# =============================================================================


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages):
        with mock.patch.object(gotyme, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = gotyme.parse_transactions("gotyme.pdf", "01311990")
        opened.assert_called_once_with("gotyme.pdf", "01311990")
        return rows

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        with mock.patch.object(gotyme, "open_pdf", return_value=FakePDF(statement_pages())):
            gotyme.parse_transactions("gotyme.pdf", "01311990", on_page=on_page)
        pages = len(statement_pages())
        self.assertEqual(on_page.call_args_list, [mock.call(n, pages) for n in range(1, pages + 1)])

    def test_one_line_wrapped_and_multi_page_statement(self):
        rows = self.parse(statement_pages())
        self.assertEqual(
            rows,
            [
                gotyme_row(
                    "05 Oct 2026",
                    "Card payment at SHOPEE PH, Card •••• 1234",
                    credits="0.00",
                    debits="249.00",
                    balance="4751.00",
                ),
                gotyme_row(
                    "06 Oct 2026",
                    "Transfer from JUAN DELA CRUZ BPI, Account No. •••••••1234",
                    credits="1000.00",
                    debits="0.00",
                    balance="5751.00",
                ),
                gotyme_row(
                    "07 Oct 2026",
                    "Qr Payment",
                    credits="0.00",
                    debits="100.00",
                    balance="5651.00",
                ),
            ],
        )
        for row in rows:
            self.assertEqual(tuple(row), gotyme.COLUMNS)

    def test_page_with_no_table_is_ignored(self):
        no_table = FakePage(words_from("Got questions about your statement?", 15.0, 132.9))
        self.assertEqual(self.parse([no_table]), [])

    def test_total_credit_check_fails(self):
        with self.assertRaisesRegex(
            StatementError, r"parsed total credit 1000\.00 != statement total 900\.00"
        ):
            self.parse(statement_pages(total_credit="900.00"))

    def test_total_debit_check_fails(self):
        with self.assertRaisesRegex(
            StatementError, r"parsed total debit 349\.00 != statement total 999\.00"
        ):
            self.parse(statement_pages(total_debit="999.00"))

    def test_closing_balance_check_fails(self):
        with self.assertRaisesRegex(
            StatementError,
            r"closing balance check failed: opening 5000\.00 \+ credits 1000\.00 "
            r"- debits 349\.00 = 5651\.00 != statement closing 9999\.00",
        ):
            self.parse(statement_pages(closing="9,999.00"))

    def test_running_balance_check_fails(self):
        with self.assertRaisesRegex(
            StatementError,
            r"running balance check failed on 07 Oct 2026: expected 5651\.00, got 5600\.00",
        ):
            self.parse(statement_pages(last_balance="5,600.00"))

    def test_wrong_password_raises_statement_error(self):
        wrong_pw = PdfminerException(PDFPasswordIncorrect("wrong password"))
        with (
            mock.patch("statement_etl.parsers.common.pdfplumber.open", side_effect=wrong_pw),
            self.assertRaisesRegex(StatementError, "Incorrect PDF password"),
        ):
            gotyme.parse_transactions("gotyme.pdf", "00000000")


# =============================================================================
# BALANCES & SUMMARY
# =============================================================================

MADE_UP_ROWS = [
    gotyme_row(
        "05 Oct 2026",
        "Card payment at SHOPEE PH, Card •••• 1234",
        credits="0.00",
        debits="249.00",
        balance="4751.00",
    ),
    gotyme_row(
        "06 Oct 2026",
        "Transfer from JUAN DELA CRUZ BPI, Account No. •••••••1234",
        credits="1000.00",
        debits="0.00",
        balance="5751.00",
    ),
]


class BalancesAndSummaryTest(unittest.TestCase):
    def test_statement_balances(self):
        self.assertEqual(gotyme.statement_balances([]), {})
        self.assertEqual(
            gotyme.statement_balances(MADE_UP_ROWS),
            {"starting": Decimal("5000.00"), "ending": Decimal("5751.00")},
        )

    def test_summary(self):
        self.assertEqual(
            gotyme.summary(MADE_UP_ROWS),
            "Total credit 1,000.00 · Total debit 249.00 · matches statement ✓",
        )


# =============================================================================
# CONVERTING - rows -> neutral Transactions
# =============================================================================


class ToTransactionsTest(unittest.TestCase):
    def test_debit_and_credit_rows(self):
        txns = gotyme.to_transactions(MADE_UP_ROWS)
        self.assertEqual(
            txns,
            [
                Transaction(
                    source="gotyme",
                    key="05 Oct 2026|0.00|249.00|4751.00|0",
                    date=date(2026, 10, 5),
                    payee="SHOPEE PH",
                    amount=Decimal("-249.00"),
                    memo="GoTyme: Card payment at SHOPEE PH, Card •••• 1234",
                    flagged=False,
                ),
                Transaction(
                    source="gotyme",
                    key="06 Oct 2026|1000.00|0.00|5751.00|0",
                    date=date(2026, 10, 6),
                    payee="Transfer from JUAN DELA CRUZ BPI",
                    amount=Decimal("1000.00"),
                    memo="GoTyme: Transfer from JUAN DELA CRUZ BPI, Account No. •••••••1234",
                    flagged=False,
                ),
            ],
        )

    def test_to_ynab(self):
        [txn] = gotyme.to_transactions([MADE_UP_ROWS[0]])
        self.assertEqual(
            to_ynab(txn),
            {
                "date": "2026-10-05",
                "payee_name": "SHOPEE PH",
                "import_id": make_import_id("gotyme", "05 Oct 2026|0.00|249.00|4751.00|0"),
                "amount": -249000,
                "memo": "GoTyme: Card payment at SHOPEE PH, Card •••• 1234",
                "flag_color": None,
            },
        )

    def test_identical_rows_increment_counter(self):
        first, second = gotyme.to_transactions([MADE_UP_ROWS[0], dict(MADE_UP_ROWS[0])])
        self.assertEqual(first.key, "05 Oct 2026|0.00|249.00|4751.00|0")
        self.assertEqual(second.key, "05 Oct 2026|0.00|249.00|4751.00|1")


# =============================================================================
# COMMAND LINE
# =============================================================================


class MainTest(unittest.TestCase):
    def run_main(self, *args, rows=MADE_UP_ROWS, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env or {}, clear=True),
            mock.patch("sys.argv", ["gotyme", "gotyme.pdf", *args]),
            mock.patch.object(gotyme, "parse_transactions", return_value=rows) as parse,
            mock.patch.object(gotyme.getpass, "getpass", return_value="typed") as prompt,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            gotyme.main()
        return parse, prompt, stdout.getvalue(), stderr.getvalue()

    def test_prints_json_with_password_flag(self):
        parse, prompt, out, _ = self.run_main("-p", "01311990")
        parse.assert_called_once_with("gotyme.pdf", "01311990")
        prompt.assert_not_called()
        self.assertEqual(json.loads(out), MADE_UP_ROWS)

    def test_reads_password_from_env(self):
        parse, prompt, _, _ = self.run_main(env={"GOTYME_PDF_PASSWORD": "01311990"})
        parse.assert_called_once_with("gotyme.pdf", "01311990")
        prompt.assert_not_called()

    def test_prompts_for_password_when_not_set(self):
        parse, prompt, _, _ = self.run_main()
        prompt.assert_called_once()
        parse.assert_called_once_with("gotyme.pdf", "typed")

    def test_writes_output_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "rows.json")
            _, _, out, err = self.run_main("-p", "", "-o", path)
            with open(path) as f:
                self.assertEqual(json.load(f), MADE_UP_ROWS)
        self.assertEqual(out, "")
        self.assertEqual(err, f"Wrote 2 transactions to {path}\n")


# =============================================================================
# PDF PASSWORD - date of birth as MMDDYYYY, for Gmail auto-import
# =============================================================================


class BuildPasswordTest(unittest.TestCase):
    def test_date_of_birth_as_mmddyyyy(self):
        self.assertEqual(gotyme.build_password({"birth_date": "1990-01-31"}), "01311990")
        self.assertEqual(gotyme.build_password({"birth_date": "2001-12-05"}), "12052001")

    def test_a_date_that_does_not_exist_raises_value_error(self):
        # /auto-import shows these as "Invalid password input" instead of saving them
        for bad in ("1990-02-30", "1990-13-01", "not a date", ""):
            with self.subTest(birth_date=bad), self.assertRaises(ValueError):
                gotyme.build_password({"birth_date": bad})


if __name__ == "__main__":
    unittest.main()
