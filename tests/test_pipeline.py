"""
Tests for pipeline.py: the steps the web app reuses, and the command line.

Run from the project folder:  python -m unittest discover tests

The parser and every YNAB call are mocked. All data here is made up.
"""

import contextlib
import dataclasses
import io
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import ynab

from statement_etl import pipeline
from statement_etl.convert import rows_to_transactions
from statement_etl.parsers.common import StatementError
from statement_etl.sources import SOURCES
from statement_etl.ynab_api.balance import compare_balance
from statement_etl.ynab_api.format import to_ynab
from tests.fakes import ACCOUNT_ID, PLAN_ID, gcash_row, ynab_txn

ROWS = [
    gcash_row(
        "2026-10-01 09:00 AM",
        "Received GCash from Test Sender",
        "1000000000001",
        credit="1,000.00",
        balance="1,500.00",
    ),
    gcash_row(
        "2026-10-02 06:50 PM",
        "Payment to Test Shop",
        "1000000000002",
        debit="250.50",
        balance="1,249.50",
    ),
]
CORRECTION = gcash_row(
    "2026-10-02 06:50 PM",
    "Refund from Test Shop",
    "R1000000000002",
    credit="50.00",
    balance="1,299.50",
)
TRANSACTIONS = [to_ynab(t) for t in rows_to_transactions(ROWS, "gcash")]
API_ERROR = ynab.ApiException(status=500, reason="Server Error", body='{"error": "boom"}')


def gcash_parsing(rows):
    """Replace the GCash parser with one that returns `rows`."""
    source = dataclasses.replace(SOURCES["gcash"], parse=mock.Mock(return_value=rows))
    return mock.patch.dict(SOURCES, {"gcash": source})


