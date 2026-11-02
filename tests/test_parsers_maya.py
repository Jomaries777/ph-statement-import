"""
Tests for parsers/maya.py: reading the Maya Wallet table's two-line rows from
page words, the account summary, the four totals/balance checks,
to_transactions(), the password rule, and the CLI.

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

from statement_etl.parsers import maya
from statement_etl.parsers.common import StatementError, group_lines
from statement_etl.sources import SOURCES
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import make_import_id, to_ynab
from tests.fakes import FakePage, FakePDF, right_word, word, words_from

# =============================================================================
# MAYA TABLE LAYOUT - x positions of each column on a fake Maya Wallet page,
# close to a real statement's
# =============================================================================

DATE_X = 29  # "6 Sep 2026", and the time under it
TYPE_X = 108  # the type, and the details under it
REFERENCE_X = 298
AMOUNT_X1 = 465  # right edge of "-PHP 500.00"
BALANCE_X1 = 550


def amount_words(amount, x1, top):
    """ "-500.00" -> the words "-PHP" "500.00", ending at x1, as Maya prints it."""
    sign, number = ("-", amount[1:]) if amount.startswith("-") else ("", amount)
    number_word = right_word(number, x1, top)
    return [right_word(f"{sign}PHP", number_word["x0"] - 2, top), number_word]


def maya_row(top, day, kind, reference, amount, balance, time="09:15:30 AM", details=""):
    """A row's two lines: the date line, and the time + details line under it."""
    return (
        words_from(day, DATE_X, top)
        + words_from(kind, TYPE_X, top)
        + [word(reference, REFERENCE_X, top)]
        + amount_words(amount, AMOUNT_X1, top)
        + amount_words(balance, BALANCE_X1, top)
        + words_from(time, DATE_X, top + 15)
        + words_from(details, TYPE_X, top + 15)
    )


def summary_lines(starting="2,000.00", incoming="1,000.00", outgoing="3,000.00", ending="0.00"):
    labels = words_from(
        "Starting balance + Total incoming - Total outgoing Ending balance", 42, 200
    )
    amounts = []
    for x0, amount in zip((53, 169, 309, 470), (starting, incoming, outgoing, ending), strict=True):
        amounts += words_from(f"PHP {amount}", x0, 235)
    return labels + amounts


def statement_page(**summary):
    """A made-up one-page statement with three rows."""
    return FakePage(
        words_from("1 of 1", 526, 71)
        + words_from("SAMPLE NAME Account number: +630000000000", 20, 113)
        + summary_lines(**summary)
        + words_from(
            "Date & Time Transaction type & details Reference ID Amount Running balance", 43, 305
        )
        + words_from("Beginning Balance", 109, 335)
        + amount_words("2,000.00", 552, 335)
        + maya_row(
            355,
            "6 Sep 2026",
            "Purchased",
            "0A1B2C3D4E5F",
            "-500.00",
            "1,500.00",
            details="SAMPLE SHOP",
        )
        + maya_row(
            410,
            "10 Sep 2026",
            "Received from bank",
            "1A2B3C4D5E6F",
            "1,000.00",
            "2,500.00",
            time="01:20:45 PM",
            details="Credit via InstaPay",
        )
        + maya_row(
            465,
            "20 Sep 2026",
            "Paid bill",
            "2A3B4C5D6E7F",
            "-2,500.00",
            "0.00",
            time="08:05:10 PM",
            details="Pay with PayMaya Wallet Bills Pay",
        )
        + words_from("Ending Balance", 109, 627)
        + amount_words("0.00", 544, 631)
        + words_from("** Nothing follows **", 239, 650)
    )


def maya_row_dict(date_time, kind, details, reference, amount, balance):
    return {
        "Date & Time": date_time,
        "Transaction type": kind,
        "Details": details,
        "Reference ID": reference,
        "Amount": amount,
        "Running balance": balance,
    }


