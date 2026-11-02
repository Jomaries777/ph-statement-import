"""
Tests for parsers/bpi_credit_card.py: decoding BPI's EBCDIC chars, reading
the summary and the transaction table, the dates' years, the balance checks,
the flipped balances, to_transactions(), the password rule, and the CLI.

Run from the project folder:  python -m unittest discover tests

The PDF is faked: pages return chars like pdfplumber does, encoded the way
BPI's font encodes them, so no real statement is needed. All data here is
made up.
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

from statement_etl.parsers import bpi_credit_card as card
from statement_etl.parsers.common import StatementError
from statement_etl.sources import SOURCES
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import make_import_id, to_ynab
from tests.fakes import FakePDF

# =============================================================================
# EBCDIC CHARS - the reverse of card.decode_chars: text -> chars as
# pdfplumber reads them from BPI's font
# =============================================================================

LETTER_WIDTH = 5.0
SPACE_WIDTH = 2.6  # as on BPI's statement

_GLYPHS = {code: glyph for glyph, code in card._STANDARD_CODES.items()}


def ebcdic(letter):
    """ "P" -> "(cid:215)", "1" -> "æ": what pdfplumber reads for `letter` in BPI's font."""
    code = letter.encode(card.EBCDIC)[0]
    return _GLYPHS.get(code, f"(cid:{code})")


def chars(text, x0, top, encode=True):
    """The chars of `text` from x0, each space a char of its own, like BPI's."""
    result = []
    for letter in text:
        width = SPACE_WIDTH if letter == " " else LETTER_WIDTH
        shown = ebcdic(letter) if encode else letter
        result.append({"text": shown, "x0": x0, "x1": x0 + width, "top": float(top)})
        x0 += width
    return result


def right_chars(text, x1, top, encode=True):
    width = sum(SPACE_WIDTH if letter == " " else LETTER_WIDTH for letter in text)
    return chars(text, x1 - width, top, encode)


class CharPage:
    """Stands in for a pdfplumber page: only .chars is read."""

    def __init__(self, chars):
        self.chars = chars


# =============================================================================
# BPI CARD LAYOUT - x positions on a fake statement, close to a real one's
# =============================================================================

TRANSACTION_X = 54
POST_X = 144
DESCRIPTION_X = 220
AMOUNT_X1 = 531
SUMMARY_X1 = 567  # right edge of page 3's values


def row(top, transaction, post, description, amount, encode=True):
    return (
        (chars(transaction, TRANSACTION_X, top, encode) if transaction else [])
        + (chars(post, POST_X, top, encode) if post else [])
        + chars(description, DESCRIPTION_X, top, encode)
        + right_chars(amount, AMOUNT_X1, top, encode)
    )


def balance_line(top, label, amount, encode=True):
    return chars(label, DESCRIPTION_X, top, encode) + right_chars(amount, AMOUNT_X1, top, encode)


def summary_page(statement_date="OCTOBER 15, 2026", total_due="2,750.00", encode=True):
    """Page 3: the statement date and total amount due."""
    return CharPage(
        chars("Prepared for", 36, 135, encode)
        + chars("STATEMENT DATE", 324, 167, encode)
        + right_chars(statement_date, SUMMARY_X1, 167, encode)
        + chars("TOTAL AMOUNT DUE", 324, 210, encode)
        + right_chars(total_due, SUMMARY_X1, 210, encode)
    )


def table_page(previous="5,000.00", ending="2,750.00", rows=None, encode=True):
    """Page 5: the transaction table, with the account's rows, its balances,
    then one card's rows."""
    if rows is None:
        rows = (
            row(377, "October 15", "October 15", "SAMPLE GADGETS     Qu: 3/24", "250.00", encode)
            + row(
                387,
                "September 20",
                "September 21",
                "SAMPLE.COM             Sample City",
                "1,000.00",
                encode,
            )
            + row(
                397,
                "September 28",
                "September 29",
                "SAMPLE STORE            Taguig",
                "1,400.00",
                encode,
            )
        )
    return CharPage(
        chars("Statement of Account", 36, 136, encode)
        + chars("Transaction", TRANSACTION_X, 211, encode)
        + chars("Post Date", POST_X, 211, encode)
        + chars("Description", DESCRIPTION_X, 211, encode)
        + chars("Amount", 504, 211, encode)
        + chars("Date", TRANSACTION_X, 222, encode)
        + row(243, "September 20", "September 21", "Payment - Thank You", "-5,000.00", encode)
        + row(253, "", "", "Finance Charge", "100.00", encode)
        + balance_line(271, "Previous Balance", previous, encode)
        + balance_line(281, "Past Due", "0.00", encode)
        + balance_line(292, "Ending Balance", ending, encode)
        + balance_line(302, "Unbilled Installment Amount", "0.00", encode)
        + chars("000000-0-00-0000000 - SAMPLE NAME", TRANSACTION_X, 344, encode)
        + chars("Installment Amortization:", DESCRIPTION_X, 366, encode)
        + rows
    )


