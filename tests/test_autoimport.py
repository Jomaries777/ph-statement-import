"""
Tests for autoimport/runner.py: each poll finds new statement emails in Gmail,
imports them once into the mapped YNAB account, and records a run.

Run from the project folder:  python -m unittest discover tests

Gmail is FakeGmail below, a made-up inbox whose GCash PDFs come from
tests/e2e/make_pdf.py, so the real GCash parser reads them. The GoTyme tests
use a stand-in parser instead (make_pdf only makes GCash statements): they
check which of an email's PDFs is parsed, with which password. The store is
FakeStore (tests/fakes.py). YNAB is faked at the edges: ynab_oauth.refresh()
and pipeline.import_statement(). All values here are made up.
"""

import base64
import dataclasses
import os
import re
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import ynab

from statement_etl import pipeline, sources
from statement_etl.autoimport import gmail, runner
from statement_etl.parsers.common import PasswordError
from tests.e2e import make_pdf
from tests.fakes import ACCOUNT_ID, ENCRYPTION_KEY, PLAN_ID, FakeStore, ynab_txn
from tests.test_parsers_gotyme_goalsave import TWO_ACCOUNT_ROWS

SUB = "100000000000000000001"  # a Google account ID ("sub")
OTHER_SUB = "100000000000000000002"
EMAIL = "juan@example.test"
GMAIL_REFRESH_TOKEN = "1//made-up-gmail-refresh-token"
GMAIL_ACCESS_TOKEN = "made-up-gmail-access-token"
INPUTS = {"last_name": "Dela Cruz", "mobile_last4": "1234"}
PASSWORD = "delacruz1234"  # what gcash.build_password() makes of INPUTS
OTHER_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"

GCASH_QUERY = sources.SOURCES["gcash"].email_query
GOTYME_QUERY = sources.SOURCES["gotyme"].email_query
GOTYME_INPUTS = {"birth_date": "1990-01-31"}
GOTYME_PASSWORD = "01311990"  # what gotyme.build_password() makes of GOTYME_INPUTS
# One GoTyme email carries both PDFs; only the Everyday one is the gotyme source
EVERYDAY_PDF = ("GoTymeStatementForEverydayDepositAccount_20261031_101500.pdf", b"%PDF everyday")
SAVINGS_PDF = ("GoTyme_BankStatement_SavingsAccounts_20261031_101500.pdf", b"%PDF savings")
LAST_CHECKED = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
NEW = LAST_CHECKED + timedelta(minutes=10)  # an email received after the last check


