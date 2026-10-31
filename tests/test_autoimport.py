"""
Tests for autoimport/runner.py: each poll finds new statement emails in Gmail,
imports them once into the mapped YNAB account, and records a run.

Run from the project folder:  python -m unittest discover tests

Gmail is FakeGmail below, a made-up inbox whose PDFs come from
tests/e2e/make_pdf.py, so the real GCash parser reads them. The store is
FakeStore (tests/fakes.py). YNAB is faked at the edges: ynab_oauth.refresh()
and pipeline.import_statement(). All values here are made up.
"""

import dataclasses
import os
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

SUB = "100000000000000000001"  # a Google account ID ("sub")
OTHER_SUB = "100000000000000000002"
EMAIL = "juan@example.test"
GMAIL_REFRESH_TOKEN = "1//made-up-gmail-refresh-token"
GMAIL_ACCESS_TOKEN = "made-up-gmail-access-token"
INPUTS = {"last_name": "Dela Cruz", "mobile_last4": "1234"}
PASSWORD = "delacruz1234"  # what gcash.build_password() makes of INPUTS
OTHER_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"

GCASH_QUERY = sources.SOURCES["gcash"].email_query
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
    searches lists every (query, after) the runner asked for.
    """

    GmailError = gmail.GmailError

    def __init__(self):
        self.emails = []  # oldest first: {"id", "query", "received", "attachment"}
        self.searches = []
        self.attachment_error = None  # raised by get_pdf_attachment() when set

    def add(self, msg_id, received=NEW, query=GCASH_QUERY, attachment=("statement.pdf", PDF)):
        self.emails.append(
            {"id": msg_id, "query": query, "received": received, "attachment": attachment}
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
        return {"id": msg_id, "internalDate": str(int(email["received"].timestamp() * 1000))}

    def get_pdf_attachment(self, access_token, msg_id, pattern):
        if self.attachment_error:
            raise self.attachment_error
        return self._email(msg_id)["attachment"]

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

    def add_user(self, sub=SUB, mappings=None, last_checked=LAST_CHECKED):
        """A user with auto-import on; GCash goes to ACCOUNT_ID unless `mappings` says otherwise."""
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
            password_inputs={key: INPUTS for key in (mappings or {"gcash": None})},
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
        self.assertEqual(self.import_statement.call_args.kwargs, {"source": "gcash"})
        self.assertEqual(len(transactions), len(make_pdf.BALANCED_ROWS))  # the real parser ran
        run = self.runs()["msg-1:gcash"]
        self.assertEqual((run["status"], run["created"], run["skipped"]), (runner.OK, 4, 0))
        self.assertEqual((run["account_id"], run["received_at"]), (ACCOUNT_ID, NEW))

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
        runner.poll_all()
        saved = self.store.get_user(SUB)["ynab_refresh_token"]
        self.assertEqual(self.store.decrypt(saved), "ynab-refresh-1")
        # The old token no longer works, so the next poll succeeds only with the saved one
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
# LOGS - one line per run, nothing personal
# =============================================================================


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
