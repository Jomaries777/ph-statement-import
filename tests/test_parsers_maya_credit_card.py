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
    """ "-500.00 PHP" in the right-hand column."""
    return [right_word(amount, x1, top), word("PHP", x1 + 3, top)]


def card_row(top, transaction, posting, description, amount):
    return (
        words_from(transaction, TRANSACTION_X, top)
        + words_from(posting, POSTING_X, top)
        + words_from(description, DESCRIPTION_X, top)
        + words_from(amount, AMOUNT_X1 - 5 * len(amount), top)
    )


def statement_pages(previous="-500.00", due="3,000.00", period="21 Sep 2026 to 20 Oct 2026"):
    """A made-up statement: page 1 with every value and three rows, then a
    page that is an image (only its page number is text)."""
    page1 = FakePage(
        words_from("Statement of Account", 339, 42)
        + words_from("Billing date: 20 October 2026", 391, 73)
        + php(previous, 146)
        + words_from("SAMPLE NAME", 51, 173)
        + php("0.00", 170)
        + [word("0000", 111, 217)]
        + php("3,500.00", 217)
        + words_from("10,000.00 PHP", 51, 260)  # the credit limit, on the left
        + php("0.00", 267)
        + words_from("9 Nov 2026", 222, 399)
        + php(due, 399)
        + php("500.00", 426)
        + words_from("Transactions", 34, 507)
        + words_from(period, 34, 525)
        + card_row(572, "20 Sep", "21 Sep", "SAMPLE STORE QUEZON CITY PH", "2,000.00")
        + card_row(590, "30 Sep", "1 Oct", "SAMPLE CAFE PH", "300.00")
        + card_row(608, "2 Oct", "5 Oct", "SAMPLE ONLINE US", "1,200.00")
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
                "previous": Decimal("-500.00"),
                "due": Decimal("3000.00"),
                "start": date(2026, 9, 21),
                "end": date(2026, 10, 20),
            },
        )

    def test_the_previous_balance_is_the_first_right_hand_amount(self):
        # The credit limit on the left, and the amounts under the first, aren't it
        lines = group_lines(
            words_from("Billing date: 20 October 2026", 391, 73)
            + words_from("10,000.00 PHP", 51, 100)
            + php("1,234.56", 146)
            + php("0.00", 170)
        )
        self.assertEqual(card.read_summary(lines)["previous"], Decimal("1234.56"))


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
        pages = statement_pages(period="21 Dec 2026 to 20 Jan 2027")
        rows = self.parse(pages)
        # Sep and Oct are after January: the year before the period's end
        self.assertEqual([r["Transaction date"] for r in rows][0], "20 Sep 2026")
        [page1, _] = statement_pages(due="0.00", period="21 Dec 2026 to 20 Jan 2027")
        page1.words = [w for w in page1.words if w["top"] < 570]
        page1.words += card_row(572, "28 Dec", "29 Dec", "SAMPLE STORE", "250.00")
        page1.words += card_row(590, "3 Jan", "4 Jan", "SAMPLE CAFE", "250.00")
        rows = self.parse([page1])
        self.assertEqual([r["Transaction date"] for r in rows], ["28 Dec 2026", "3 Jan 2027"])

    def test_a_payment_printed_with_a_minus_or_cr(self):
        [page1, _] = statement_pages(previous="1,000.00", due="0.00")
        page1.words = [w for w in page1.words if w["top"] < 570]
        page1.words += card_row(572, "1 Oct", "1 Oct", "PAYMENT - THANK YOU", "-600.00")
        page1.words += card_row(590, "2 Oct", "2 Oct", "PAYMENT - THANK YOU", "400.00")
        page1.words.append(word("CR", AMOUNT_X1 + 3, 590))
        rows = self.parse([page1])
        self.assertEqual([r["Amount"] for r in rows], ["-600.00", "-400.00"])
        self.assertEqual(rows[-1]["Balance"], "0.00")

    def test_rows_on_a_later_page(self):
        pages = statement_pages(due="3,100.00")
        pages[1].words += card_row(100, "3 Oct", "6 Oct", "SAMPLE SHOP", "100.00")
        self.assertEqual(self.parse(pages)[-1]["Description"], "SAMPLE SHOP")

    def test_the_amount_due_check(self):
        with self.assertRaisesRegex(
            StatementError,
            r"amount due check failed: previous balance -500.00 \+ amounts 3,500.00 "
            "= 3,000.00 != statement amount due 2,900.00",
        ):
            self.parse(statement_pages(due="2,900.00"))

    def test_a_missing_value_is_an_error(self):
        [page1, _] = statement_pages()
        cases = {
            # The previous balance is found under the billing date. (Without the
            # amount itself, the next one is read instead, and the amount due
            # check fails.)
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
            "Previous balance -500.00 + amounts 3,500.00 = amount due 3,000.00 "
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