def statement_pdf():
    """A made-up GCash statement that adds up (make_pdf.balanced), as bytes."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "statement.pdf")
        make_pdf.balanced(path)
        return path.read_bytes()


PDF = statement_pdf()


def imported(created=4, matches=True):
    """What pipeline.import_statement() returns after an upload."""
    return pipeline.ImportResult(
        new_account=None,
        ynab_before=Decimal("1000.00"),
        created=[ynab_txn(id=f"txn-{n}") for n in range(created)],
        skipped=0,
        ending_check={"matches": matches},
        ending_error=None,
    )


# =============================================================================
# FAKE GMAIL - the functions of autoimport/gmail.py that the runner calls
# =============================================================================


class FakeGmail:
    """A made-up inbox in place of autoimport/gmail.py.

        gmail.add("msg-1", received=NEW)          # a GCash statement email with PDF
        gmail.add("msg-2", attachment=None)       # an email without a PDF
        gmail.add("msg-3", query=GOTYME_QUERY, attachments=[SAVINGS_PDF, EVERYDAY_PDF])
        gmail.add("msg-4", body="Mobile number: 0918765****")  # for one GCash account
    searches lists every (query, after) the runner asked for; downloads every
    attachment filename it got.
    """

    GmailError = gmail.GmailError
    # Plain functions of a fetched message: the real ones
    message_text = staticmethod(gmail.message_text)
    attachment_names = staticmethod(gmail.attachment_names)

    def __init__(self):
        self.emails = []  # oldest first: {"id", "query", "received", "attachments"}
        self.searches = []
        self.downloads = []
        self.attachment_error = None  # raised by get_pdf_attachment() when set

    def add(
        self,
        msg_id,
        received=NEW,
        query=GCASH_QUERY,
        attachment=("statement.pdf", PDF),
        attachments=None,
        body="",
    ):
        """`attachment` is one (filename, bytes) or None; `attachments` a list of them."""
        if attachments is None:
            attachments = [attachment] if attachment else []
        self.emails.append(
            {
                "id": msg_id,
                "query": query,
                "received": received,
                "attachments": attachments,
                "body": body,
            }
        )

    def refresh_access_token(self, refresh_token):
        if refresh_token != GMAIL_REFRESH_TOKEN:
            raise gmail.GmailError("Google API returned HTTP 400: invalid_grant", status=400)
        return GMAIL_ACCESS_TOKEN

    def list_messages(self, access_token, query, after=None):
        self.searches.append((query, after))
        found = [
            {"id": e["id"], "threadId": e["id"]}
            for e in self.emails
            if e["query"] == query and (after is None or e["received"] > after)
        ]
        return found[::-1]  # Gmail lists the newest first

    def get_message(self, access_token, msg_id):
        email = self._email(msg_id)
        text = base64.urlsafe_b64encode(email["body"].encode()).decode()
        return {
            "id": msg_id,
            "internalDate": str(int(email["received"].timestamp() * 1000)),
            "payload": {
                "mimeType": "multipart/mixed",
                "parts": [{"mimeType": "text/plain", "body": {"data": text}}]
                + [{"filename": name, "body": {}} for name, _pdf in email["attachments"]],
            },
        }

    def get_pdf_attachment(self, access_token, msg_id, pattern):
        """The first attachment whose filename matches, like gmail.get_pdf_attachment()."""
        if self.attachment_error:
            raise self.attachment_error
        regex = re.compile(pattern, re.IGNORECASE)
        for filename, pdf_bytes in self._email(msg_id)["attachments"]:
            if regex.search(filename):
                self.downloads.append(filename)
                return filename, pdf_bytes
        return None

    def _email(self, msg_id):
        return next(e for e in self.emails if e["id"] == msg_id)


class RunnerTest(unittest.TestCase):
    """A FakeStore, a FakeGmail inbox and a fake YNAB login for each test."""

    def setUp(self):
        self.store = FakeStore()
        self.gmail = FakeGmail()
        self.ynab_refresh_tokens = set()  # the ones YNAB would accept now
        self.ynab_logins = 0  # how many times a YNAB refresh token was used
        self.import_statement = mock.Mock(return_value=imported())
        for patcher in (
            mock.patch.dict(os.environ, ENCRYPTION_KEYS=ENCRYPTION_KEY),
            mock.patch.object(runner, "store", self.store),
            mock.patch.object(runner, "gmail", self.gmail),
            mock.patch.object(runner.ynab_oauth, "refresh", side_effect=self.ynab_refresh),
            mock.patch.object(runner.pipeline, "import_statement", self.import_statement),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ynab_refresh(self, refresh_token):
        """Like YNAB: each refresh token works once, and a new one comes back."""
        if refresh_token not in self.ynab_refresh_tokens:
            raise urllib.error.HTTPError(
                "https://app.ynab.com/oauth/token", 401, "Unauthorized", {}, None
            )
        self.ynab_refresh_tokens.remove(refresh_token)
        self.ynab_logins += 1
        new_token = f"ynab-refresh-{self.ynab_logins}"
        self.ynab_refresh_tokens.add(new_token)
        return {"access_token": f"ynab-access-{self.ynab_logins}", "refresh_token": new_token}

    def add_user(self, sub=SUB, mappings=None, last_checked=LAST_CHECKED, password_inputs=None):
        """A user with auto-import on; GCash goes to ACCOUNT_ID unless `mappings` says otherwise.

        Every mapped source gets the GCash INPUTS unless `password_inputs` says otherwise.
        """
        ynab_refresh_token = f"ynab-refresh-start-{sub}"
        self.ynab_refresh_tokens.add(ynab_refresh_token)
        self.store.save_user(
            sub,
            email=EMAIL,
            enabled=True,
            last_checked=last_checked,
            gmail_refresh_token=GMAIL_REFRESH_TOKEN,
            ynab_refresh_token=ynab_refresh_token,
            plan_id=PLAN_ID,
            plan_name="Test Plan",
            mappings=mappings
            or {"gcash": {"account_id": ACCOUNT_ID, "account_name": "GCash", "enabled": True}},
            password_inputs=password_inputs
            or {key: INPUTS for key in (mappings or {"gcash": None})},
        )

    def runs(self, sub=SUB):
        """The user's runs, oldest first: {"msg_id:source": run}."""
        return self.store.runs.get(sub, {})


# =============================================================================
# POLL - import once, record the run, skip it next time
# =============================================================================


