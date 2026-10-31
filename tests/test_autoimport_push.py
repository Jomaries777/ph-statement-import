"""
Tests for Gmail push in autoimport/runner.py: process_push() imports for the
one user whose inbox changed, YNAB is only touched when there's a statement to
import, and each user's Gmail push is started and renewed.

Run from the project folder:  python -m unittest discover tests

Uses the made-up inbox, store and YNAB login of tests/test_autoimport.py
(RunnerTest). gmail.watch() is a Mock here. All values here are made up.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest import mock

from statement_etl.autoimport import gmail, runner
from tests.test_autoimport import EMAIL, GMAIL_ACCESS_TOKEN, LAST_CHECKED, SUB, RunnerTest

TOPIC = "projects/example-project/topics/gmail-push"
WATCH_ENDS = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)  # what the fake gmail.watch() returns


class PushTest(RunnerTest):
    """RunnerTest with GMAIL_PUSH_TOPIC set and a fake gmail.watch()."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, GMAIL_PUSH_TOPIC=TOPIC))
        self.gmail.watch = mock.Mock(return_value=WATCH_ENDS)

    def add_user(self, *args, watch_ends=None, **kwargs):
        """A user whose Gmail push runs until `watch_ends` (default: far off, so not renewed)."""
        super().add_user(*args, **kwargs)
        sub = kwargs.get("sub", args[0] if args else SUB)
        ends = watch_ends or datetime.now(UTC) + timedelta(days=6)
        self.store.save_user(sub, gmail_watch_expires=ends)


# =============================================================================
# PUSH - one user, and YNAB only when there's a statement
# =============================================================================


class ProcessPushTest(PushTest):
    def test_an_unrelated_inbox_change_does_not_touch_ynab(self):
        self.add_user()
        before = datetime.now(UTC)

        self.assertEqual(runner.process_push(EMAIL), runner.PUSH_DONE)

        self.assertEqual(self.ynab_logins, 0)
        self.import_statement.assert_not_called()
        self.assertEqual(self.runs(), {})
        self.assertGreaterEqual(self.store.get_user(SUB)["last_checked"], before)

    def test_a_new_statement_refreshes_ynab_once_and_is_imported(self):
        self.add_user()
        self.gmail.add("msg-1")

        self.assertEqual(runner.process_push(EMAIL), runner.PUSH_DONE)

        self.assertEqual(self.ynab_logins, 1)
        self.assertEqual(self.runs()["msg-1:gcash"]["status"], runner.OK)
        # The same email pushed again (e.g. it was opened): nothing new, no YNAB call
        runner.process_push(EMAIL)
        self.assertEqual(self.ynab_logins, 1)
        self.import_statement.assert_called_once()

    def test_the_address_matches_whatever_its_case(self):
        self.add_user()
        self.gmail.add("msg-1")
        self.assertEqual(runner.process_push("  JUAN@Example.TEST "), runner.PUSH_DONE)
        self.assertIn("msg-1:gcash", self.runs())

    def test_an_unknown_or_turned_off_address_is_ignored(self):
        self.add_user()
        for address in ("someone@example.test", "", "   "):
            with self.subTest(address=address):
                self.assertEqual(runner.process_push(address), runner.PUSH_IGNORED)

        self.store.save_user(SUB, enabled=False)
        self.assertEqual(runner.process_push(EMAIL), runner.PUSH_IGNORED)
        self.assertEqual(self.gmail.searches, [])

    def test_a_failing_user_is_logged_and_left_for_the_poll(self):
        self.add_user()
        self.store.save_user(SUB, gmail_refresh_token="1//revoked")

        with self.assertLogs(runner.log, "WARNING") as logs:
            self.assertEqual(runner.process_push(EMAIL), runner.PUSH_FAILED)

        self.assertIn("HTTP 400", logs.output[0])
        self.assertEqual(self.store.get_user(SUB)["last_checked"], LAST_CHECKED)

    def test_the_store_failing_is_logged(self):
        broken = mock.patch.object(self.store, "list_enabled_users", side_effect=RuntimeError)
        with broken, self.assertLogs(runner.log, "ERROR"):
            self.assertEqual(runner.process_push(EMAIL), runner.PUSH_FAILED)

    def test_the_logs_name_the_user_by_hash_only(self):
        self.add_user()
        with self.assertLogs(runner.log, "INFO") as logs:
            runner.process_push(EMAIL)
        output = "\n".join(logs.output)
        self.assertIn(runner.user_id({"sub": SUB}), output)
        for secret in (EMAIL, SUB, GMAIL_ACCESS_TOKEN):
            self.assertNotIn(secret, output)


