"""
Tests for parsers/maya_credit_card.py: reading page 1's unlabelled values by
where they are, the rows and their years, the amount-due check, the flipped
balances, to_transactions(), the password rule, and the CLI.

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

from statement_etl.parsers import maya_credit_card as card
from statement_etl.parsers.common import StatementError, group_lines
from statement_etl.sources import SOURCES
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import make_import_id, to_ynab
from tests.fakes import FakePage, FakePDF, right_word, word, words_from

# =============================================================================
# MAYA CARD LAYOUT - x positions on a fake page 1, close to a real statement's.
# The labels are images, so only values are on the page.
# =============================================================================

TRANSACTION_X = 34
POSTING_X = 125
DESCRIPTION_X = 215
AMOUNT_X1 = 541
RIGHT_COLUMN_X1 = 514  # right edge of the right-hand column's amounts


def php(amount, top, x1=RIGHT_COLUMN_X1):
    """ "500.00 PHP", or a payment's "- 500.00 PHP", in the right-hand column."""
    sign, number = ("-", amount[2:]) if amount.startswith("- ") else ("", amount)
    number_word = right_word(number, x1, top)
    sign_words = [right_word(sign, number_word["x0"] - 4, top)] if sign else []
    return sign_words + [number_word, word("PHP", x1 + 3, top)]


def card_row(top, transaction, posting, description, amount):
    """A row; `amount` as printed: "2,000.00", or a payment's "- 600.00"."""
    return (
        words_from(transaction, TRANSACTION_X, top)
        + words_from(posting, POSTING_X, top)
        + words_from(description, DESCRIPTION_X, top)
        + words_from(amount, AMOUNT_X1 - 5 * len(amount), top)
    )


ROWS = (
    ("20 Sep", "21 Sep", "SAMPLE STORE QUEZON CITY PH", "2,000.00"),
    ("30 Sep", "1 Oct", "SAMPLE CAFE PH", "300.00"),
    ("2 Oct", "5 Oct", "SAMPLE ONLINE US", "1,200.00"),
)


def statement_pages(
    parts=("-500.00", "0.00", "3,500.00", "0.00"),
    due="3,000.00",
    period="21 Sep 2026 to 20 Oct 2026",
    rows=ROWS,
):
    """A made-up statement: page 1 with every value and the rows, then a page
    that is an image (only its page number is text).

    `parts` is the right-hand column under the billing date: the previous
    balance, then what changed it (payments, purchases, fees).
    """
    right_column = []
    for number, part in enumerate(parts):
        right_column += php(part, 146 + 24 * number)
    page1 = FakePage(
        words_from("Statement of Account", 339, 42)
        + words_from("Billing date: 20 October 2026", 391, 73)
        + right_column
        + words_from("SAMPLE NAME", 51, 173)
        + [word("0000", 111, 217)]
        + words_from("10,000.00 PHP", 51, 260)  # the credit limit, on the left
        + words_from("9 Nov 2026", 222, 399)
        + php(due, 399)
        + php("500.00", 426)  # the minimum due
        + words_from("Transactions", 34, 507)
        + words_from(period, 34, 525)
        + [w for n, r in enumerate(rows) for w in card_row(572 + 18 * n, *r)]
        + words_from("Page 1 of5", 512, 809)
    )
    return [page1, FakePage(words_from("Page 2 of5", 512, 785))]


def card_row_dict(transaction, posting, description, amount, balance):
    return {
        "Transaction date": transaction,
        "Posting date": posting,
        "Description": description,
        "Amount": amount,
        "Balance": balance,
    }


STATEMENT_ROWS = [
    card_row_dict(
        "20 Sep 2026", "21 Sep 2026", "SAMPLE STORE QUEZON CITY PH", "2000.00", "1500.00"
    ),
    card_row_dict("30 Sep 2026", "1 Oct 2026", "SAMPLE CAFE PH", "300.00", "1800.00"),
    card_row_dict("2 Oct 2026", "5 Oct 2026", "SAMPLE ONLINE US", "1200.00", "3000.00"),
]


# =============================================================================
# PAGE LAYOUT
# =============================================================================