class PollTest(RunnerTest):
    def test_a_new_email_is_imported_once(self):
        self.add_user()
        self.gmail.add("msg-1")

        counts = runner.poll_all()

        self.assertEqual(counts, {"users": 1, "runs": 1, "failed": 0, "deferred": 0})
        self.import_statement.assert_called_once()
        rows, transactions, account_id, plan_id, token = self.import_statement.call_args.args
        self.assertEqual((account_id, plan_id, token), (ACCOUNT_ID, PLAN_ID, "ynab-access-1"))
        self.assertEqual(
            self.import_statement.call_args.kwargs,
            {"source": "gcash", "on_step": mock.ANY, "transfers": {}},
        )
        self.assertEqual(len(transactions), len(make_pdf.BALANCED_ROWS))  # the real parser ran
        run = self.runs()["msg-1:gcash"]
        self.assertEqual((run["status"], run["created"], run["skipped"]), (runner.OK, 4, 0))
        self.assertEqual((run["account_id"], run["received_at"]), (ACCOUNT_ID, NEW))
        self.assertEqual(run["account_name"], "GCash")

        # The next poll finds the same email, but its run is already there
        self.assertEqual(runner.poll_all()["runs"], 0)
        self.import_statement.assert_called_once()

    def test_last_checked_moves_on_and_the_search_starts_a_little_before_it(self):
        self.add_user()
        before = datetime.now(UTC)
        runner.poll_all()
        self.assertEqual(self.gmail.searches, [(GCASH_QUERY, LAST_CHECKED - runner.SEARCH_OVERLAP)])
        self.assertGreaterEqual(self.store.get_user(SUB)["last_checked"], before)

    def test_emails_are_imported_oldest_first(self):
        self.add_user()
        self.gmail.add("msg-old", received=NEW)
        self.gmail.add("msg-new", received=NEW + timedelta(days=1))
        runner.poll_all()
        self.assertEqual(list(self.runs()), ["msg-old:gcash", "msg-new:gcash"])

    def test_each_source_goes_to_its_own_account_and_dont_import_is_not_searched(self):
        gcash = sources.SOURCES["gcash"]
        wallet = dataclasses.replace(gcash, key="wallet", email_query="from:wallet@example.test")
        unused = dataclasses.replace(gcash, key="unused", email_query="from:unused@example.test")
        self.enterContext(mock.patch.dict(sources.SOURCES, wallet=wallet, unused=unused))
        self.add_user(
            mappings={
                "gcash": {"account_id": ACCOUNT_ID, "account_name": "GCash", "enabled": True},
                "wallet": {"account_id": OTHER_ACCOUNT_ID, "account_name": "W", "enabled": True},
                "unused": {"account_id": "", "account_name": "", "enabled": False},
            }
        )
        self.gmail.add("msg-1")
        self.gmail.add("msg-2", query=wallet.email_query)
        self.gmail.add("msg-3", query=unused.email_query)

        runner.poll_all()

        accounts = {c.kwargs["source"]: c.args[2] for c in self.import_statement.call_args_list}
        self.assertEqual(accounts, {"gcash": ACCOUNT_ID, "wallet": OTHER_ACCOUNT_ID})
        searched = [query for query, _after in self.gmail.searches]
        self.assertEqual(searched, [GCASH_QUERY, wallet.email_query])
        self.assertEqual(sorted(self.runs()), ["msg-1:gcash", "msg-2:wallet"])

    def test_the_rotated_ynab_refresh_token_is_saved(self):
        self.add_user()
        self.gmail.add("msg-1")
        runner.poll_all()
        saved = self.store.get_user(SUB)["ynab_refresh_token"]
        self.assertEqual(self.store.decrypt(saved), "ynab-refresh-1")
        # The old token no longer works, so the next import succeeds only with the saved one
        self.gmail.add("msg-2", received=datetime.now(UTC))  # after the first poll
        self.assertEqual(runner.poll_all()["failed"], 0)
        self.assertEqual(self.ynab_logins, 2)

    def test_poll_all_never_raises(self):
        broken = mock.patch.object(self.store, "list_enabled_users", side_effect=RuntimeError)
        with broken, self.assertLogs(runner.log, "ERROR"):
            self.assertEqual(runner.poll_all()["users"], 0)

    def test_users_not_reached_in_time_are_left_for_the_next_poll(self):
        self.add_user(OTHER_SUB, last_checked=LAST_CHECKED)
        self.add_user(SUB, last_checked=LAST_CHECKED - timedelta(days=1))  # waited longer
        # The runner's clock: the start, before the first user, then past the budget
        clock = mock.Mock(monotonic=mock.Mock(side_effect=[0, 0, runner.TIME_BUDGET_SECONDS + 1]))
        with mock.patch.object(runner, "time", clock):
            counts = runner.poll_all()
        self.assertEqual((counts["users"], counts["deferred"]), (1, 1))
        self.assertGreater(self.store.get_user(SUB)["last_checked"], LAST_CHECKED)
        self.assertEqual(self.store.get_user(OTHER_SUB)["last_checked"], LAST_CHECKED)


# =============================================================================
# GOTYME - a second real source: one email with two PDFs, its own account
# =============================================================================


class GoTymeTest(RunnerTest):
    """GoTyme's real Source settings, with a stand-in parser that records its input."""

    ROWS = [
        {
            "Date": "05 Oct 2026",
            "Details": "Card payment at SHOPEE PH, Card •••• 1234",
            "Credits": "0.00",
            "Debits": "249.00",
            "Running Balance": "4751.00",
        }
    ]

    def setUp(self):
        super().setUp()
        self.parsed = []  # (pdf bytes, password) for every PDF the parser was given
        gotyme = dataclasses.replace(sources.SOURCES["gotyme"], parse=self.parse)
        self.enterContext(mock.patch.dict(sources.SOURCES, gotyme=gotyme))

    def parse(self, pdf, password, on_page=None):
        self.parsed.append((pdf.read(), password))
        return [dict(row) for row in self.ROWS]

    def add_gotyme_user(self):
        """GCash goes to ACCOUNT_ID and GoTyme to OTHER_ACCOUNT_ID, each with its own inputs."""
        self.add_user(
            mappings={
                "gcash": {"account_id": ACCOUNT_ID, "account_name": "GCash", "enabled": True},
                "gotyme": {
                    "account_id": OTHER_ACCOUNT_ID,
                    "account_name": "GoTyme",
                    "enabled": True,
                },
            },
            password_inputs={"gcash": INPUTS, "gotyme": GOTYME_INPUTS},
        )

    def test_only_the_everyday_pdf_is_downloaded_and_parsed(self):
        self.add_gotyme_user()
        # The savings PDF comes first, so taking any PDF would pick the wrong one
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF, EVERYDAY_PDF])

        runner.poll_all()

        self.assertEqual(self.gmail.downloads, [EVERYDAY_PDF[0]])
        self.assertEqual(self.parsed, [(EVERYDAY_PDF[1], GOTYME_PASSWORD)])
        run = self.runs()["msg-g:gotyme"]
        self.assertEqual((run["status"], run["account_id"]), (runner.OK, OTHER_ACCOUNT_ID))

    def test_an_email_with_only_the_savings_pdf_imports_nothing(self):
        self.add_gotyme_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])

        runner.poll_all()

        self.assertEqual(self.runs()["msg-g:gotyme"]["status"], runner.NO_ATTACHMENT)
        self.assertEqual(self.parsed, [])
        self.import_statement.assert_not_called()

    def test_gcash_and_gotyme_emails_go_to_their_own_accounts(self):
        self.add_gotyme_user()
        self.gmail.add("msg-c")  # a GCash email, read by the real GCash parser
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF, EVERYDAY_PDF])

        runner.poll_all()

        accounts = {c.kwargs["source"]: c.args[2] for c in self.import_statement.call_args_list}
        self.assertEqual(accounts, {"gcash": ACCOUNT_ID, "gotyme": OTHER_ACCOUNT_ID})
        self.assertEqual(
            [query for query, _after in self.gmail.searches], [GCASH_QUERY, GOTYME_QUERY]
        )
        self.assertEqual(sorted(self.runs()), ["msg-c:gcash", "msg-g:gotyme"])
        self.assertEqual(self.parsed, [(EVERYDAY_PDF[1], GOTYME_PASSWORD)])  # only GoTyme's