class LockTest(PushTest):
    """One run at a time: a push and a poll can't refresh the same YNAB token together."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(runner, "LOCK_WAIT_SECONDS", 0.01))
        self.add_user()
        self.gmail.add("msg-1")

    def test_a_push_while_another_run_is_going_is_busy(self):
        with runner._lock:
            self.assertEqual(runner.process_push(EMAIL), runner.PUSH_BUSY)
        self.assertEqual(self.runs(), {})
        # Pub/Sub sends it again, and then it runs
        self.assertEqual(runner.process_push(EMAIL), runner.PUSH_DONE)
        self.assertIn("msg-1:gcash", self.runs())

    def test_a_poll_while_another_run_is_going_is_skipped(self):
        with runner._lock, self.assertLogs(runner.log, "WARNING"):
            self.assertEqual(runner.poll_all()["users"], 0)
        self.assertEqual(self.runs(), {})


# =============================================================================
# WATCH - Gmail push started and renewed by each run
# =============================================================================


class WatchTest(PushTest):
    def test_a_user_without_push_gets_it_started_and_saved(self):
        self.add_user()
        self.store.save_user(SUB, gmail_watch_expires=None)

        runner.poll_all()

        self.gmail.watch.assert_called_once_with(GMAIL_ACCESS_TOKEN, TOPIC)
        self.assertEqual(self.store.get_user(SUB)["gmail_watch_expires"], WATCH_ENDS)

    def test_push_is_renewed_only_within_a_day_of_ending(self):
        for left, renewed in ((timedelta(days=3), False), (timedelta(hours=12), True)):
            with self.subTest(left=left):
                self.gmail.watch.reset_mock()
                self.add_user(watch_ends=datetime.now(UTC) + left)
                runner.process_push(EMAIL)
                self.assertEqual(self.gmail.watch.called, renewed)

    def test_without_a_topic_push_is_never_started(self):
        self.add_user()
        self.store.save_user(SUB, gmail_watch_expires=None)
        with mock.patch.dict(os.environ, GMAIL_PUSH_TOPIC=""):
            runner.poll_all()
            runner.start_watch(self.store.get_user(SUB))
        self.gmail.watch.assert_not_called()

    def test_push_failing_to_start_does_not_stop_the_import(self):
        self.add_user()
        self.store.save_user(SUB, gmail_watch_expires=None)
        self.gmail.add("msg-1")
        self.gmail.watch.side_effect = gmail.GmailError(
            "Google API returned HTTP 403: User not authorized to perform this action.",
            status=403,
        )

        with self.assertLogs(runner.log, "WARNING") as logs:
            counts = runner.poll_all()

        self.assertEqual((counts["runs"], counts["failed"]), (1, 0))
        self.assertIn("couldn't start Gmail push", logs.output[0])
        self.assertIsNone(self.store.get_user(SUB)["gmail_watch_expires"])

    def test_start_watch_starts_it_now_even_if_not_due(self):
        self.add_user()  # push runs for days yet
        runner.start_watch(self.store.get_user(SUB))
        self.gmail.watch.assert_called_once_with(GMAIL_ACCESS_TOKEN, TOPIC)
        self.assertEqual(self.store.get_user(SUB)["gmail_watch_expires"], WATCH_ENDS)

    def test_start_watch_never_raises(self):
        self.add_user()
        self.store.save_user(SUB, gmail_refresh_token="1//revoked")
        with self.assertLogs(runner.log, "WARNING"):
            runner.start_watch(self.store.get_user(SUB))
        self.gmail.watch.assert_not_called()
