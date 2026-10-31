"""
Tests for parsers/common.py: opening the PDF, grouping words into lines, amounts.

Run from the project folder:  python -m unittest discover tests
"""

import unittest
from decimal import Decimal
from unittest import mock

from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from statement_etl.parsers import common
from tests.fakes import word


class OpenPdfTest(unittest.TestCase):
    def test_returns_the_opened_pdf(self):
        with mock.patch.object(common.pdfplumber, "open", return_value="pdf") as opened:
            self.assertEqual(common.open_pdf("statement.pdf", "secret"), "pdf")
        opened.assert_called_once_with("statement.pdf", password="secret")

    def test_password_defaults_to_empty(self):
        with mock.patch.object(common.pdfplumber, "open") as opened:
            common.open_pdf("statement.pdf")
        opened.assert_called_once_with("statement.pdf", password="")

    def test_wrong_password(self):
        error = PdfminerException(PDFPasswordIncorrect())
        with (
            mock.patch.object(common.pdfplumber, "open", side_effect=error),
            self.assertRaisesRegex(common.StatementError, "^Incorrect PDF password.$"),
        ):
            common.open_pdf("statement.pdf", "wrong")

    def test_other_pdf_errors_are_raised_as_they_are(self):
        error = PdfminerException("broken file")
        with (
            mock.patch.object(common.pdfplumber, "open", side_effect=error),
            self.assertRaises(PdfminerException),
        ):
            common.open_pdf("statement.pdf")

    def test_empty_pdfminer_error_is_raised_as_it_is(self):
        with (
            mock.patch.object(common.pdfplumber, "open", side_effect=PdfminerException()),
            self.assertRaises(PdfminerException),
        ):
            common.open_pdf("statement.pdf")


class GroupLinesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(common.group_lines([]), [])

    def test_words_at_the_same_height_form_one_line(self):
        lines = common.group_lines([word("world", 60, top=100), word("hello", 10, top=101)])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "hello world")
        self.assertEqual(lines[0]["top"], 100)

    def test_tolerance_boundary(self):
        just_inside = common.LINE_TOLERANCE - 0.1
        lines = common.group_lines([word("a", 10, top=100), word("b", 20, top=100 + just_inside)])
        self.assertEqual(len(lines), 1)
        lines = common.group_lines(
            [word("a", 10, top=100), word("b", 20, top=100 + common.LINE_TOLERANCE)]
        )
        self.assertEqual(len(lines), 2)

    def test_lines_are_sorted_top_to_bottom_and_left_to_right(self):
        words = [
            word("third", 10, top=200),
            word("second-b", 90, top=150),
            word("first", 10, top=100),
            word("second-a", 10, top=150),
        ]
        lines = common.group_lines(words)
        self.assertEqual([line["text"] for line in lines], ["first", "second-a second-b", "third"])
        self.assertEqual([line["top"] for line in lines], [100, 150, 200])

    def test_words_are_kept(self):
        w = word("Debit", 410, top=180)
        self.assertEqual(common.group_lines([w])[0]["words"], [w])


class ToDecimalTest(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(common.to_decimal("1,234.50"), Decimal("1234.50"))
        self.assertEqual(common.to_decimal("1,000,000.00"), Decimal("1000000.00"))
        self.assertEqual(common.to_decimal("-59.00"), Decimal("-59.00"))

    def test_blank_is_zero(self):
        self.assertEqual(common.to_decimal(""), Decimal(0))
        self.assertEqual(common.to_decimal(None), Decimal(0))


if __name__ == "__main__":
    unittest.main()