class GoalSaveTest(RunnerTest):
    """GoTyme GoalSave's real Source settings: one savings PDF in the GoTyme
    email holds every GoalSave account, each imported into its own wallet's
    YNAB account. A stand-in parser returns two accounts, 11111 and 22222."""

    def setUp(self):
        super().setUp()
        self.parsed = []  # (pdf bytes, password) for every PDF the parser was given
        replaced = {
            key: dataclasses.replace(sources.SOURCES[key], parse=self.parse)
            for key in ("gotyme", "gotyme_goalsave")
        }
        self.enterContext(mock.patch.dict(sources.SOURCES, replaced))

    def parse(self, pdf, password, on_page=None):
        self.parsed.append((pdf.read(), password))
        if pdf.getvalue() == SAVINGS_PDF[1]:
            return [dict(row) for row in TWO_ACCOUNT_ROWS]
        return [dict(row) for row in GoTymeTest.ROWS]

    def add_goalsave_user(self, second=True, second_enabled=True):
        """GoTyme Everyday to OTHER_ACCOUNT_ID, GoalSave 22222 to "goalsave-22222"
        and, with `second`, 11111 to "goalsave-11111". Every GoalSave wallet has
        the date of birth, as saving the page leaves it (Source.shared_password)."""
        mappings = {
            "gotyme": {"account_id": OTHER_ACCOUNT_ID, "account_name": "GoTyme", "enabled": True},
            "gotyme_goalsave": {
                "source": "gotyme_goalsave",
                "account_id": "goalsave-22222",
                "account_name": "GoalSave Emergency",
                "enabled": True,
            },
        }
        password_inputs = {
            "gotyme": GOTYME_INPUTS,
            "gotyme_goalsave": {**GOTYME_INPUTS, "goalsave_last5": "22222"},
        }
        if second:
            mappings["gotyme_goalsave-2"] = {
                "source": "gotyme_goalsave",
                "account_id": "goalsave-11111" if second_enabled else "",
                "account_name": "GoalSave Travel" if second_enabled else "",
                "enabled": second_enabled,
            }
            password_inputs["gotyme_goalsave-2"] = (
                {**GOTYME_INPUTS, "goalsave_last5": "11111"}
                if second_enabled
                else {"goalsave_last5": "11111"}
            )
        self.add_user(mappings=mappings, password_inputs=password_inputs)

    def imported_into(self):
        """{YNAB account: the GoalSave No. of every row imported into it}."""
        return {
            c.args[2]: [row.get("GoalSave No.") for row in c.args[0]]
            for c in self.import_statement.call_args_list
        }

    def test_both_pdfs_of_one_email_are_imported(self):
        self.add_goalsave_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF, EVERYDAY_PDF])

        runner.poll_all()

        self.assertEqual(sorted(self.gmail.downloads), sorted([EVERYDAY_PDF[0], SAVINGS_PDF[0]]))
        self.assertEqual(
            sorted(self.parsed),
            sorted([(EVERYDAY_PDF[1], GOTYME_PASSWORD), (SAVINGS_PDF[1], GOTYME_PASSWORD)]),
        )
        self.assertEqual(
            self.imported_into(),
            {
                OTHER_ACCOUNT_ID: [None],
                "goalsave-11111": ["11111"],
                "goalsave-22222": ["22222", "22222"],
            },
        )
        self.assertEqual(sorted(self.runs()), ["msg-g:gotyme", "msg-g:gotyme_goalsave"])

    def test_one_run_with_an_entry_per_account(self):
        self.add_goalsave_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])

        runner.poll_all()

        run = self.runs()["msg-g:gotyme_goalsave"]
        self.assertEqual(
            (run["status"], run["created"], run["wallet_name"], run["account_id"]),
            (runner.OK, 8, "GoTyme GoalSave", ""),
        )
        self.assertEqual(
            [
                (e["wallet"], e["wallet_name"], e["account_id"], e["status"], e["created"])
                for e in run["accounts"]
            ],
            [  # in the PDF's order
                ("gotyme_goalsave-2", "GoTyme GoalSave · 11111", "goalsave-11111", runner.OK, 4),
                ("gotyme_goalsave", "GoTyme GoalSave · 22222", "goalsave-22222", runner.OK, 4),
            ],
        )
        self.assertEqual(self.runs()["msg-g:gotyme"]["status"], runner.NO_ATTACHMENT)

    def test_an_account_not_on_the_page(self):
        self.add_goalsave_user(second=False)
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])

        runner.poll_all()

        self.assertEqual(self.imported_into(), {"goalsave-22222": ["22222", "22222"]})
        run = self.runs()["msg-g:gotyme_goalsave"]
        self.assertEqual(run["status"], runner.NO_WALLET)
        missing = run["accounts"][0]
        self.assertEqual(
            (missing["wallet"], missing["status"], missing["message"]),
            ("", runner.NO_WALLET, "This statement has GoalSave No. 11111. Add it on this page."),
        )
        self.assertEqual(run["accounts"][1]["status"], runner.OK)

    def test_a_dont_import_account_is_left_out(self):
        self.add_goalsave_user(second_enabled=False)
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])

        runner.poll_all()

        self.assertEqual(self.imported_into(), {"goalsave-22222": ["22222", "22222"]})
        run = self.runs()["msg-g:gotyme_goalsave"]
        self.assertEqual(run["status"], runner.OK)
        self.assertEqual([e["wallet"] for e in run["accounts"]], ["gotyme_goalsave"])

    def test_ynab_busy_on_the_second_account_records_nothing(self):
        self.add_goalsave_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])
        busy = ynab.ApiException(status=503, reason="Unavailable")
        self.import_statement.side_effect = [imported(), busy]

        with self.assertLogs(runner.log, "WARNING"):
            self.assertEqual(runner.poll_all()["failed"], 1)
        self.assertNotIn("msg-g:gotyme_goalsave", self.runs())

        # The next poll imports both again; YNAB's import_ids skip what's already there
        self.import_statement.side_effect = None
        runner.poll_all()
        self.assertEqual(self.runs()["msg-g:gotyme_goalsave"]["status"], runner.OK)

    def test_gemini_categorizes_once_per_email(self):
        self.add_goalsave_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])

        with mock.patch.object(runner, "_categorize") as categorize:
            runner.poll_all()

        categorize.assert_called_once()
        self.assertEqual(len(categorize.call_args.args[1]), 8)  # both accounts' created rows

    def test_the_page_shows_how_many_accounts_it_imports_into(self):
        self.add_goalsave_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF])

        runner.poll_all()

        emails = self.store.progress_history[0]["emails"]
        goalsave = next(e for e in emails if e["source"] == "gotyme_goalsave")
        self.assertEqual(
            (goalsave["wallet_name"], goalsave["account_name"]), ("GoTyme GoalSave", "2 accounts")
        )

    def test_the_test_button_names_the_accounts_found(self):
        self.add_goalsave_user()
        self.gmail.add("msg-g", query=GOTYME_QUERY, attachments=[SAVINGS_PDF, EVERYDAY_PDF])

        ok, message = runner.test_password(
            self.store.get_user(SUB), "gotyme_goalsave", GOTYME_INPUTS
        )

        self.assertEqual(
            (ok, message),
            (
                True,
                "The password opens your newest GoTyme GoalSave statement. "
                "It has GoalSave No. 11111, 22222.",
            ),
        )
        self.assertEqual(self.parsed, [(SAVINGS_PDF[1], GOTYME_PASSWORD)])