def statement_pages(encode=True, **table):
    """A made-up statement: a terms page, the summary, the table, and an
    image-only last page."""
    return [
        CharPage(chars("RATES AND FEES TABLE", 31, 240, encode)),
        summary_page(encode=encode),
        table_page(encode=encode, **table),
        CharPage([]),
    ]


def card_row_dict(transaction, post, description, amount, balance):
    return {
        "Transaction Date": transaction,
        "Post Date": post,
        "Description": description,
        "Amount": amount,
        "Balance": balance,
    }


STATEMENT_ROWS = [
    card_row_dict("Sep 20, 2026", "Sep 21, 2026", "Payment - Thank You", "-5000.00", "0.00"),
    card_row_dict("Oct 15, 2026", "Oct 15, 2026", "Finance Charge", "100.00", "100.00"),
    card_row_dict("Oct 15, 2026", "Oct 15, 2026", "SAMPLE GADGETS  Qu: 3/24", "250.00", "350.00"),
    card_row_dict("Sep 20, 2026", "Sep 21, 2026", "SAMPLE.COM  Sample City", "1000.00", "1350.00"),
    card_row_dict("Sep 28, 2026", "Sep 29, 2026", "SAMPLE STORE  Taguig", "1400.00", "2750.00"),
]


# =============================================================================
# DECODING
# =============================================================================


class DecodeTest(unittest.TestCase):
    def test_decodes_cids_and_standard_glyphs(self):
        # Lowercase letters have no StandardEncoding glyph; digits and capitals do
        self.assertEqual(ebcdic("r"), "(cid:153)")
        self.assertEqual(ebcdic("1"), "æ")
        decoded = card.decode_chars(chars("Prepared for 2026", 36, 100))
        self.assertEqual("".join(c["text"] for c in decoded), "Prepared for 2026")

    def test_a_plain_text_page_is_read_as_it_is(self):
        plain = chars("Prepared for 2026", 36, 100, encode=False)
        self.assertEqual(card.decode_chars(plain), plain)

    def test_lines_and_words(self):
        page = CharPage(
            row(387, "September 20", "September 21", "SAMPLE.COM      City", "1,000.00")
        )
        [line] = card.page_lines(page)
        self.assertEqual(line["text"], "September 20 September 21 SAMPLE.COM City 1,000.00")
        self.assertEqual(line["words"][0]["x0"], TRANSACTION_X)


