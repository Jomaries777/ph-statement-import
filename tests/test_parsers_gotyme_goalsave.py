"""
Tests for parsers/gotyme_goalsave.py: one section per GoalSave account, each
read with GoTyme Everyday's page helpers and checked on its own, and the CLI.

Run from the project folder:  python -m unittest discover tests

The PDF is faked: pages return word positions like pdfplumber does (see
tests/fakes.py and the GoTyme helpers in tests/test_parsers_gotyme.py), so no
real statement is needed. All data here is made up.
"""

import contextlib
import io
import json
import os
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

from statement_etl.parsers import gotyme_goalsave
from statement_etl.parsers.common import StatementError
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import make_import_id
from tests.fakes import FakePage, FakePDF, words_from
from tests.test_parsers_gotyme import (
    gotyme_footer,
    gotyme_header,
    gotyme_line,
    gotyme_row,
    gotyme_summary_box,
)

# =============================================================================
# A FAKE GOALSAVE PDF - one page per account, like the real one
# =============================================================================


def goalsave_page(account, lines, opening, total_credit, total_debit, closing, page=1, pages=2):
    """One GoalSave account's page: summary box, GoalSave No., table, footer."""
    return FakePage(
        words_from("Statement of Account", 15.0, 77.3)
        + gotyme_summary_box(
            opening=opening, total_credit=total_credit, total_debit=total_debit, closing=closing
        )
        + words_from(f"GoalSave No.: *******{account}", 15.0, 168.8)
        + gotyme_header(207.3)
        + [word for top, line in enumerate(lines) for word in gotyme_line(228.0 + 20 * top, *line)]
        + gotyme_footer(page, pages)
    )


def two_accounts(last_balance="42,200.00"):
    """Fake 2-page GoalSave PDF: account 11111 on page 1, 22222 on page 2."""
    return [
        goalsave_page(
            "11111",
            [("18 Oct 2026", "Transfer from Deposit account", "2,500.00", "0.00", "2,500.00")],
            opening="0.00",
            total_credit="2,500.00",
            total_debit="0.00",
            closing="2,500.00",
            page=1,
        ),
        goalsave_page(
            "22222",
            [
                ("01 Oct 2026", "Earned interest", "200.00", "0.00", "50,200.00"),
                ("04 Oct 2026", "Transfer to Deposit account", "0.00", "8,000.00", last_balance),
            ],
            opening="50,000.00",
            total_credit="200.00",
            total_debit="8,000.00",
            closing="42,200.00",
            page=2,
        ),
    ]


def goalsave_row(account, *args, **kwargs):
    """A parsed GoalSave row dict, as parse_transactions() returns it."""
    return {"GoalSave No.": account, **gotyme_row(*args, **kwargs)}


TWO_ACCOUNT_ROWS = [
    goalsave_row(
        "11111",
        "18 Oct 2026",
        "Transfer from Deposit account",
        credits="2500.00",
        balance="2500.00",
    ),
    goalsave_row("22222", "01 Oct 2026", "Earned interest", credits="200.00", balance="50200.00"),
    goalsave_row(
        "22222",
        "04 Oct 2026",
        "Transfer to Deposit account",
        debits="8000.00",
        balance="42200.00",
    ),
]


# =============================================================================
# PARSING
# =============================================================================


class ParseTransactionsTest(unittest.TestCase):
    def parse(self, pages, on_page=None):
        with mock.patch.object(gotyme_goalsave, "open_pdf", return_value=FakePDF(pages)) as opened:
            rows = gotyme_goalsave.parse_transactions("savings.pdf", "01311990", on_page)
        opened.assert_called_once_with("savings.pdf", "01311990")
        return rows

    def test_each_account_section_in_pdf_order(self):
        rows = self.parse(two_accounts())
        self.assertEqual(rows, TWO_ACCOUNT_ROWS)
        for row in rows:
            self.assertEqual(tuple(row), gotyme_goalsave.COLUMNS)

    def test_reports_each_page_as_it_is_read(self):
        on_page = mock.Mock()
        self.parse(two_accounts(), on_page)
        self.assertEqual(on_page.call_args_list, [mock.call(1, 2), mock.call(2, 2)])

    def test_an_account_that_runs_over_a_page(self):
        # Page 2 has no GoalSave No.: its rows belong to the account above
        first = goalsave_page(
            "22222",
            [("01 Oct 2026", "Earned interest", "200.00", "0.00", "50,200.00")],
            opening="50,000.00",
            total_credit="200.00",
            total_debit="40.00",
            closing="50,160.00",
        )
        more = FakePage(
            gotyme_header(111.5)
            + gotyme_line(
                132.3,
                "01 Oct 2026",
                "Withholding tax from earned interest",
                debits="40.00",
                balance="50,160.00",
            )
            + gotyme_footer(2, 2)
        )
        rows = self.parse([first, more])
        self.assertEqual([r["GoalSave No."] for r in rows], ["22222", "22222"])
        self.assertEqual(rows[1]["Details"], "Withholding tax from earned interest")

    def test_each_account_is_checked_on_its_own(self):
        with self.assertRaisesRegex(
            StatementError,
            r"^GoalSave No\. 22222: running balance check failed on 04 Oct 2026: "
            r"expected 42200\.00, got 76000\.00$",
        ):
            self.parse(two_accounts(last_balance="76,000.00"))

    def test_no_goalsave_account_found(self):
        page = FakePage(words_from("Got questions about your statement?", 15.0, 132.9))
        with self.assertRaisesRegex(StatementError, "No GoalSave account found"):
            self.parse([page])

    def test_a_table_before_any_goalsave_number(self):
        page = FakePage(
            gotyme_header(111.5)
            + gotyme_line(132.3, "01 Oct 2026", "Earned interest", "1.00", "0.00", "1.00")
        )
        with self.assertRaisesRegex(StatementError, "Page 1 has transactions but no GoalSave No."):
            self.parse([page])