# =============================================================================
# RUN STATUSES - what one email's run records
# =============================================================================


class RunStatusTest(RunnerTest):
    def poll_one(self, **email):
        self.add_user()
        self.gmail.add("msg-1", **email)
        runner.poll_all()
        return self.runs()["msg-1:gcash"]

    def test_wrong_password_uploads_nothing(self):
        wrong = PasswordError("Incorrect PDF password.")
        with mock.patch.object(runner.pipeline, "parse_statement", side_effect=wrong):
            run = self.poll_one()
        self.assertEqual(run["status"], runner.PDF_LOCKED)
        self.import_statement.assert_not_called()

    def test_the_password_comes_from_the_stored_inputs(self):
        with mock.patch.object(
            runner.pipeline, "parse_statement", wraps=runner.pipeline.parse_statement
        ) as parse:
            self.poll_one()
        self.assertEqual(parse.call_args.args[1], PASSWORD)

    def test_a_statement_that_does_not_parse(self):
        run = self.poll_one(attachment=("statement.pdf", b"%PDF-not really"))
        self.assertEqual(
            (run["status"], run["message"]), (runner.PARSE_ERROR, "The PDF couldn't be read.")
        )
        self.import_statement.assert_not_called()

    def test_no_attachment(self):
        run = self.poll_one(attachment=None)
        self.assertEqual(run["status"], runner.NO_ATTACHMENT)

    def test_balance_mismatch(self):
        self.import_statement.return_value = imported(created=3, matches=False)
        run = self.poll_one()
        self.assertEqual((run["status"], run["created"]), (runner.BALANCE_MISMATCH, 3))

    def test_ynab_rejecting_the_upload_is_recorded(self):
        self.import_statement.side_effect = ynab.ApiException(status=400, reason="Bad Request")
        run = self.poll_one()
        self.assertEqual(
            (run["status"], run["message"]), (runner.YNAB_ERROR, "YNAB returned HTTP 400.")
        )


# =============================================================================
# FAILURES THAT MAY PASS - nothing recorded, tried again next poll
# =============================================================================