def capture():
    """Redirect stdout and stderr; returns (stdout, stderr, context)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(stdout))
    stack.enter_context(contextlib.redirect_stderr(stderr))
    return stdout, stderr, stack


# =============================================================================
# STEPS - used by the command line and the web app
# =============================================================================


class ParseStatementTest(unittest.TestCase):
    def test_returns_rows_and_transactions(self):
        with gcash_parsing(ROWS):
            rows, transactions = pipeline.parse_statement("statement.pdf", "secret")
            SOURCES["gcash"].parse.assert_called_once_with(
                "statement.pdf", "secret", on_page=mock.ANY
            )
        self.assertEqual(rows, ROWS)
        self.assertEqual(transactions, TRANSACTIONS)

    def test_reports_each_page_read(self):
        def parse(pdf, password, on_page=None):
            on_page(1, 2)
            on_page(2, 2)
            return ROWS

        on_step = mock.Mock()
        source = dataclasses.replace(SOURCES["gcash"], parse=parse)
        with mock.patch.dict(SOURCES, {"gcash": source}):
            pipeline.parse_statement("statement.pdf", "secret", on_step=on_step)
        self.assertEqual(
            on_step.call_args_list,
            [
                mock.call(pipeline.STEP_READ, {"page": 1, "pages": 2}),
                mock.call(pipeline.STEP_READ, {"page": 2, "pages": 2}),
            ],
        )

    def test_correction_rows_are_converted_too(self):
        with gcash_parsing(ROWS + [CORRECTION]):
            _, transactions = pipeline.parse_statement("statement.pdf", "")
        self.assertEqual(len(transactions), 3)
        self.assertEqual(transactions[-1]["flag_color"], "red")

    def test_no_transactions(self):
        for rows in ([], [CORRECTION]):
            with (
                self.subTest(rows=rows),
                gcash_parsing(rows),
                self.assertRaisesRegex(StatementError, "^no transactions found in the PDF$"),
            ):
                pipeline.parse_statement("statement.pdf", "")

    def test_unknown_source(self):
        with self.assertRaisesRegex(ValueError, "Unknown source 'nope'"):
            pipeline.parse_statement("statement.pdf", "", source="nope")

    def test_parser_errors_are_raised(self):
        source = dataclasses.replace(
            SOURCES["gcash"], parse=mock.Mock(side_effect=ValueError("Incorrect PDF password."))
        )
        with (
            mock.patch.dict(SOURCES, {"gcash": source}),
            self.assertRaisesRegex(ValueError, "Incorrect PDF password."),
        ):
            pipeline.parse_statement("statement.pdf", "wrong")


class UploadTransactionsTest(unittest.TestCase):
    def test_delegates_to_import_transactions(self):
        with mock.patch.object(pipeline, "import_transactions", return_value="result") as upload:
            self.assertEqual(
                pipeline.upload_transactions(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token"), "result"
            )
        upload.assert_called_once_with(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token")


class BalanceStepsTest(unittest.TestCase):
    def test_starting_balance_check_leaves_out_transactions_already_in_ynab(self):
        already = {TRANSACTIONS[0]["import_id"]}
        with (
            mock.patch.object(pipeline, "find_already_imported", return_value=already) as find,
            mock.patch.object(pipeline, "check_starting_balance", return_value="check") as check,
        ):
            result = pipeline.starting_balance_check(
                ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token"
            )
        self.assertEqual(result, "check")
        find.assert_called_once_with(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token")
        check.assert_called_once_with(
            Decimal("500.00"),  # statement starting balance
            Decimal("-250.5"),  # only the payment will be uploaded
            ACCOUNT_ID,
            PLAN_ID,
            "token",
            already_in_ynab=1,
            to_upload=1,
        )

    def test_starting_balance_check_with_nothing_in_ynab(self):
        with (
            mock.patch.object(pipeline, "find_already_imported", return_value=set()),
            mock.patch.object(pipeline, "check_starting_balance") as check,
        ):
            pipeline.starting_balance_check(ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID)
        self.assertEqual(check.call_args.args[1], Decimal("749.5"))
        self.assertEqual(check.call_args.kwargs, {"already_in_ynab": 0, "to_upload": 2})

    def test_new_account_starting_balance(self):
        with mock.patch.object(
            pipeline, "set_new_account_starting_balance", return_value="set"
        ) as set_balance:
            self.assertEqual(
                pipeline.new_account_starting_balance(ROWS, ACCOUNT_ID, PLAN_ID, "token"), "set"
            )
        set_balance.assert_called_once_with(Decimal("500.00"), ACCOUNT_ID, PLAN_ID, "token")

    def test_ending_balance_check(self):
        with mock.patch.object(pipeline, "check_ending_balance", return_value="check") as check:
            result = pipeline.ending_balance_check(
                ROWS, ACCOUNT_ID, PLAN_ID, "token", Decimal("10")
            )
        self.assertEqual(result, "check")
        check.assert_called_once_with(
            Decimal("1249.50"), ACCOUNT_ID, PLAN_ID, "token", Decimal("10")
        )


class FakeYnabUpload:
    """Stands in for upload_transactions(): like YNAB, it creates each
    import_id once and reports it as a duplicate after that."""

    def __init__(self):
        self.imported = set()

    def __call__(self, transactions, account_id, plan_id, access_token=None):
        new = [t for t in transactions if t["import_id"] not in self.imported]
        duplicates = [t["import_id"] for t in transactions if t["import_id"] in self.imported]
        self.imported |= {t["import_id"] for t in new}
        return SimpleNamespace(
            transactions=[ynab_txn(memo=t["memo"]) for t in new],
            duplicate_import_ids=duplicates,
        )


class ImportStatementTest(unittest.TestCase):
    def setUp(self):
        # One mock holds every step, so the tests can check the order of the calls
        self.steps = mock.Mock()
        self.steps.new_account_starting_balance.return_value = None
        self.steps.get_account_balance.return_value = Decimal("500.00")
        self.steps.upload_transactions.side_effect = FakeYnabUpload()
        self.steps.ending_balance_check.return_value = ending_check()
        for name in (
            "new_account_starting_balance",
            "get_account_balance",
            "upload_transactions",
            "ending_balance_check",
        ):
            patcher = mock.patch.object(pipeline, name, getattr(self.steps, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_import(self):
        return pipeline.import_statement(ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token")

    def test_runs_the_steps_in_order(self):
        result = self.run_import()
        self.assertEqual(
            self.steps.mock_calls,
            [
                mock.call.new_account_starting_balance(
                    ROWS, ACCOUNT_ID, PLAN_ID, "token", source="gcash"
                ),
                mock.call.get_account_balance(PLAN_ID, ACCOUNT_ID, "token"),
                mock.call.upload_transactions(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token"),
                mock.call.ending_balance_check(
                    ROWS, ACCOUNT_ID, PLAN_ID, "token", Decimal("500.00"), source="gcash"
                ),
            ],
        )
        self.assertEqual(
            (len(result.created), result.skipped, result.ynab_before), (2, 0, Decimal("500.00"))
        )
        self.assertEqual(result.ending_check, ending_check())
        self.assertIsNone(result.new_account)
        self.assertIsNone(result.ending_error)

    def test_reports_the_upload_and_the_ending_check_as_they_start(self):
        self.steps.attach_mock(mock.Mock(), "on_step")
        pipeline.import_statement(
            ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "token", on_step=self.steps.on_step
        )
        called = [c[0] for c in self.steps.mock_calls]
        self.assertEqual(
            called,
            [
                "on_step",
                "new_account_starting_balance",
                "get_account_balance",
                "upload_transactions",
                "on_step",
                "ending_balance_check",
            ],
        )
        self.assertEqual(
            self.steps.on_step.call_args_list,
            [
                mock.call(pipeline.STEP_UPLOAD, {"count": len(TRANSACTIONS)}),
                mock.call(pipeline.STEP_END_BALANCE, {}),
            ],
        )

    def test_new_account_is_set_before_the_balance_is_read(self):
        # The balance read in step 2 already includes the new Starting Balance
        self.steps.new_account_starting_balance.return_value = {
            "from": Decimal("0"),
            "to": Decimal("500.00"),
        }
        result = self.run_import()
        self.assertEqual(result.new_account, {"from": Decimal("0"), "to": Decimal("500.00")})
        called = [c[0] for c in self.steps.mock_calls]
        self.assertLess(
            called.index("new_account_starting_balance"), called.index("get_account_balance")
        )

    def test_running_it_twice_creates_nothing_the_second_time(self):
        # e.g. the web app retrying after a token refresh
        first, second = self.run_import(), self.run_import()
        self.assertEqual((len(first.created), first.skipped), (2, 0))
        self.assertEqual((second.created, second.skipped), ([], 2))

    def test_ending_mismatch_is_returned_not_raised(self):
        self.steps.ending_balance_check.return_value = ending_check(ynab_after="1149.50")
        result = self.run_import()
        self.assertFalse(result.ending_check["matches"])
        self.assertEqual(len(result.created), 2)

    def test_failed_ending_check_is_returned_not_raised(self):
        self.steps.ending_balance_check.side_effect = API_ERROR
        result = self.run_import()
        self.assertIs(result.ending_error, API_ERROR)
        self.assertIsNone(result.ending_check)
        self.assertEqual(len(result.created), 2)

    def test_errors_before_and_during_the_upload_are_raised(self):
        for step in ("new_account_starting_balance", "get_account_balance", "upload_transactions"):
            with self.subTest(step=step):
                self.steps.reset_mock()
                getattr(self.steps, step).side_effect = API_ERROR
                with self.assertRaises(ynab.ApiException):
                    self.run_import()
                getattr(self.steps, step).side_effect = (
                    FakeYnabUpload() if step == "upload_transactions" else None
                )
                self.steps.ending_balance_check.assert_not_called()


# =============================================================================
# OUTPUT HELPERS
# =============================================================================


class PrintTableTest(unittest.TestCase):
    def table(self, *args, **kwargs):
        stdout, _, stack = capture()
        with stack:
            pipeline._print_table(*args, **kwargs)
        return stdout.getvalue()

    def test_no_rows_prints_nothing(self):
        self.assertEqual(self.table(("A", "B"), []), "")

    def test_aligned_and_clipped(self):
        output = self.table(
            ("Name", "Amount"),
            [("a", "1.00"), ("abcdefghijkl", "100.00"), (None, "")],
            right_aligned=("Amount",),
            max_width=10,
        )
        self.assertEqual(
            output,
            "\n".join(
                [
                    "",
                    "Name        Amount",
                    "----------  ------",
                    "a             1.00",
                    "abcdefghi…  100.00",
                    "                  ",
                    "",
                    "",
                ]
            ),
        )


class FailTest(unittest.TestCase):
    def test_prints_error_and_exits(self):
        _, stderr, stack = capture()
        with stack, self.assertRaises(SystemExit) as exit:
            pipeline._fail("boom")
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "ERROR: boom\n")


# =============================================================================
# COMMAND-LINE RUN
# =============================================================================


def starting_check(ynab_before="500.00", new_account=None, already=0):
    before = Decimal(ynab_before)
    return compare_balance(
        "starting",
        Decimal("500.00"),
        before,
        before + Decimal("749.50"),
        new_account,
        already,
        2 - already,
    )


def ending_check(ynab_after="1249.50", ynab_before=Decimal("500.00")):
    return compare_balance("ending", Decimal("1249.50"), ynab_before, Decimal(ynab_after))


UPLOAD_RESULT = SimpleNamespace(
    transactions=[
        ynab_txn(
            var_date=date(2026, 10, 2),
            payee_name="Test Shop",
            category_name=None,
            memo="GCash Ref: 1000000000002",
            amount=-250500,
        )
    ],
    duplicate_import_ids=["ETLPH:already"],
)


class RunPipelineTest(unittest.TestCase):
    def setUp(self):
        self.parse = self.patch("parse_statement", return_value=(ROWS, TRANSACTIONS))
        self.start = self.patch("starting_balance_check", return_value=starting_check())
        self.new_account = self.patch("new_account_starting_balance", return_value=None)
        self.balance = self.patch("get_account_balance", return_value=Decimal("500.00"))
        self.upload = self.patch("upload_transactions", return_value=UPLOAD_RESULT)
        self.end = self.patch("ending_balance_check", return_value=ending_check())

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(pipeline, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_pipeline(self, dry_run=False):
        stdout, stderr, stack = capture()
        with stack:
            result = pipeline.run_pipeline("statement.pdf", "secret", ACCOUNT_ID, PLAN_ID, dry_run)
        return result, stdout.getvalue(), stderr.getvalue()

    def run_failing(self):
        stdout, stderr, stack = capture()
        with stack, self.assertRaises(SystemExit) as exit:
            pipeline.run_pipeline("statement.pdf", "secret", ACCOUNT_ID, PLAN_ID)
        self.assertEqual(exit.exception.code, 1)
        return stdout.getvalue(), stderr.getvalue()

    def test_dry_run_only_parses(self):
        result, out, _ = self.run_pipeline(dry_run=True)
        self.assertEqual(result, TRANSACTIONS)
        self.parse.assert_called_once_with("statement.pdf", "secret", "gcash")
        for text in (
            "Uploading GCash Statement...",
            "STEP 1: PARSE",
            "Received GCash from Test Sender",
            "Parsing successful: 2 transactions from statement.pdf",
            "Total debit 250.50 · Total credit 1,000.00 · matches statement ✓",
            "Statement balance: starting 500.00, ending 1,249.50",
            "Dry run: nothing was sent to YNAB",
        ):
            self.assertIn(text, out)
        self.assertNotIn("WARNING", out)
        self.assertNotIn("STEP 2", out)
        for step in (self.start, self.new_account, self.balance, self.upload, self.end):
            step.assert_not_called()

    def test_balance_warning_and_correction_count(self):
        self.parse.return_value = (ROWS + [CORRECTION], TRANSACTIONS)
        _, out, _ = self.run_pipeline(dry_run=True)
        self.assertIn("Parsing successful: 2 transactions", out)
        self.assertIn("WARNING: Balance does not add up: 1 correction row(s) added", out)

    def test_parse_failure_exits(self):
        self.parse.side_effect = ValueError("Incorrect PDF password.")
        _, err = self.run_failing()
        self.assertEqual(err, "ERROR: Parsing failed: Incorrect PDF password.\n")
        self.start.assert_not_called()

    def test_full_run(self):
        result, out, _ = self.run_pipeline()
        self.assertEqual(result, TRANSACTIONS)
        self.start.assert_called_once_with(ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID, source="gcash")
        # import_statement(); None = the token from .env
        self.new_account.assert_called_once_with(ROWS, ACCOUNT_ID, PLAN_ID, None, source="gcash")
        self.balance.assert_called_once_with(PLAN_ID, ACCOUNT_ID, None)
        self.upload.assert_called_once_with(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, None)
        self.end.assert_called_once_with(
            ROWS, ACCOUNT_ID, PLAN_ID, None, Decimal("500.00"), source="gcash"
        )
        for text in (
            "STEP 2: STARTING BALANCE\nStarting balance matches YNAB: 500.00",
            "STEP 3: IMPORT",
            "2026-10-02  Test Shop  Uncategorized  GCash Ref: 1000000000002  -250.50",
            "Import successful: 1 transactions created in YNAB (unapproved)",
            "Skipped 1 already imported",
            "Review and approve them in YNAB.",
            "STEP 4: ENDING BALANCE\nEnding balance matches YNAB: 1,249.50",
        ):
            self.assertIn(text, out)
        self.assertNotIn("WARNING", out)

    def test_balance_mismatches_are_warnings(self):
        self.start.return_value = starting_check(ynab_before="400.00")
        self.end.return_value = ending_check(ynab_after="1149.50")
        _, out, _ = self.run_pipeline()
        self.assertIn("WARNING: Starting balance does not match YNAB. You can still import.", out)
        self.assertIn("WARNING: Ending balance does not match YNAB.", out)
        self.upload.assert_called_once()

    def test_already_in_ynab_is_a_warning(self):
        self.start.return_value = starting_check(already=1)
        _, out, _ = self.run_pipeline()
        self.assertIn("WARNING: 1 transactions already in YNAB. Only 1 will be uploaded.", out)

    def test_nothing_created(self):
        self.upload.return_value = SimpleNamespace(transactions=None, duplicate_import_ids=None)
        _, out, _ = self.run_pipeline()
        self.assertIn("Import successful: 0 transactions created in YNAB (unapproved)", out)
        self.assertNotIn("Skipped", out)
        self.assertNotIn("Review and approve", out)

    def test_new_account_gets_its_starting_balance_set(self):
        self.start.return_value = starting_check(
            ynab_before="0.00", new_account={"current": Decimal("0")}
        )
        self.new_account.return_value = {"from": Decimal("0"), "to": Decimal("500.00")}
        _, out, _ = self.run_pipeline()
        self.new_account.assert_called_once_with(ROWS, ACCOUNT_ID, PLAN_ID, None, source="gcash")
        self.assertIn("New YNAB account: Starting Balance set from 0.00 to 500.00", out)
        # the balance read after the Starting Balance was set
        self.assertEqual(self.end.call_args.args[4], Decimal("500.00"))

    def test_new_account_that_got_other_transactions_is_compared_normally(self):
        self.start.return_value = starting_check(
            ynab_before="0.00", new_account={"current": Decimal("0")}
        )
        self.new_account.return_value = None
        self.balance.return_value = Decimal("0.00")
        _, out, _ = self.run_pipeline()
        self.assertIn("WARNING: Starting balance does not match YNAB. You can still import.", out)
        self.assertNotIn("New YNAB account", out)
        self.assertEqual(self.end.call_args.args[4], Decimal("0.00"))

    def test_starting_check_error_exits(self):
        self.start.side_effect = API_ERROR
        _, err = self.run_failing()
        self.assertIn("ERROR: Could not read the YNAB balance: YNAB returned 500 Server Error", err)
        self.upload.assert_not_called()

    def test_new_account_error_exits(self):
        self.start.return_value = starting_check(new_account={"current": Decimal("0")})
        self.new_account.side_effect = API_ERROR
        _, err = self.run_failing()
        # import_statement() raises it like any YNAB error before the upload
        self.assertIn("ERROR: Import failed: YNAB returned 500 Server Error", err)
        self.upload.assert_not_called()

    def test_import_error_exits(self):
        self.upload.side_effect = API_ERROR
        _, err = self.run_failing()
        self.assertIn(
            'ERROR: Import failed: YNAB returned 500 Server Error\n{"error": "boom"}', err
        )
        self.end.assert_not_called()

    def test_ending_check_error_is_only_a_warning(self):
        self.end.side_effect = API_ERROR
        result, out, _ = self.run_pipeline()
        self.assertEqual(result, TRANSACTIONS)
        self.assertIn(
            "WARNING: could not read the YNAB balance after the upload:"
            " YNAB returned 500 Server Error",
            out,
        )


class MainTest(unittest.TestCase):
    def run_main(self, args, env):
        _, stderr, stack = capture()
        with (
            stack,
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("sys.argv", ["pipeline", *args]),
            mock.patch.object(pipeline, "run_pipeline") as run,
        ):
            try:
                pipeline.main()
                code = None
            except SystemExit as exit:
                code = exit.code
        return run, code, stderr.getvalue()

    FULL_ENV = {
        "GCASH_PDF_PASSWORD": "secret",
        "BEARER_TOKEN": "token",
        "YNAB_ACCOUNT_ID": ACCOUNT_ID,
        "YNAB_PLAN_ID": PLAN_ID,
    }

    def test_dry_run_needs_only_the_password(self):
        run, code, _ = self.run_main(
            ["statement.pdf", "--dry-run"], {"GCASH_PDF_PASSWORD": "secret"}
        )
        self.assertIsNone(code)
        run.assert_called_once_with("statement.pdf", "secret", None, None, True, "gcash")

    def test_full_run_reads_ids_from_env(self):
        run, code, _ = self.run_main(["statement.pdf"], self.FULL_ENV)
        self.assertIsNone(code)
        run.assert_called_once_with("statement.pdf", "secret", ACCOUNT_ID, PLAN_ID, False, "gcash")

    def test_command_line_ids_win(self):
        run, _, _ = self.run_main(
            [
                "statement.pdf",
                "--source",
                "gcash",
                "--plan-id",
                "plan-x",
                "--account-id",
                "account-x",
            ],
            self.FULL_ENV,
        )
        run.assert_called_once_with(
            "statement.pdf", "secret", "account-x", "plan-x", False, "gcash"
        )

    def test_missing_settings(self):
        cases = {
            "YNAB_ACCOUNT_ID": "--account-id or YNAB_ACCOUNT_ID is required",
            "YNAB_PLAN_ID": "--plan-id or YNAB_PLAN_ID is required",
            "BEARER_TOKEN": "BEARER_TOKEN environment variable is not set",
            "GCASH_PDF_PASSWORD": "GCASH_PDF_PASSWORD environment variable is not set",
        }
        for missing, message in cases.items():
            with self.subTest(missing=missing):
                env = {k: v for k, v in self.FULL_ENV.items() if k != missing}
                run, code, err = self.run_main(["statement.pdf"], env)
                self.assertEqual(code, 2)
                self.assertIn(message, err)
                run.assert_not_called()

    def test_unknown_source(self):
        run, code, err = self.run_main(
            ["statement.pdf", "--source", "nope", "--dry-run"], self.FULL_ENV
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid choice: 'nope'", err)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