# =============================================================================
# BALANCES & SUMMARY - one account's rows
# =============================================================================


class BalancesAndSummaryTest(unittest.TestCase):
    def test_one_accounts_balances(self):
        self.assertEqual(
            gotyme_goalsave.statement_balances(TWO_ACCOUNT_ROWS[1:]),
            {"starting": Decimal("50000.00"), "ending": Decimal("42200.00")},
        )

    def test_summary(self):
        self.assertEqual(
            gotyme_goalsave.summary(TWO_ACCOUNT_ROWS[1:]),
            "Total credit 200.00 · Total debit 8,000.00 · matches statement ✓",
        )


# =============================================================================
# CONVERTING - rows -> neutral Transactions
# =============================================================================


class ToTransactionsTest(unittest.TestCase):
    def test_rows_to_transactions(self):
        key = "22222|01 Oct 2026|200.00|0.00|50200.00|0"
        [txn] = gotyme_goalsave.to_transactions([TWO_ACCOUNT_ROWS[1]])
        self.assertEqual(
            txn,
            Transaction(
                source="gotyme_goalsave",
                key=key,
                date=date(2026, 10, 1),
                payee="Earned interest",
                amount=Decimal("200.00"),
                memo=txn.memo,
                flagged=False,
            ),
        )
        # The memo's Ref is the hash part of the import_id, as for GoTyme Everyday
        ref = txn.memo.removeprefix("GoTyme Ref: ")
        self.assertEqual("ETLPH:" + ref, make_import_id("gotyme_goalsave", key))

    def test_outflow_is_negative(self):
        [txn] = gotyme_goalsave.to_transactions([TWO_ACCOUNT_ROWS[2]])
        self.assertEqual(txn.amount, Decimal("-8000.00"))

    def test_same_row_in_two_accounts_gets_two_keys(self):
        row = TWO_ACCOUNT_ROWS[1]
        first, second, third = gotyme_goalsave.to_transactions(
            [row, {**row, "GoalSave No.": "11111"}, dict(row)]
        )
        self.assertEqual(first.key, "22222|01 Oct 2026|200.00|0.00|50200.00|0")
        self.assertEqual(second.key, "11111|01 Oct 2026|200.00|0.00|50200.00|0")
        self.assertEqual(third.key, "22222|01 Oct 2026|200.00|0.00|50200.00|1")


# =============================================================================
# COMMAND LINE
# =============================================================================


class MainTest(unittest.TestCase):
    def run_main(self, *args, env=None):
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, env or {}, clear=True),
            mock.patch("sys.argv", ["gotyme_goalsave", "savings.pdf", *args]),
            mock.patch.object(
                gotyme_goalsave, "parse_transactions", return_value=TWO_ACCOUNT_ROWS
            ) as parse,
            mock.patch.object(gotyme_goalsave.getpass, "getpass", return_value="typed") as prompt,
            contextlib.redirect_stdout(stdout),
        ):
            gotyme_goalsave.main()
        return parse, prompt, stdout.getvalue()

    def test_prints_json_with_password_flag(self):
        parse, prompt, out = self.run_main("-p", "01311990")
        parse.assert_called_once_with("savings.pdf", "01311990")
        prompt.assert_not_called()
        self.assertEqual(json.loads(out), TWO_ACCOUNT_ROWS)

    def test_reads_the_gotyme_password_from_env(self):
        parse, _, _ = self.run_main(env={"GOTYME_PDF_PASSWORD": "01311990"})
        parse.assert_called_once_with("savings.pdf", "01311990")

    def test_prompts_for_password_when_not_set(self):
        parse, prompt, _ = self.run_main()
        prompt.assert_called_once()
        parse.assert_called_once_with("savings.pdf", "typed")


if __name__ == "__main__":
    unittest.main()