class RetryTest(RunnerTest):
    def test_one_users_ynab_error_does_not_stop_the_next_user(self):
        self.add_user(OTHER_SUB, last_checked=LAST_CHECKED - timedelta(days=1))
        self.store.save_user(OTHER_SUB, ynab_refresh_token="revoked")  # checked first, and fails
        self.add_user(SUB)
        self.gmail.add("msg-1")

        with self.assertLogs(runner.log, "WARNING"):
            counts = runner.poll_all()

        self.assertEqual((counts["failed"], counts["runs"]), (1, 1))
        self.assertIn("msg-1:gcash", self.runs(SUB))
        self.assertEqual(
            self.store.get_user(OTHER_SUB)["last_checked"], LAST_CHECKED - timedelta(days=1)
        )

    def test_gmail_failing_records_nothing_and_the_next_poll_tries_again(self):
        self.add_user()
        self.gmail.add("msg-1")
        self.gmail.attachment_error = gmail.GmailError("Could not reach Google API: timed out")

        with self.assertLogs(runner.log, "WARNING"):
            self.assertEqual(runner.poll_all()["failed"], 1)
        self.assertEqual(self.runs(), {})
        self.assertEqual(self.store.get_user(SUB)["last_checked"], LAST_CHECKED)

        self.gmail.attachment_error = None
        runner.poll_all()
        self.assertEqual(self.runs()["msg-1:gcash"]["status"], runner.OK)

    def test_ynab_busy_records_nothing_and_the_next_poll_tries_again(self):
        self.add_user()
        self.gmail.add("msg-1")
        self.import_statement.side_effect = ynab.ApiException(status=503, reason="Unavailable")

        with self.assertLogs(runner.log, "WARNING"):
            self.assertEqual(runner.poll_all()["failed"], 1)
        self.assertEqual(self.runs(), {})

        self.import_statement.side_effect = None
        runner.poll_all()
        self.assertEqual(self.runs()["msg-1:gcash"]["status"], runner.OK)


# =============================================================================
# PROGRESS - what the page shows while a run imports
# =============================================================================


class ProgressTest(RunnerTest):
    def importing(self, error=None):
        """import_statement() that reports its steps, then fails with `error` if given."""

        def import_statement(*args, on_step, **kwargs):
            on_step(pipeline.STEP_UPLOAD, {"count": 4})
            if error:
                raise error
            on_step(pipeline.STEP_END_BALANCE, {})
            return imported()

        return import_statement

    def test_each_step_is_shown_once_then_cleared(self):
        self.add_user()
        self.gmail.add("msg-1")
        self.gmail.add("msg-2", received=NEW + timedelta(minutes=1))
        self.import_statement.side_effect = self.importing()

        runner.poll_all()

        history = self.store.progress_history
        steps = [runner.STEP_DOWNLOAD, pipeline.STEP_READ, pipeline.STEP_UPLOAD]
        steps.append(pipeline.STEP_END_BALANCE)
        # One write per step (the PDF's pages don't each write), for each email
        self.assertEqual(
            [(p["index"], p["step"]) for p in history],
            [(0, s) for s in steps] + [(1, s) for s in steps],
        )
        self.assertEqual(history[0]["emails"][0]["account_name"], "GCash")
        self.assertEqual(history[0]["emails"][0]["received_at"], NEW)
        self.assertEqual(history[2]["rows"], 4)
        self.assertIsNone(self.store.get_user(SUB)["progress"])

        run = self.runs()["msg-1:gcash"]
        self.assertEqual(run["step"], pipeline.STEP_END_BALANCE)
        self.assertIsInstance(run["duration_seconds"], int)

    def test_a_failed_run_records_the_step_it_stopped_at(self):
        self.add_user()
        self.gmail.add("msg-1")
        error = ynab.ApiException(status=400, reason="Bad Request")
        self.import_statement.side_effect = self.importing(error)

        runner.poll_all()

        run = self.runs()["msg-1:gcash"]
        self.assertEqual((run["status"], run["step"]), (runner.YNAB_ERROR, pipeline.STEP_UPLOAD))
        self.assertIsNone(self.store.get_user(SUB)["progress"])

    def test_progress_is_cleared_when_a_run_raises(self):
        self.add_user()
        self.gmail.add("msg-1")
        busy = ynab.ApiException(status=503, reason="Unavailable")
        self.import_statement.side_effect = self.importing(busy)

        with self.assertLogs(runner.log, "WARNING"):
            runner.poll_all()

        self.assertEqual(self.runs(), {})
        self.assertTrue(self.store.progress_history)
        self.assertIsNone(self.store.get_user(SUB)["progress"])

    def test_nothing_is_written_when_there_is_nothing_to_import(self):
        self.add_user()
        runner.poll_all()
        self.assertEqual(self.store.progress_history, [])
        self.assertNotIn("progress", self.store.get_user(SUB))


# =============================================================================
# LOGS - one line per run, nothing personal
# =============================================================================


