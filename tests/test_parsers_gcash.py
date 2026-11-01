"""
Tests for parsers/gcash.py: reading the GCash table from page words, the
totals check, the running-balance corrections and to_transactions().

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
from datetime import datetime
from decimal import Decimal
from unittest import mock

from statement_etl.parsers import gcash
from statement_etl.parsers.common import StatementError, group_lines
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import to_ynab
from tests.fakes import (
    BALANCE_X1,
    CREDIT_X1,
    DEBIT_X1,
    DESC_X,
    REF_X,
    FakePage,
    FakePDF,
    gcash_header,
    gcash_line,
    gcash_row,
    right_word,
    word,
    words_from,
)


def one_line(words):
    """The single text line made of `words`."""
    lines = group_lines(words)
    assert len(lines) == 1, lines
    return lines[0]


def parse_line(*args, **kwargs):
    return gcash._parse_transaction_line(one_line(gcash_line(*args, **kwargs)), DEBIT_X1, CREDIT_X1)


def summary_line(top, label, amount, x1=BALANCE_X1):
    """A summary line inside the table, e.g. "Total Debit 350.50"."""
    return words_from(label, DESC_X, top) + [right_word(amount, x1, top)]


# =============================================================================
# PAYEE
# =============================================================================


class GCashPayeeTest(unittest.TestCase):
    def test_payment_with_merchant_number(self):
        self.assertEqual(
            gcash.gcash_payee("Payment to Test Shop, Merchant Transaction Number: X1"), "Test Shop"
        )

    def test_payment_is_case_insensitive(self):
        self.assertEqual(
            gcash.gcash_payee("payment to Test Shop, merchant transaction number: X1"), "Test Shop"
        )

    def test_payment_without_merchant_number(self):
        self.assertEqual(gcash.gcash_payee("Payment to   Test Shop  "), "Test Shop")

    def test_other_descriptions_are_kept(self):
        for description in ("Received GCash from Test Sender", "Refund from Test Rides", ""):
            self.assertEqual(gcash.gcash_payee(description), description)


# =============================================================================
# PAGE LAYOUT
# =============================================================================


class FindColumnsTest(unittest.TestCase):
    def test_header_found(self):
        lines = group_lines(words_from("Transaction History", 20, 50) + gcash_header(100))
        self.assertEqual(gcash._find_columns(lines), (100, DEBIT_X1, CREDIT_X1))

    def test_no_header(self):
        self.assertIsNone(
            gcash._find_columns(group_lines(words_from("Transaction History", 20, 50)))
        )
        self.assertIsNone(gcash._find_columns([]))

    def test_needs_all_three_labels(self):
        header = [w for w in gcash_header(100) if w["text"] != "Balance"]
        self.assertIsNone(gcash._find_columns(group_lines(header)))


class ParseTransactionLineTest(unittest.TestCase):
    def test_debit_row(self):
        row = parse_line(
            150,
            "2026-10-02 06:50 PM",
            "Payment to Test Shop",
            "1000000000002",
            debit="250.50",
            balance="1,249.50",
        )
        self.assertEqual(row["datetime"], datetime(2026, 10, 2, 18, 50))
        self.assertEqual(row["reference"], "1000000000002")
        self.assertEqual((row["debit"], row["credit"], row["balance"]), ("250.50", "", "1,249.50"))
        self.assertEqual([w["text"] for w in row["desc_words"]], ["Payment", "to", "Test", "Shop"])
        self.assertEqual(row["top"], 150)

    def test_credit_row(self):
        row = parse_line(
            130,
            "2026-10-01 09:00 AM",
            "Received GCash",
            "1000000000001",
            credit="1,000.00",
            balance="1,500.00",
        )
        self.assertEqual((row["debit"], row["credit"]), ("", "1,000.00"))

    def test_description_column_edges(self):
        row = parse_line(
            130, "2026-10-01 09:00 AM", "Cash in", "1000000000001", credit="1.00", balance="1.00"
        )
        self.assertEqual(row["desc_left"], 115)  # right edge of "AM"
        self.assertEqual(row["desc_right"], REF_X)

    def test_negative_amount(self):
        row = parse_line(
            130, "2026-10-01 09:00 AM", "Reversal", "1000000000001", debit="-59.00", balance="1.00"
        )
        self.assertEqual(row["debit"], "-59.00")

    def test_not_a_transaction(self):
        good = gcash_line(
            130, "2026-10-01 09:00 AM", "Cash in", "1000000000001", credit="1.00", balance="1.00"
        )
        cases = {
            "too few words": good[:4],
            "no date": [word("Hello", 20, 130)] + good[1:],
            "bad time": good[:1] + [word("9am", 75, 130)] + good[2:],
            "no AM/PM": good[:2] + [word("XX", 105, 130)] + good[3:],
            "no reference": [w for w in good if w["text"] != "1000000000001"],
            "short reference": [
                word("12345", REF_X, 130) if w["text"] == "1000000000001" else w for w in good
            ],
            "balance only": [w for w in good if w["text"] != "1.00"]
            + [right_word("1.00", BALANCE_X1, 130)],
        }
        for name, words in cases.items():
            with self.subTest(name):
                line = {"top": 130, "words": words, "text": " ".join(w["text"] for w in words)}
                self.assertIsNone(gcash._parse_transaction_line(line, DEBIT_X1, CREDIT_X1))


class AttachWrappedLinesTest(unittest.TestCase):
    def test_wrapped_text_goes_to_the_nearest_row(self):
        first = parse_line(
            100, "2026-10-01 09:00 AM", "First", "1000000000001", credit="1.00", balance="1.00"
        )
        second = parse_line(
            200, "2026-10-02 09:00 AM", "Second", "1000000000002", debit="1.00", balance="0.00"
        )
        other_lines = group_lines(
            words_from("first-wrap", DESC_X, 110)
            + words_from("second-wrap", DESC_X, 190)
            + [word("Page", 20, 150), word("1", 400, 150)]  # outside the Description column
        )
        gcash._attach_wrapped_lines([first, second], other_lines)
        self.assertEqual([w["text"] for w in first["desc_words"]], ["First", "first-wrap"])
        self.assertEqual([w["text"] for w in second["desc_words"]], ["Second", "second-wrap"])


class ToOutputRowTest(unittest.TestCase):
    def test_columns_and_wrapped_words_in_reading_order(self):
        row = parse_line(
            150,
            "2026-10-02 06:50 PM",
            "Payment to Test Shop,",
            "1000000000002",
            debit="250.50",
            balance="1,249.50",
        )
        # wrapped lines are attached out of order: below first, then above
        row["desc_words"] += words_from("Number: X1", DESC_X, 160) + words_from(
            "Merchant Transaction", DESC_X, 155
        )
        out = gcash._to_output_row(row)
        self.assertEqual(tuple(out), gcash.COLUMNS)
        self.assertEqual(out["Date and Time"], "2026-10-02 06:50 PM")
        self.assertEqual(
            out["Description"], "Payment to Test Shop, Merchant Transaction Number: X1"
        )
        self.assertEqual(out["Reference No."], "1000000000002")
        self.assertEqual((out["Debit"], out["Credit"], out["Balance"]), ("250.50", "", "1,249.50"))


# =============================================================================
# PARSING - a whole fake PDF
# =============================================================================


def statement_pages(total_debit="350.50", ending="1149.50"):
    cover = FakePage(words_from("GCash Transaction History", 20, 50))
    page1 = FakePage(
        words_from("Transaction History", 20, 50)
        + gcash_header(100)
        + summary_line(115, "STARTING BALANCE", "500.00")
        + gcash_line(
            130,
            "2026-10-01 09:00 AM",
            "Received GCash from Test Sender",
            "1000000000001",
            credit="1,000.00",
            balance="1,500.00",
        )
        + gcash_line(
            150,
            "2026-10-02 06:50 PM",
            "Payment to Test Shop,",
            "1000000000002",
            debit="250.50",
            balance="1,249.50",
        )
        + words_from("Merchant Transaction Number: X1", DESC_X, 160)
        + words_from("Page 1 of 2", 20, 700)
    )
    # no header: the columns from page 1 are reused
    page2 = FakePage(
        gcash_line(
            120,
            "2026-10-03 08:15 AM",
            "Payment to Test Cafe",
            "1000000000003",
            debit="100.00",
            balance="1,149.50",
        )
        + summary_line(140, "ENDING BALANCE", ending)
        + summary_line(150, "Total Debit", total_debit, DEBIT_X1)
        + summary_line(160, "Total Credit", "1,000.00", CREDIT_X1)
    )
    return [cover, page1, page2]


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages):
        with mock.patch.object(gcash, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = gcash.parse_transactions("statement.pdf", "secret")
        opened.assert_called_once_with("statement.pdf", "secret")
        return rows

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        with mock.patch.object(gcash, "open_pdf", return_value=FakePDF(statement_pages())):
            gcash.parse_transactions("statement.pdf", "secret", on_page=on_page)
        pages = len(statement_pages())
        self.assertEqual(on_page.call_args_list, [mock.call(n, pages) for n in range(1, pages + 1)])

    def test_whole_statement(self):
        rows = self.parse(statement_pages())
        self.assertEqual(
            rows,
            [
                gcash_row(
                    "2026-10-01 09:00 AM",
                    "Received GCash from Test Sender",
                    "1000000000001",
                    credit="1,000.00",
                    balance="1,500.00",
                ),
                gcash_row(
                    "2026-10-02 06:50 PM",
                    "Payment to Test Shop, Merchant Transaction Number: X1",
                    "1000000000002",
                    debit="250.50",
                    balance="1,249.50",
                ),
                gcash_row(
                    "2026-10-03 08:15 AM",
                    "Payment to Test Cafe",
                    "1000000000003",
                    debit="100.00",
                    balance="1,149.50",
                ),
            ],
        )

    def test_totals_mismatch(self):
        with self.assertRaisesRegex(
            StatementError, "parsed total debit 350.50 != statement total 999.00"
        ):
            self.parse(statement_pages(total_debit="999.00"))

    def test_ending_balance_off_adds_missing_row(self):
        rows = self.parse(statement_pages(ending="1100.00"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["Description"], gcash.MISSING_DESCRIPTION)
        self.assertEqual(rows[-1]["Debit"], "49.50")

    def test_no_table(self):
        self.assertEqual(self.parse([FakePage(words_from("Nothing here", 20, 50))]), [])


class CheckTotalsTest(unittest.TestCase):
    ROWS = [
        gcash_row("2026-10-01 09:00 AM", "In", "1000000000001", credit="1,000.00", balance="1"),
        gcash_row("2026-10-01 09:00 AM", "Out", "1000000000002", debit="250.50", balance="1"),
    ]

    def test_matching_totals(self):
        gcash._check_totals(self.ROWS, {"debit": Decimal("250.50"), "credit": Decimal("1000.00")})

    def test_missing_totals_are_skipped(self):
        gcash._check_totals(self.ROWS, {})
        gcash._check_totals(self.ROWS, {"credit": Decimal("1000.00")})

    def test_mismatch(self):
        with self.assertRaisesRegex(
            StatementError, "parsed total credit 1000.00 != statement total 900.00"
        ):
            gcash._check_totals(self.ROWS, {"credit": Decimal("900.00")})


# =============================================================================
# BALANCE CORRECTION
# =============================================================================


def balances(starting=None, ending=None):
    result = {}
    if starting is not None:
        result["starting"] = Decimal(starting)
    if ending is not None:
        result["ending"] = Decimal(ending)
    return result


class CorrectBalancesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(gcash._correct_balances([], balances("0", "0")), [])

    def test_statement_that_adds_up_is_unchanged(self):
        rows = [
            gcash_row(
                "2026-10-01 09:00 AM",
                "Received GCash",
                "1000000000001",
                credit="1,000.00",
                balance="1,500.00",
            ),
            gcash_row(
                "2026-10-02 09:00 AM",
                "Payment to Test Shop",
                "1000000000002",
                debit="250.50",
                balance="1,249.50",
            ),
        ]
        self.assertEqual(gcash._correct_balances(rows, balances("500", "1249.50")), rows)

    def test_zero_balance_debit_is_split(self):
        row = gcash_row(
            "2026-10-01 09:00 AM",
            "Payment to Test Rides",
            "1000000000010",
            debit="100.00",
            balance="0.00",
        )
        result = gcash._correct_balances([row], balances("500", "500"))
        self.assertEqual(
            result,
            [
                # the statement leg keeps its Reference No.
                gcash_row(
                    "2026-10-01 09:00 AM",
                    "Payment to Test Rides",
                    "1000000000010",
                    debit="100.00",
                    balance="400.00",
                ),
                gcash_row(
                    "2026-10-01 09:00 AM",
                    "Refund from Test Rides",
                    "R1000000000010",
                    credit="100.00",
                    balance="500.00",
                ),
            ],
        )

    def test_zero_balance_credit_is_split(self):
        row = gcash_row(
            "2026-10-01 09:00 AM", "Test Rides", "1000000000011", credit="80.00", balance="0.00"
        )
        result = gcash._correct_balances([row], balances("500", "500"))
        self.assertEqual(
            result,
            [
                gcash_row(
                    "2026-10-01 09:00 AM",
                    "Payment to Test Rides",
                    "P1000000000011",
                    debit="80.00",
                    balance="420.00",
                ),
                gcash_row(
                    "2026-10-01 09:00 AM",
                    "Refund from Test Rides",
                    "1000000000011",
                    credit="80.00",
                    balance="500.00",
                ),
            ],
        )

    def test_real_zero_balance_is_not_split(self):
        rows = [
            gcash_row(
                "2026-10-01 09:00 AM",
                "Payment to Test Shop",
                "1000000000001",
                debit="100.00",
                balance="0.00",
            )
        ]
        self.assertEqual(gcash._correct_balances(rows, balances("100", "0")), rows)

    def test_first_row_gap_corrects_the_starting_balance(self):
        row = gcash_row(
            "2026-10-01 09:00 AM",
            "Received GCash",
            "1000000000001",
            credit="1,000.00",
            balance="1,600.00",
        )
        result = gcash._correct_balances([row], balances("500"))
        self.assertEqual(
            result,
            [
                gcash_row(
                    "2026-10-01 09:00 AM",
                    gcash.START_DESCRIPTION,
                    "S1000000000001",
                    credit="100.00",
                    balance="600.00",
                ),
                row,
            ],
        )

    def test_first_row_gap_can_be_money_out(self):
        row = gcash_row(
            "2026-10-01 09:00 AM",
            "Received GCash",
            "1000000000001",
            credit="1,000.00",
            balance="1,400.00",
        )
        start = gcash._correct_balances([row], balances("500"))[0]
        self.assertEqual(
            (start["Debit"], start["Credit"], start["Balance"]), ("100.00", "", "400.00")
        )

    def later_gap(self, *rows):
        gap_row = gcash_row(
            "2026-10-05 10:00 AM",
            "Received GCash from Test Sender",
            "1000000000009",
            credit="10.00",
            balance="420.00",
        )
        result = gcash._correct_balances([*rows, gap_row], balances("500"))
        self.assertEqual(result[-1], gap_row)
        return result[-2]

    def test_later_gap_refund_named_after_the_last_payment(self):
        refund = self.later_gap(
            gcash_row(
                "2026-10-01 09:00 AM",
                "Payment to Test Shop, Merchant Transaction Number: X1",
                "1000000000001",
                debit="100.00",
                balance="400.00",
            ),
        )
        self.assertEqual(
            refund,
            gcash_row(
                "2026-10-05 10:00 AM",
                "Refund from Test Shop",
                "R1000000000009",
                credit="10.00",
                balance="410.00",
            ),
        )

    def test_later_gap_refund_prefers_the_last_split_payee(self):
        refund = self.later_gap(
            gcash_row(
                "2026-10-01 09:00 AM",
                "Payment to Test Shop",
                "1000000000001",
                debit="100.00",
                balance="400.00",
            ),
            gcash_row(
                "2026-10-02 09:00 AM",
                "Payment to Test Rides",
                "1000000000002",
                debit="50.00",
                balance="0.00",
            ),
        )
        self.assertEqual(refund["Description"], "Refund from Test Rides")

    def test_later_gap_refund_without_payments_uses_the_row(self):
        refund = self.later_gap(
            gcash_row(
                "2026-10-01 09:00 AM",
                "Cash in from Test Bank",
                "1000000000001",
                credit="100.00",
                balance="600.00",
            ),
            gcash_row(
                "2026-10-02 09:00 AM",
                "Cash out to Test Bank",
                "1000000000002",
                debit="200.00",
                balance="400.00",
            ),
        )
        self.assertEqual(refund["Description"], "Refund from Received GCash from Test Sender")

    def test_money_missing_becomes_one_row_at_the_end(self):
        rows = [
            gcash_row(
                "2026-10-01 09:00 AM",
                "Received GCash",
                "1000000000001",
                credit="100.00",
                balance="600.00",
            ),
            gcash_row(
                "2026-10-02 09:00 AM",
                "Payment to Test Shop",
                "1000000000002",
                debit="100.00",
                balance="450.00",
            ),
        ]
        result = gcash._correct_balances(rows, balances("500", "450"))
        self.assertEqual(result[:2], rows)
        self.assertEqual(
            result[2],
            gcash_row(
                "2026-10-02 09:00 AM",
                gcash.MISSING_DESCRIPTION,
                gcash._missing_reference(rows),
                debit="50.00",
                balance="450.00",
            ),
        )

    def test_without_starting_balance_the_first_row_is_not_checked(self):
        rows = [
            gcash_row(
                "2026-10-01 09:00 AM",
                "Received GCash",
                "1000000000001",
                credit="100.00",
                balance="999.00",
            )
        ]
        self.assertEqual(gcash._correct_balances(rows, balances(ending="0")), rows)

    def test_without_starting_balance_the_second_row_is_checked(self):
        rows = [
            gcash_row(
                "2026-10-01 09:00 AM",
                "Received GCash",
                "1000000000001",
                credit="100.00",
                balance="1,500.00",
            ),
            gcash_row(
                "2026-10-02 09:00 AM",
                "Payment to Test Shop",
                "1000000000002",
                debit="250.50",
                balance="1,250.00",
            ),
        ]
        result = gcash._correct_balances(rows, balances())
        self.assertEqual(result[1]["Reference No."], "S1000000000002")
        self.assertEqual(result[1]["Credit"], "0.50")


class MissingReferenceTest(unittest.TestCase):
    ROWS = [gcash_row("d", "a", "1000000000001"), gcash_row("d", "b", "1000000000002")]

    def test_format(self):
        reference = gcash._missing_reference(self.ROWS)
        self.assertRegex(reference, r"^M0000\d{9}$")
        self.assertTrue(gcash.is_correction_row(gcash_row("d", "x", reference)))

    def test_same_statement_same_number(self):
        self.assertEqual(
            gcash._missing_reference(self.ROWS),
            gcash._missing_reference([dict(r) for r in self.ROWS]),
        )

    def test_depends_on_first_and_last_reference(self):
        other_last = [self.ROWS[0], gcash_row("d", "b", "1000000000003")]
        other_first = [gcash_row("d", "a", "1000000000000"), self.ROWS[1]]
        reference = gcash._missing_reference(self.ROWS)
        self.assertNotEqual(reference, gcash._missing_reference(other_last))
        self.assertNotEqual(reference, gcash._missing_reference(other_first))


# =============================================================================
# CORRECTION ROWS, BALANCES, SUMMARY
# =============================================================================

STATEMENT = [
    gcash_row(
        "2026-10-01 09:00 AM",
        "Received GCash from Test Sender",
        "1000000000001",
        credit="1,000.00",
        balance="1,500.00",
    ),
    gcash_row(
        "2026-10-02 10:00 AM",
        "Refund from Test Shop",
        "R1000000000002",
        credit="50.00",
        balance="1,550.00",
    ),
    gcash_row(
        "2026-10-02 10:00 AM",
        "Payment to Test Shop",
        "1000000000002",
        debit="250.50",
        balance="1,299.50",
    ),
]


class CorrectionRowsTest(unittest.TestCase):
    def test_is_correction_row(self):
        for reference in ("S1000000000001", "R1", "P1000000000001", "M0000123456789"):
            self.assertTrue(gcash.is_correction_row(gcash_row("d", "x", reference)), reference)
        for reference in ("1000000000001", "X1000000000001", "R", "RABC", "r1000000000001", ""):
            self.assertFalse(gcash.is_correction_row(gcash_row("d", "x", reference)), reference)

    def test_balance_corrections(self):
        self.assertEqual(
            gcash.balance_corrections(STATEMENT),
            [
                {
                    "date_time": "2026-10-02 10:00 AM",
                    "description": "Refund from Test Shop",
                    "reference": "R1000000000002",
                    "amount": Decimal("50.00"),
                }
            ],
        )
        money_out = gcash_row("d", "x", "M0000000000001", debit="12.00")
        self.assertEqual(gcash.balance_corrections([money_out])[0]["amount"], Decimal("-12.00"))

    def test_balance_warning(self):
        self.assertIsNone(gcash.balance_warning([STATEMENT[0], STATEMENT[2]]))
        self.assertEqual(
            gcash.balance_warning(STATEMENT),
            "Balance does not add up: 1 correction row(s) added, total +50.00. "
            "They are uploaded to YNAB, flagged red for review.\n"
            "  2026-10-02 10:00 AM  " + "Refund from Test Shop".ljust(40) + "       +50.00",
        )

    def test_statement_balances(self):
        self.assertEqual(gcash.statement_balances([]), {})
        self.assertEqual(
            gcash.statement_balances(STATEMENT),
            {"starting": Decimal("500.00"), "ending": Decimal("1299.50")},
        )
        first_out = [gcash_row("d", "x", "1000000000001", debit="100.00", balance="400.00")]
        self.assertEqual(gcash.statement_balances(first_out)["starting"], Decimal("500.00"))

    def test_summary_leaves_out_correction_rows(self):
        self.assertEqual(
            gcash.summary(STATEMENT),
            "Total debit 250.50 · Total credit 1,000.00 · matches statement ✓",
        )


# =============================================================================
# CONVERTING - rows -> neutral Transactions
# =============================================================================

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


class ToTransactionsTest(unittest.TestCase):
    def test_money_out(self):
        self.assertEqual(
            gcash.to_transactions([PAYMENT]),
            [
                Transaction(
                    source="gcash",
                    key="1000000000002",
                    date=datetime(2026, 10, 5, 8, 15),
                    payee="Test Shop",
                    amount=Decimal("-249.00"),
                    memo="GCash Ref: 1000000000002",
                    flagged=False,
                )
            ],
        )

    def test_money_in(self):
        [t] = gcash.to_transactions([RECEIVED])
        self.assertEqual(t.amount, Decimal("1000.00"))
        self.assertEqual(t.payee, "Received GCash from Test Sender")
        self.assertEqual(t.date, datetime(2026, 10, 1, 21, 0))

    def test_correction_rows_are_flagged(self):
        row = gcash_row(
            "2026-10-02 10:00 AM", "Refund from Test Shop", "R1000000000002", credit="50.00"
        )
        [t] = gcash.to_transactions([row])
        self.assertTrue(t.flagged)
        self.assertEqual(t.memo, "GCash Ref: R1000000000002")
        self.assertEqual(t.key, "R1000000000002")

    def test_key_is_only_the_reference(self):
        # The key becomes YNAB's import_id, so later fixes to the amount,
        # payee or date must not change it
        changed = {
            **PAYMENT,
            "Debit": "600.00",
            "Description": "Payment to Other Shop",
            "Date and Time": "2026-10-21 07:00 AM",
        }
        [before, after] = gcash.to_transactions([PAYMENT, changed])
        self.assertEqual((before.source, before.key), (after.source, after.key))

    def test_ynab_format_is_unchanged(self):
        # The same dict the old transform.gcash_to_ynab() gave, import_id included
        self.assertEqual(
            to_ynab(gcash.to_transactions([PAYMENT])[0]),
            {
                "date": "2026-10-05",
                "payee_name": "Test Shop",
                "import_id": "ETLPH:707f7b0d95f2bb43995f608f704d67",
                "amount": -249000,
                "memo": "GCash Ref: 1000000000002",
                "flag_color": None,
            },
        )

    def test_one_per_row_in_order(self):
        keys = [t.key for t in gcash.to_transactions([PAYMENT, RECEIVED])]
        self.assertEqual(keys, ["1000000000002", "1000000000001"])
        self.assertEqual(gcash.to_transactions([]), [])


# =============================================================================
# PDF PASSWORD
# =============================================================================


class BuildPasswordTest(unittest.TestCase):
    def test_last_name_and_mobile_last4(self):
        self.assertEqual(gcash.build_password("Dela Cruz", "1234"), "delacruz1234")

    def test_input_dict(self):
        self.assertEqual(
            gcash.build_password({"last_name": "Dela Cruz", "mobile_last4": "1234"}),
            "delacruz1234",
        )

    def test_strips_all_spaces_and_lowercases(self):
        self.assertEqual(gcash.build_password("  De La   Rosa ", "0099"), "delarosa0099")

    def test_missing_mobile_last4_raises(self):
        with self.assertRaises(TypeError):
            gcash.build_password("Dela Cruz")
        with self.assertRaises(KeyError):
            gcash.build_password({"last_name": "Dela Cruz"})


# =============================================================================
# COMMAND LINE
# =============================================================================


class MainTest(unittest.TestCase):
    def run_main(self, *args, rows=STATEMENT):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.argv", ["gcash", "statement.pdf", *args]),
            mock.patch.object(gcash, "parse_transactions", return_value=rows) as parse,
            mock.patch.object(gcash.getpass, "getpass", return_value="typed") as prompt,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            gcash.main()
        return parse, prompt, stdout.getvalue(), stderr.getvalue()

    def test_prints_json(self):
        parse, prompt, out, err = self.run_main("-p", "secret")
        parse.assert_called_once_with("statement.pdf", "secret")
        prompt.assert_not_called()
        self.assertEqual(json.loads(out), STATEMENT)
        self.assertIn("WARNING: Balance does not add up", err)

    def test_prompts_for_password(self):
        parse, prompt, _, _ = self.run_main()
        prompt.assert_called_once()
        parse.assert_called_once_with("statement.pdf", "typed")

    def test_writes_file_without_warning(self):
        rows = [STATEMENT[0]]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "rows.json")
            _, _, out, err = self.run_main("-p", "", "-o", path, rows=rows)
            with open(path) as f:
                self.assertEqual(json.load(f), rows)
        self.assertEqual(out, "")
        self.assertEqual(err, f"Wrote 1 transactions to {path}\n")


if __name__ == "__main__":
    unittest.main()