STATEMENT_ROWS = [
    maya_row_dict(
        "6 Sep 2026 09:15:30 AM", "Purchased", "SAMPLE SHOP", "0A1B2C3D4E5F", "-500.00", "1500.00"
    ),
    maya_row_dict(
        "10 Sep 2026 01:20:45 PM",
        "Received from bank",
        "Credit via InstaPay",
        "1A2B3C4D5E6F",
        "1000.00",
        "2500.00",
    ),
    maya_row_dict(
        "20 Sep 2026 08:05:10 PM",
        "Paid bill",
        "Pay with PayMaya Wallet Bills Pay",
        "2A3B4C5D6E7F",
        "-2500.00",
        "0.00",
    ),
]


# =============================================================================
# PAGE LAYOUT
# =============================================================================


class ReadSummaryTest(unittest.TestCase):
    def test_reads_the_four_amounts_under_their_labels(self):
        self.assertEqual(
            maya.read_summary(group_lines(statement_page().words)),
            {
                "starting": Decimal("2000.00"),
                "incoming": Decimal("1000.00"),
                "outgoing": Decimal("3000.00"),
                "ending": Decimal("0.00"),
            },
        )

    def test_a_page_without_the_summary(self):
        self.assertEqual(maya.read_summary(group_lines(words_from("2 of 2", 526, 71))), {})