class ReadSummaryTest(unittest.TestCase):
    def test_reads_the_values_by_where_they_are(self):
        self.assertEqual(
            card.read_summary(group_lines(statement_pages()[0].words)),
            {
                "billing_date": date(2026, 10, 20),
                "parts": [Decimal("-500.00"), Decimal("0.00"), Decimal("3500.00"), Decimal("0.00")],
                "due": Decimal("3000.00"),
                "start": date(2026, 9, 21),
                "end": date(2026, 10, 20),
            },
        )

    def test_the_parts_are_the_right_hand_amounts_above_the_due_date(self):
        # Not the credit limit on the left, nor the minimum due under the due date
        page = statement_pages(parts=("1,234.56", "- 1,000.00"))[0]
        self.assertEqual(
            card.read_summary(group_lines(page.words))["parts"],
            [Decimal("1234.56"), Decimal("-1000.00")],
        )


# =============================================================================
# PARSING
# =============================================================================


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages, password="secret"):
        with mock.patch.object(card, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = card.parse_transactions("card.pdf", password)
        opened.assert_called_once_with("card.pdf", password)
        return rows

    def test_reads_every_row_and_column(self):
        self.assertEqual(self.parse(statement_pages()), STATEMENT_ROWS)

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        with mock.patch.object(card, "open_pdf", return_value=FakePDF(statement_pages())):
            card.parse_transactions("card.pdf", "secret", on_page=on_page)
        self.assertEqual(on_page.call_args_list, [mock.call(1, 2), mock.call(2, 2)])

    def test_a_period_across_new_year(self):
        pages = statement_pages(
            parts=("0.00", "500.00"),
            due="500.00",
            period="21 Dec 2026 to 20 Jan 2027",
            rows=(
                ("28 Dec", "29 Dec", "SAMPLE STORE", "250.00"),
                ("3 Jan", "4 Jan", "SAMPLE CAFE", "250.00"),
            ),
        )
        rows = self.parse(pages)
        self.assertEqual([r["Transaction date"] for r in rows], ["28 Dec 2026", "3 Jan 2027"])

    def test_payments(self):
        # Maya prints "- 600.00"; a leading "-" and a trailing "CR" are read too.
        # A "-" in the description, away from the amount, stays in it.
        pages = statement_pages(
            parts=("1,000.00", "- 1,000.00"),
            due="0.00",
            rows=(
                ("1 Oct", "1 Oct", "BILL PAYMENT - THANK YOU", "- 600.00"),
                ("2 Oct", "2 Oct", "BILL PAYMENT", "-300.00"),
                ("3 Oct", "3 Oct", "BILL PAYMENT", "100.00 CR"),
            ),
        )
        rows = self.parse(pages)
        self.assertEqual(
            [(r["Description"], r["Amount"]) for r in rows],
            [
                ("BILL PAYMENT - THANK YOU", "-600.00"),
                ("BILL PAYMENT", "-300.00"),
                ("BILL PAYMENT", "-100.00"),
            ],
        )
        self.assertEqual(rows[-1]["Balance"], "0.00")

    def test_an_overpaid_card(self):
        # Paid more than owed: the balance is below 0 and the amount due is 0.00
        pages = statement_pages(
            parts=("1,000.00", "- 1,500.00", "200.00"),
            due="0.00",
            rows=(
                ("1 Oct", "1 Oct", "BILL PAYMENT", "- 1,500.00"),
                ("2 Oct", "3 Oct", "SAMPLE CAFE", "200.00"),
            ),
        )
        rows = self.parse(pages)
        self.assertEqual(rows[-1]["Balance"], "-300.00")
        self.assertEqual(
            card.statement_balances(rows),
            {"starting": Decimal("-1000.00"), "ending": Decimal("300.00")},
        )

    def test_rows_on_a_later_page(self):
        pages = statement_pages(parts=("-500.00", "3,600.00"), due="3,100.00")
        pages[1].words += card_row(100, "3 Oct", "6 Oct", "SAMPLE SHOP", "100.00")
        self.assertEqual(self.parse(pages)[-1]["Description"], "SAMPLE SHOP")

    def test_checks(self):
        cases = {
            "rows and parts": (
                statement_pages(parts=("-500.00", "3,400.00"), due="2,900.00"),
                "the rows add up to 3,500.00, but the statement's summary adds up to 3,400.00",
            ),
            "amount due": (
                statement_pages(due="2,900.00"),
                r"amount due check failed: previous balance -500.00 \+ amounts 3,500.00 "
                "= 3,000.00, so the amount due should be 3,000.00, not 2,900.00",
            ),
            "overpaid amount due": (
                statement_pages(
                    parts=("1,000.00", "- 1,500.00"),
                    due="500.00",
                    rows=(("1 Oct", "1 Oct", "BILL PAYMENT", "- 1,500.00"),),
                ),
                "= -500.00, so the amount due should be 0.00, not 500.00",
            ),
        }
        for name, (pages, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(StatementError, message):
                self.parse(pages)

    def test_a_missing_value_is_an_error(self):
        [page1, _] = statement_pages()
        cases = {
            # The parts, previous balance first, are read under the billing date
            "previous balance": lambda w: w["top"] == 73,
            "amount due": lambda w: w["top"] == 399,
            "period": lambda w: w["top"] == 525,
        }
        for what, drop in cases.items():
            page = FakePage([w for w in page1.words if not drop(w)])
            with (
                self.subTest(what),
                self.assertRaisesRegex(StatementError, f"Could not find the {what}"),
            ):
                self.parse([page])


class BalancesAndSummaryTest(unittest.TestCase):
    def test_statement_balances_are_flipped_for_ynab(self):
        # The statement's -500.00 is an overpayment: +500.00 in YNAB
        self.assertEqual(
            card.statement_balances(STATEMENT_ROWS),
            {"starting": Decimal("500.00"), "ending": Decimal("-3000.00")},
        )
        self.assertEqual(card.statement_balances([]), {})

    def test_summary(self):
        self.assertEqual(
            card.summary(STATEMENT_ROWS),
            "Previous balance -500.00 + amounts 3,500.00 = ending balance 3,000.00 "
            "· matches statement ✓",
        )
        self.assertIsNone(card.summary([]))


# =============================================================================
# CONVERTING
# =============================================================================


class ToTransactionsTest(unittest.TestCase):
    def test_row_to_transaction_and_to_ynab(self):
        txn = card.to_transactions(STATEMENT_ROWS)[1]
        key = "30 Sep 2026|1 Oct 2026|300.00|SAMPLE CAFE PH|0"
        self.assertEqual(
            txn,
            Transaction(
                source="maya_credit_card",
                key=key,
                date=date(2026, 9, 30),
                payee="SAMPLE CAFE PH",
                amount=Decimal("-300.00"),
                memo=txn.memo,
                flagged=False,
            ),
        )
        self.assertRegex(txn.memo, r"^Maya Card Ref: [0-9a-f]{30}$")
        ynab = to_ynab(txn)
        self.assertEqual(
            (ynab["date"], ynab["amount"], ynab["import_id"]),
            ("2026-09-30", -300000, make_import_id("maya_credit_card", key)),
        )

    def test_a_payment_is_money_in(self):
        row = card_row_dict("1 Oct 2026", "1 Oct 2026", "PAYMENT", "-600.00", "0.00")
        self.assertEqual(card.to_transactions([row])[0].amount, Decimal("600.00"))

    def test_same_looking_rows_get_different_stable_keys(self):
        row = STATEMENT_ROWS[0]
        first, second = card.to_transactions([row, row])
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(card.to_transactions([row, row]), [first, second])


class PasswordTest(unittest.TestCase):
    def test_build_password(self):
        cases = {
            ("2000-01-01", "123456"): "01Jan2000123456",
            ("1990-12-31", "000001"): "31Dec1990000001",
        }
        for (birth_date, mobile), password in cases.items():
            with self.subTest(birth_date):
                inputs = {"birth_date": birth_date, "mobile_last6": mobile}
                self.assertEqual(card.build_password(inputs), password)

    def test_a_date_that_does_not_exist(self):
        with self.assertRaises(ValueError):
            card.build_password({"birth_date": "2000-02-30", "mobile_last6": "123456"})


class SourceTest(unittest.TestCase):
    def test_auto_import(self):
        source = SOURCES["maya_credit_card"]
        self.assertEqual(source.columns, card.COLUMNS)
        self.assertEqual(source.password_inputs, ("birth_date", "mobile_last6"))
        self.assertRegex("MayaCreditCard_SoA_0a1b2c_2026OCT.pdf", source.attachment_pattern)


# =============================================================================
# CLI
# =============================================================================


class MainTest(unittest.TestCase):
    def test_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "card.json")
            stderr = io.StringIO()
            with (
                mock.patch.object(card, "open_pdf", return_value=FakePDF(statement_pages())),
                mock.patch("sys.argv", ["card", "card.pdf", "-p", "secret", "-o", out]),
                contextlib.redirect_stderr(stderr),
            ):
                card.main()
            with open(out) as f:
                self.assertEqual(json.load(f), STATEMENT_ROWS)
        self.assertIn("Wrote 3 transactions", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