class CategorizeTest(RunnerTest):
    """Gemini categorizes what each import created, for a user who turned it on."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, GEMINI_API_KEY="test-gemini-key"))
        self.categorize = self.enterContext(
            mock.patch.object(runner, "categorize_transactions", return_value=([], 3))
        )

    def poll_one(self, categorize=True):
        self.add_user()
        self.store.save_user(SUB, categorize=categorize)
        self.gmail.add("msg-1")
        with self.assertLogs(runner.log, "INFO") as logs:
            runner.poll_all()
        self.categorize_logs = [line for line in logs.output if "categorize" in line]
        return self.runs()["msg-1:gcash"]

    def test_only_what_the_import_created(self):
        run = self.poll_one()

        plan_id, transactions, api_key, token = self.categorize.call_args.args
        self.assertEqual(
            (plan_id, [t["id"] for t in transactions], api_key, token),
            (PLAN_ID, ["txn-0", "txn-1", "txn-2", "txn-3"], "test-gemini-key", "ynab-access-1"),
        )
        self.assertIsNotNone(self.categorize.call_args.kwargs["deadline"])
        self.assertEqual((run["status"], run["categorized"]), (runner.OK, 3))
        user = runner.user_id({"sub": SUB})
        self.assertEqual(
            self.categorize_logs,
            [f"INFO:{runner.log.name}:Auto-import categorize: user {user}, categorized 3 of 4."],
        )
        steps = [p["step"] for p in self.store.progress_history]
        self.assertEqual(steps[-1], pipeline.STEP_CATEGORIZE)
        self.assertTrue(self.store.progress_history[0]["categorize"])

    def test_off(self):
        run = self.poll_one(categorize=False)
        self.categorize.assert_not_called()
        self.assertNotIn("categorized", run)
        self.assertFalse(self.store.progress_history[0]["categorize"])
        self.assertIn("skipped (turned off)", self.categorize_logs[0])

    def test_no_gemini_key_on_the_server(self):
        del os.environ["GEMINI_API_KEY"]
        run = self.poll_one()
        self.categorize.assert_not_called()
        self.assertNotIn("categorized", run)
        self.assertIn("WARNING", self.categorize_logs[0])
        self.assertIn("skipped (GEMINI_API_KEY isn't set)", self.categorize_logs[0])

    def test_nothing_new_to_categorize(self):
        self.import_statement.return_value = imported(created=0)
        run = self.poll_one()
        self.categorize.assert_not_called()
        self.assertEqual(run["categorized"], 0)

    def test_a_failure_still_records_the_import(self):
        cases = [
            (runner.gemini.GeminiError("Gemini ran out of time."), "Gemini ran out of time."),
            (ynab.ApiException(status=400, reason="Bad Request"), "YNAB returned HTTP 400."),
        ]
        for error, message in cases:
            with self.subTest(message):
                self.store.runs.clear()
                self.categorize.side_effect = error
                run = self.poll_one()
                self.assertEqual((run["status"], run["created"]), (runner.OK, 4))
                self.assertEqual(run["categorize_error"], message)
                self.assertIn(f"failed: {message}", self.categorize_logs[0])


class TransferRulesTest(RunnerTest):
    """Rows a saved transfer rule matches go to import_statement() as transfers."""

    def test_matching_rows_are_imported_as_transfers(self):
        self.add_user()  # GCash goes to ACCOUNT_ID; the other bank needs no mapping
        # make_pdf.BALANCED_ROWS[2] is "Cash-in from Test Bank"
        rule = {
            "account_1_id": ACCOUNT_ID,
            "account_1_name": "GCash",
            "description_1": "cash-in FROM test",
            "account_2_id": OTHER_ACCOUNT_ID,
            "account_2_name": "GoTyme",
            "description_2": "",
        }
        self.store.save_user(SUB, transfer_pairs=[rule])
        self.import_statement.return_value = dataclasses.replace(imported(), transfers=1)
        self.gmail.add("msg-1")

        runner.poll_all()

        self.assertEqual(self.import_statement.call_args.kwargs["transfers"], {2: OTHER_ACCOUNT_ID})
        self.assertEqual(self.runs()["msg-1:gcash"]["transfers"], 1)

    def test_no_rules_no_transfers(self):
        self.add_user()
        self.gmail.add("msg-1")
        runner.poll_all()
        self.assertEqual(self.import_statement.call_args.kwargs["transfers"], {})
        self.assertNotIn("transfers", self.runs()["msg-1:gcash"])


class SeveralWalletsTest(RunnerTest):
    """Two GCash accounts: each email goes to the one whose mobile digits it names."""

    def add_two_gcash(self, second_enabled=True):
        self.add_user(
            mappings={
                "gcash": {
                    "source": "gcash",
                    "account_id": ACCOUNT_ID,
                    "account_name": "GCash1",
                    "enabled": True,
                },
                "gcash-2": {
                    "source": "gcash",
                    "account_id": OTHER_ACCOUNT_ID,
                    "account_name": "GCash2",
                    "enabled": second_enabled,
                },
            },
            password_inputs={
                "gcash": {**INPUTS, "mobile_first7": "0917123"},
                "gcash-2": {
                    "last_name": "Dela Cruz",
                    "mobile_last4": "5678",
                    "mobile_first7": "0918765",
                },
            },
        )

    def test_each_email_goes_to_its_account(self):
        self.add_two_gcash()
        self.gmail.add("msg-1", body="Your statement. Mobile number: 0918765****")
        # No digits in the body: the PDF's name says
        self.gmail.add("msg-2", attachment=("enc-0917123_20261101.pdf", PDF))
        passwords = []
        parse = runner.pipeline.parse_statement

        def parse_statement(pdf, password, *args, **kwargs):
            passwords.append(password)
            return parse(pdf, PASSWORD, *args, **kwargs)  # the made-up PDF opens with one

        with mock.patch.object(runner.pipeline, "parse_statement", side_effect=parse_statement):
            runner.poll_all()

        accounts = [c.args[2] for c in self.import_statement.call_args_list]
        self.assertEqual(accounts, [OTHER_ACCOUNT_ID, ACCOUNT_ID])
        self.assertEqual(passwords, ["delacruz5678", PASSWORD])  # each account's own inputs
        run = self.runs()["msg-1:gcash"]
        self.assertEqual(
            (run["wallet"], run["wallet_name"], run["account_name"]),
            ("gcash-2", "GCash · 0918765", "GCash2"),
        )

    def test_an_account_not_on_the_page(self):
        self.add_two_gcash()
        self.gmail.add("msg-1", body="Mobile number: 0999000****")
        runner.poll_all()
        self.import_statement.assert_not_called()
        run = self.runs()["msg-1:gcash"]
        self.assertEqual((run["status"], run["wallet_name"]), (runner.NO_WALLET, "GCash · 0999000"))
        self.assertIn("This statement is for 0999000****", run["message"])

    def test_an_email_that_does_not_say(self):
        self.add_two_gcash()
        self.gmail.add("msg-1")  # no digits in the body or the PDF's name
        runner.poll_all()
        run = self.runs()["msg-1:gcash"]
        self.assertEqual(run["status"], runner.NO_WALLET)
        self.assertIn("Couldn't tell which GCash account", run["message"])

    def test_an_account_set_to_dont_import_is_skipped(self):
        self.add_two_gcash(second_enabled=False)
        self.gmail.add("msg-1", body="Mobile number: 0918765****")
        runner.poll_all()
        self.import_statement.assert_not_called()
        self.assertEqual(self.runs(), {})

    def test_one_account_with_digits_takes_only_its_own(self):
        self.add_user(password_inputs={"gcash": {**INPUTS, "mobile_first7": "0917123"}})
        self.gmail.add("msg-1", body="Mobile number: 0917123****")
        self.gmail.add("msg-2", body="Mobile number: 0918765****")
        self.gmail.add("msg-3")  # doesn't say: the one account, as before
        runner.poll_all()
        statuses = {key: run["status"] for key, run in self.runs().items()}
        self.assertEqual(
            statuses,
            {"msg-1:gcash": runner.OK, "msg-2:gcash": runner.NO_WALLET, "msg-3:gcash": runner.OK},
        )

    def test_the_test_button_tries_that_accounts_newest_email(self):
        self.add_two_gcash()
        self.gmail.add("msg-1", body="Mobile number: 0918765****")
        self.gmail.add("msg-2", body="Mobile number: 0917123****")
        user = self.store.get_user(SUB)
        with mock.patch.object(
            runner.pipeline, "parse_statement", wraps=runner.pipeline.parse_statement
        ):
            ok, _ = runner.test_password(user, "gcash", INPUTS, "0917123")
        self.assertTrue(ok)
        self.assertEqual(self.gmail.downloads, ["statement.pdf"])

        ok, message = runner.test_password(user, "gcash", INPUTS, "0999000")
        self.assertFalse(ok)
        self.assertIn("No GCash statement email for 0999000****", message)


class LoggingTest(RunnerTest):
    def test_no_password_token_email_or_statement_text_in_the_logs(self):
        self.add_user()
        self.gmail.add("msg-ok")
        self.add_user(OTHER_SUB)
        self.store.save_user(OTHER_SUB, ynab_refresh_token="revoked-refresh-token")
        with self.assertLogs(runner.log, level="DEBUG") as logs:
            runner.poll_all()
            with mock.patch.object(
                runner.pipeline,
                "parse_statement",
                side_effect=PasswordError("Incorrect PDF password."),
            ):
                self.gmail.add("msg-wrong", received=NEW + timedelta(minutes=1))
                runner.poll_all()

        output = "\n".join(logs.output)
        self.assertIn(runner.user_id({"sub": SUB}), output)
        secrets = [SUB, OTHER_SUB, EMAIL, PASSWORD, *INPUTS.values(), GMAIL_REFRESH_TOKEN]
        secrets += [GMAIL_ACCESS_TOKEN, "ynab-access-", "ynab-refresh-", "revoked-refresh-token"]
        secrets += [row.description.split("\n")[0] for row in make_pdf.BALANCED_ROWS]
        secrets += [row.reference for row in make_pdf.BALANCED_ROWS]
        for secret in secrets:
            self.assertNotIn(secret, output)


# =============================================================================
# TEST BUTTON - the password inputs tried on the newest email, nothing imported
# =============================================================================


class TestPasswordTest(RunnerTest):
    def test_inputs_that_open_the_newest_statement(self):
        self.add_user()
        self.gmail.add("msg-1")
        ok, message = runner.test_password(self.store.get_user(SUB), "gcash", INPUTS)
        self.assertEqual((ok, message), (True, "The password opens your newest GCash statement."))
        self.import_statement.assert_not_called()
        self.assertEqual(self.runs(), {})

    def test_wrong_inputs(self):
        self.add_user()
        self.gmail.add("msg-1")
        wrong = PasswordError("Incorrect PDF password.")
        with mock.patch.object(runner.pipeline, "parse_statement", side_effect=wrong) as parse:
            ok, message = runner.test_password(self.store.get_user(SUB), "gcash", INPUTS)
        self.assertEqual((ok, message), (False, "Wrong password. Check the inputs."))
        self.assertEqual(parse.call_args.args[1], PASSWORD)

    def test_no_statement_email_yet(self):
        self.add_user()
        ok, message = runner.test_password(self.store.get_user(SUB), "gcash", INPUTS)
        self.assertFalse(ok)
        self.assertEqual(message, "No GCash statement email in Gmail yet. Request one in the app.")

    def test_the_newest_email_has_no_pdf(self):
        self.add_user()
        self.gmail.add("msg-1")
        self.gmail.add("msg-2", received=NEW + timedelta(days=1), attachment=None)
        ok, message = runner.test_password(self.store.get_user(SUB), "gcash", INPUTS)
        self.assertEqual(
            (ok, message), (False, "The newest GCash statement email has no PDF attached.")
        )


if __name__ == "__main__":
    unittest.main()