# =============================================================================
# PARSING
# =============================================================================


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages, password="secret"):
        with mock.patch.object(maya, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = maya.parse_transactions("maya.pdf", password)
        opened.assert_called_once_with("maya.pdf", password)
        return rows

    def test_reads_every_row_and_column(self):
        self.assertEqual(self.parse([statement_page()]), STATEMENT_ROWS)

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        with mock.patch.object(maya, "open_pdf", return_value=FakePDF([statement_page()])):
            maya.parse_transactions("maya.pdf", "secret", on_page=on_page)
        self.assertEqual(on_page.call_args_list, [mock.call(1, 1)])

    def test_details_wrapped_onto_another_line(self):
        page = FakePage(
            maya_row(
                355, "6 Sep 2026", "Purchased", "0A1B2C3D4E5F", "-1.00", "9.00", details="SAMPLE"
            )
            + words_from("SHOP BRANCH", TYPE_X, 382)
        )
        [row] = self.parse([page])
        self.assertEqual(row["Details"], "SAMPLE SHOP BRANCH")

    def test_a_row_without_details(self):
        page = FakePage(maya_row(355, "6 Sep 2026", "Cash in", "0A1B2C3D4E5F", "10.00", "10.00"))
        [row] = self.parse([page])
        self.assertEqual((row["Date & Time"], row["Details"]), ("6 Sep 2026 09:15:30 AM", ""))

    def test_rows_on_a_second_page(self):
        second = FakePage(
            words_from("2 of 2", 526, 71)
            + words_from(
                "Date & Time Transaction type & details Reference ID Amount Running balance",
                43,
                100,
            )
            + maya_row(
                130, "21 Sep 2026", "Cash in", "3A4B5C6D7E8F", "5.00", "5.00", details="SAMPLE"
            )
            + words_from("Ending Balance", 109, 200)
        )
        rows = self.parse([statement_page(incoming="1,005.00", ending="5.00"), second])
        self.assertEqual([r["Reference ID"] for r in rows][-1], "3A4B5C6D7E8F")
        self.assertEqual(rows[-1]["Details"], "SAMPLE")

    def test_nothing_after_the_table_is_a_row(self):
        page = statement_page()
        page.words += maya_row(700, "30 Sep 2026", "Cash in", "9A9B9C9D9E9F", "1.00", "1.00")
        self.assertEqual(len(self.parse([page])), 3)

    def test_checks(self):
        cases = {
            "total incoming": (
                statement_page(incoming="900.00"),
                "parsed total incoming 1,000.00 != statement total 900.00",
            ),
            "total outgoing": (
                statement_page(outgoing="2,000.00"),
                "parsed total outgoing 3,000.00 != statement total 2,000.00",
            ),
            "ending balance": (
                statement_page(ending="1.00"),
                r"ending balance check failed: starting 2,000.00 \+ incoming 1,000.00 "
                "- outgoing 3,000.00 = 0.00 != statement ending 1.00",
            ),
        }
        for name, (page, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(StatementError, message):
                self.parse([page])

    def test_running_balance_check(self):
        rows = [STATEMENT_ROWS[0], dict(STATEMENT_ROWS[1], **{"Running balance": "2600.00"})]
        with self.assertRaisesRegex(
            StatementError,
            "running balance check failed on 10 Sep 2026 01:20:45 PM: "
            "expected 2,500.00, got 2,600.00",
        ):
            maya.check_totals(rows, {"starting": Decimal("2000.00")})


class BalancesAndSummaryTest(unittest.TestCase):
    def test_statement_balances(self):
        self.assertEqual(
            maya.statement_balances(STATEMENT_ROWS),
            {"starting": Decimal("2000.00"), "ending": Decimal("0.00")},
        )
        self.assertEqual(maya.statement_balances([]), {})

    def test_summary(self):
        self.assertEqual(
            maya.summary(STATEMENT_ROWS),
            "Total incoming 1,000.00 · Total outgoing 3,000.00 · matches statement ✓",
        )


# =============================================================================
# CONVERTING
# =============================================================================


class ToTransactionsTest(unittest.TestCase):
    def test_row_to_transaction_and_to_ynab(self):
        txn = maya.to_transactions(STATEMENT_ROWS)[1]
        self.assertEqual(
            txn,
            Transaction(
                source="maya",
                key="1A2B3C4D5E6F",
                date=date(2026, 9, 10),
                payee="Credit via InstaPay",
                amount=Decimal("1000.00"),
                memo="Maya Ref: 1A2B3C4D5E6F",
                flagged=False,
            ),
        )
        ynab = to_ynab(txn)
        self.assertEqual(
            (ynab["date"], ynab["amount"], ynab["import_id"]),
            ("2026-09-10", 1000000, make_import_id("maya", "1A2B3C4D5E6F")),
        )

    def test_money_out_is_negative(self):
        amounts = [t.amount for t in maya.to_transactions(STATEMENT_ROWS)]
        self.assertEqual(amounts, [Decimal("-500.00"), Decimal("1000.00"), Decimal("-2500.00")])

    def test_payee_is_the_details_or_else_the_type(self):
        row = dict(STATEMENT_ROWS[0], Details="")
        self.assertEqual(maya.to_transactions([row])[0].payee, "Purchased")


class PasswordTest(unittest.TestCase):
    def test_build_password(self):
        cases = {
            ("Juan", "De La Cruz", "12345"): "JDelacruz12345",
            ("juan", "SANTOS", "00001"): "JSantos00001",
            (" Ana Marie ", "Reyes", "54321"): "AReyes54321",
        }
        for (first, last, mobile), password in cases.items():
            with self.subTest(last):
                inputs = {"first_name": first, "last_name": last, "mobile_last5": mobile}
                self.assertEqual(maya.build_password(inputs), password)


class SourceTest(unittest.TestCase):
    def test_auto_import(self):
        source = SOURCES["maya"]
        self.assertEqual(source.columns, maya.COLUMNS)
        self.assertEqual(source.password_inputs, ("first_name", "last_name", "mobile_last5"))
        self.assertRegex(
            "MayaWallet_SoA_abc-123_01-Sep-2026_30-Sep-2026.pdf", source.attachment_pattern
        )


# =============================================================================
# CLI
# =============================================================================


class MainTest(unittest.TestCase):
    def test_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "maya.json")
            stderr = io.StringIO()
            with (
                mock.patch.object(maya, "open_pdf", return_value=FakePDF([statement_page()])),
                mock.patch("sys.argv", ["maya", "maya.pdf", "-p", "secret", "-o", out]),
                contextlib.redirect_stderr(stderr),
            ):
                maya.main()
            with open(out) as f:
                self.assertEqual(json.load(f), STATEMENT_ROWS)
        self.assertIn("Wrote 3 transactions", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