# =============================================================================
# PARSING
# =============================================================================


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages, password="20000101"):
        with mock.patch.object(card, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = card.parse_transactions("card.pdf", password)
        opened.assert_called_once_with("card.pdf", password)
        return rows

    def test_reads_every_row_and_column(self):
        self.assertEqual(self.parse(statement_pages()), STATEMENT_ROWS)

    def test_reads_a_plain_text_statement_too(self):
        self.assertEqual(self.parse(statement_pages(encode=False)), STATEMENT_ROWS)

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        with mock.patch.object(card, "open_pdf", return_value=FakePDF(statement_pages())):
            card.parse_transactions("card.pdf", "20000101", on_page=on_page)
        self.assertEqual(on_page.call_args, mock.call(4, 4))

    def test_rows_of_two_cards(self):
        rows = row(377, "October 1", "October 2", "SAMPLE CAFE      City", "500.00")
        rows += chars("000000-0-00-0000001 - SAMPLE OTHER NAME", TRANSACTION_X, 400)
        rows += row(422, "October 3", "October 4", "SAMPLE BOOKS      City", "2,150.00")
        parsed = self.parse(statement_pages(rows=rows))
        self.assertEqual(
            [r["Description"] for r in parsed][-2:], ["SAMPLE CAFE  City", "SAMPLE BOOKS  City"]
        )

    def test_a_statement_across_new_year(self):
        pages = statement_pages(rows=row(377, "December 28", "December 29", "SAMPLE", "2,650.00"))
        pages[1] = summary_page(statement_date="JANUARY 15, 2027")
        rows = self.parse(pages)
        self.assertEqual(
            [r["Transaction Date"] for r in rows],
            ["Sep 20, 2026", "Jan 15, 2027", "Dec 28, 2026"],
        )

    def test_the_ending_balance_check(self):
        with self.assertRaisesRegex(
            StatementError,
            r"ending balance check failed: previous balance 5,000.00 \+ amounts -2,250.00 "
            "= 2,750.00 != statement ending balance 2,700.00",
        ):
            self.parse(statement_pages(ending="2,700.00"))

    def test_the_total_amount_due_check(self):
        pages = statement_pages()
        pages[1] = summary_page(total_due="2,000.00")
        with self.assertRaisesRegex(
            StatementError, "ending balance 2,750.00 != statement total amount due 2,000.00"
        ):
            self.parse(pages)

    def test_a_missing_summary_line_is_an_error(self):
        pages = statement_pages()
        pages[1] = CharPage([])
        with self.assertRaisesRegex(
            StatementError, "Could not find the statement's STATEMENT DATE"
        ):
            self.parse(pages)


class BalancesAndSummaryTest(unittest.TestCase):
    def test_statement_balances_are_flipped_for_ynab(self):
        self.assertEqual(
            card.statement_balances(STATEMENT_ROWS),
            {"starting": Decimal("-5000.00"), "ending": Decimal("-2750.00")},
        )
        self.assertEqual(card.statement_balances([]), {})

    def test_summary(self):
        self.assertEqual(
            card.summary(STATEMENT_ROWS),
            "Previous balance 5,000.00 + amounts -2,250.00 = ending balance 2,750.00 "
            "· matches statement ✓",
        )
        self.assertIsNone(card.summary([]))


# =============================================================================
# CONVERTING
# =============================================================================


class ToTransactionsTest(unittest.TestCase):
    def test_row_to_transaction_and_to_ynab(self):
        txn = card.to_transactions(STATEMENT_ROWS)[4]
        key = "Sep 28, 2026|Sep 29, 2026|1400.00|SAMPLE STORE  Taguig|0"
        self.assertEqual(
            txn,
            Transaction(
                source="bpi_credit_card",
                key=key,
                date=date(2026, 9, 28),
                payee="SAMPLE STORE",
                amount=Decimal("-1400.00"),
                memo=txn.memo,
                flagged=False,
            ),
        )
        self.assertRegex(txn.memo, r"^BPI Card Ref: [0-9a-f]{30}$")
        ynab = to_ynab(txn)
        self.assertEqual(
            (ynab["date"], ynab["amount"], ynab["import_id"]),
            ("2026-09-28", -1400000, make_import_id("bpi_credit_card", key)),
        )

    def test_a_payment_is_money_in(self):
        self.assertEqual(card.to_transactions(STATEMENT_ROWS)[0].amount, Decimal("5000.00"))

    def test_payee_is_the_merchant(self):
        payees = [t.payee for t in card.to_transactions(STATEMENT_ROWS)]
        self.assertEqual(
            payees,
            [
                "Payment - Thank You",
                "Finance Charge",
                "SAMPLE GADGETS",
                "SAMPLE.COM",
                "SAMPLE STORE",
            ],
        )

    def test_same_looking_rows_get_different_stable_keys(self):
        row_ = STATEMENT_ROWS[1]
        first, second = card.to_transactions([row_, row_])
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(card.to_transactions([row_, row_]), [first, second])


class PasswordTest(unittest.TestCase):
    def test_build_password(self):
        self.assertEqual(card.build_password({"birth_date": "2000-01-31"}), "20000131")


class SourceTest(unittest.TestCase):
    def test_auto_import(self):
        source = SOURCES["bpi_credit_card"]
        self.assertEqual(source.columns, card.COLUMNS)
        self.assertEqual(source.password_inputs, ("birth_date",))
        self.assertRegex("BPI eStatement-October 15, 2026.pdf", source.attachment_pattern)


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
                mock.patch("sys.argv", ["card", "card.pdf", "-p", "20000101", "-o", out]),
                contextlib.redirect_stderr(stderr),
            ):
                card.main()
            with open(out) as f:
                self.assertEqual(json.load(f), STATEMENT_ROWS)
        self.assertIn("Wrote 5 transactions", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
