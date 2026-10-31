"""
ph-statement-import - auto-import: find new statement emails in Gmail and import them.

Two things start a run (web/auto_import.py), and there's no browser, so
everything comes from the user's stored settings (store.py):
    - Gmail push: Gmail tells a Pub/Sub topic when a user's inbox changes, and
      Pub/Sub posts that to POST /tasks/gmail-push within seconds -> process_push()
    - Cloud Scheduler, every 15 minutes: POST /tasks/poll-gmail -> poll_all().
      It catches anything a push missed and keeps each user's push going.

    poll_all()                - every user with auto-import on. Never raises.
    process_push(email)       - the one user whose inbox changed. Never raises.
      process_user(user)        1. a fresh Gmail access token
                                2. renew Gmail push if it ends within a day
                                3. find_new_emails() for each source mapped to an account
                                4. only if there are any: a fresh YNAB access token (save
                                   YNAB's new refresh token), then process_message() each
                                5. remember when Gmail was checked
        find_new_emails(...)    - search Gmail for the source's statement emails since
                                  the last check, oldest first, skipping emails
                                  already in the run log
        process_message(...)    1. the PDF attachment, in memory only
                                2. the PDF password, built from the user's inputs
                                3. parse it (pipeline.parse_statement)
                                4. import it, unapproved (pipeline.import_statement)
                                5. record the run, with its status
    start_watch(user)         - start Gmail push now, when auto-import is turned on
    test_password(...)        - steps 1-3 on the newest email, for the page's Test button

Why YNAB waits until step 4: Gmail pushes on every inbox change (each email,
each one read), so most runs find nothing. YNAB replaces its refresh token on
every refresh, so refreshing only when there's something to import keeps that
to once per statement.

What happens when something fails:
    - A problem with one email (wrong password, a statement that won't parse,
      YNAB rejecting the upload) is recorded as that email's run, so it isn't
      tried again every 15 minutes. The run log on the page shows it.
    - A problem that may pass (Gmail or YNAB unreachable or busy) records no
      run and leaves last_checked as it was, so the next poll tries again.
      import_statement() is safe to repeat: YNAB skips what it already has.
    - A problem with one user (e.g. they revoked Gmail access) is logged and
      skipped; the other users still run.
    - A push that fails is only logged: the next poll tries again.

Logs: one line per run and per failed user, with a hash of the user's Google
ID, the source and the status. Never the email address, a password, a token
or anything from the statement.
"""

import hashlib
import io
import json
import logging
import os
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta

import ynab

from statement_etl import pipeline
from statement_etl.autoimport import gmail, store
from statement_etl.parsers.common import PasswordError, StatementError
from statement_etl.sources import SOURCES, Source, get_source
from statement_etl.web import ynab_oauth

log = logging.getLogger(__name__)

# A run's status: what happened to one email for one source, shown in the run log
OK = "ok"
PDF_LOCKED = "wrong password"  # the user's inputs don't open the PDF
PARSE_ERROR = "parse error"
BALANCE_MISMATCH = "balance mismatch"
YNAB_ERROR = "YNAB error"
NO_ATTACHMENT = "no attachment"

# Search from a little before the last check: Gmail can take a while to make a
# new email searchable. run_exists() skips the emails that were already done.
SEARCH_OVERLAP = timedelta(hours=1)

# Cloud Run ends the request after 60 seconds. Users not reached in time are
# checked first in the next poll (poll_all() starts with the longest-unchecked).
TIME_BUDGET_SECONDS = 40

# One run at a time: a push and a poll can arrive together, and two YNAB
# refreshes with the same single-use refresh token would fail one of them.
# Cloud Run runs a single instance (deploy.yml), so a lock in memory is enough.
_lock = threading.Lock()
LOCK_WAIT_SECONDS = 20

# Gmail push lasts about 7 days. Renew it once less than this is left; the
# 15-minute poll gets there long before it ends.
WATCH_RENEW_BEFORE = timedelta(days=1)

# What process_push() did, for the route's reply to Pub/Sub
PUSH_DONE = "done"
PUSH_IGNORED = "ignored"  # no user with auto-import on has this address
PUSH_BUSY = "busy"  # another run held the lock: Pub/Sub sends the push again
PUSH_FAILED = "failed"  # logged; the next poll tries again

_NEVER = datetime.min.replace(tzinfo=UTC)


def user_id(user: dict) -> str:
    """A short hash of the user's Google ID, for the logs: "1234" -> "03ac674216f3"."""
    return hashlib.sha256(user["sub"].encode()).hexdigest()[:12]


def _reason(e: Exception) -> str:
    """Why something failed, safe to log: no tokens, email addresses or statement text."""
    if isinstance(e, gmail.GmailError):  # its message is Google's error, never a token
        return str(e)
    if isinstance(e, ynab.ApiException):  # its str() has YNAB's whole response
        return f"YNAB returned HTTP {e.status}"
    if isinstance(e, urllib.error.HTTPError):  # from ynab_oauth.refresh()
        return f"YNAB login returned HTTP {e.code}"
    return type(e).__name__


def _try_again_later(e: ynab.ApiException) -> bool:
    """True for a YNAB error that may pass: busy (429) or down (5xx)."""
    return e.status == 429 or (e.status or 0) >= 500


# =============================================================================
# POLL - every user, then each of their sources, then each new email
# =============================================================================


def poll_all() -> dict:
    """Check every enabled user's Gmail once and import what's new. Never raises.

    poll_all() -> {"users": 2, "runs": 1, "failed": 0, "deferred": 0}
    users: checked; runs: emails imported or recorded; failed: users that hit an
    error (logged); deferred: users left for the next poll (time budget).
    """
    counts = {"users": 0, "runs": 0, "failed": 0, "deferred": 0}
    if not _lock.acquire(timeout=LOCK_WAIT_SECONDS):
        log.warning("Auto-import poll skipped: another run is still going.")
        return counts
    try:
        return _poll_all(counts)
    finally:
        _lock.release()


def _poll_all(counts: dict) -> dict:
    """poll_all(), with the lock held."""
    started = time.monotonic()
    try:
        users = store.list_enabled_users()
    except Exception as e:
        log.error("Auto-import: couldn't read the users (%s).", _reason(e))
        return counts

    for user in sorted(users, key=lambda u: u.get("last_checked") or _NEVER):
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            counts["deferred"] += 1
            continue
        counts["users"] += 1
        try:
            counts["runs"] += process_user(user)
        except Exception as e:  # one user's problem doesn't stop the others
            counts["failed"] += 1
            log.warning("Auto-import user %s failed: %s.", user_id(user), _reason(e))

    log.info("Auto-import poll: %s", counts)
    return counts


def process_push(email_address: str) -> str:
    """Import new statement emails for the user whose inbox just changed. Never raises.

    process_push("Juan@Example.com") -> "done"
    Returns PUSH_DONE, PUSH_IGNORED, PUSH_BUSY or PUSH_FAILED (see above).
    """
    wanted = email_address.strip().casefold()
    if not wanted:
        return PUSH_IGNORED
    if not _lock.acquire(timeout=LOCK_WAIT_SECONDS):
        return PUSH_BUSY
    try:
        try:
            # A few reads: only the users with auto-import on
            users = store.list_enabled_users()
        except Exception as e:
            log.error("Auto-import push: couldn't read the users (%s).", _reason(e))
            return PUSH_FAILED
        user = next((u for u in users if (u.get("email") or "").strip().casefold() == wanted), None)
        if user is None:
            return PUSH_IGNORED
        try:
            runs = process_user(user)
        except Exception as e:  # the next poll tries again
            log.warning("Auto-import push for user %s failed: %s.", user_id(user), _reason(e))
            return PUSH_FAILED
        log.info("Auto-import push: user %s, %s runs.", user_id(user), runs)
        return PUSH_DONE
    finally:
        _lock.release()


def process_user(user: dict) -> int:
    """Import new statement emails for one user; returns how many runs it recorded.

    Raises on a problem with the user as a whole, e.g. revoked Gmail access.
    """
    checked_at = datetime.now(UTC)

    # 1. A fresh Gmail access token
    gmail_token = gmail.refresh_access_token(store.decrypt(user["gmail_refresh_token"]))

    # 2. Keep Gmail push going
    _renew_watch(user, gmail_token)

    # 3. New emails for each source mapped to a YNAB account ("Don't import" ones are off)
    new_emails = []  # (source, mapping, Gmail message ID), oldest first per source
    for source_key, mapping in user.get("mappings", {}).items():
        if mapping.get("enabled"):
            source, msg_ids = find_new_emails(user, source_key, gmail_token)
            new_emails += [(source, mapping, msg_id) for msg_id in msg_ids]

    # 4. Only now a fresh YNAB access token. YNAB rotates refresh tokens, and the
    #    old one stops working, so the new one is saved before anything else can fail.
    if new_emails:
        ynab_tokens = ynab_oauth.refresh(store.decrypt(user["ynab_refresh_token"]))
        store.save_ynab_tokens(user["sub"], ynab_tokens)
        for source, mapping, msg_id in new_emails:
            process_message(user, msg_id, source, mapping, gmail_token, ynab_tokens["access_token"])

    # 5. Next time, search from here. Not reached if anything above raised, so
    #    the emails since the last check are searched again.
    store.set_last_checked(user["sub"], checked_at)
    return len(new_emails)


def find_new_emails(
    user: dict, source_key: str, gmail_token: str
) -> tuple[Source | None, list[str]]:
    """This source's statement emails that aren't in the run log yet, oldest first.

    find_new_emails(user, "gcash", "token") -> (<the gcash Source>, ["18c2f0a1b2"])
    """
    source = SOURCES.get(source_key)
    if source is None or not source.email_query:
        log.warning("Auto-import user %s: no email settings for %s.", user_id(user), source_key)
        return source, []

    # The page sets last_checked when auto-import is turned on, so older emails
    # are never imported; without it, only the last SEARCH_OVERLAP is searched.
    since = (user.get("last_checked") or datetime.now(UTC)) - SEARCH_OVERLAP
    messages = gmail.list_messages(gmail_token, source.email_query, after=since)

    # Gmail lists the newest first. Oldest first keeps statements in order, so
    # each one's ending balance check sees the ones before it.
    return source, [
        m["id"]
        for m in reversed(messages)
        if not store.run_exists(user["sub"], m["id"], source_key)
    ]


def process_message(
    user: dict, msg_id: str, source, mapping: dict, gmail_token: str, ynab_token: str
) -> str:
    """Import one statement email into the mapped account and record the run; returns its status.

    Raises, recording nothing, when Gmail or YNAB may work next time.
    """
    received_at = _received_at(gmail_token, msg_id)
    result = {"created": 0, "skipped": 0, "message": ""}
    status = _import_email(user, msg_id, source, mapping, gmail_token, ynab_token, result)

    # 5. Record the run, so this email isn't imported again
    store.write_run(
        user["sub"],
        msg_id,
        source.key,
        account_id=mapping["account_id"],
        status=status,
        received_at=received_at,
        **result,
    )
    log.info("Auto-import run: user %s, %s, %s.", user_id(user), source.key, status)
    return status


def _import_email(user, msg_id, source, mapping, gmail_token, ynab_token, result) -> str:
    """Steps 1-4 of process_message(). Fills in `result` and returns the run's status."""
    # 1. The PDF attachment, in memory only
    attachment = gmail.get_pdf_attachment(gmail_token, msg_id, source.attachment_pattern)
    if attachment is None:
        return NO_ATTACHMENT
    _filename, pdf_bytes = attachment

    # 2. The PDF password, built from the inputs the user entered once
    password = pdf_password(user, source)

    # 3. Parse
    try:
        rows, transactions = pipeline.parse_statement(io.BytesIO(pdf_bytes), password, source.key)
    except PasswordError:
        result["message"] = "Check the password inputs on the auto-import page."
        return PDF_LOCKED
    except StatementError as e:  # its message is written to be shown
        result["message"] = str(e)
        return PARSE_ERROR
    except Exception:  # a PDF library's message can carry internal details
        result["message"] = "The PDF couldn't be read."
        return PARSE_ERROR

    # 4. Import, unapproved, the same way as a manual upload
    try:
        imported = pipeline.import_statement(
            rows,
            transactions,
            mapping["account_id"],
            user["plan_id"],
            ynab_token,
            source=source.key,
        )
    except ynab.ApiException as e:
        if _try_again_later(e):
            raise  # YNAB is busy or down: nothing recorded, so the next poll tries again
        result["message"] = f"{_reason(e)}."
        return YNAB_ERROR

    result["created"] = len(imported.created)
    result["skipped"] = imported.skipped
    if imported.ending_check is None:  # the upload worked; only the check after it failed
        result["message"] = "Imported, but YNAB's balance couldn't be checked."
    elif not imported.ending_check["matches"]:
        result["message"] = "The YNAB balance doesn't match the statement's ending balance."
        return BALANCE_MISMATCH
    return OK


def _received_at(gmail_token: str, msg_id: str) -> datetime | None:
    """When Gmail received the email, for the run log; None if Gmail doesn't say."""
    internal_date = gmail.get_message(gmail_token, msg_id).get("internalDate")  # epoch ms, as text
    if not internal_date:
        return None
    return datetime.fromtimestamp(int(internal_date) / 1000, UTC)


def _renew_watch(user: dict, gmail_token: str) -> None:
    """Start or renew Gmail push for this user if it ends within a day.

    A Gmail error is logged, not raised: importing works without push.
    Does nothing when GMAIL_PUSH_TOPIC isn't set: then only the poll runs.
    """
    topic = os.environ.get("GMAIL_PUSH_TOPIC", "").strip()
    expires = user.get("gmail_watch_expires")
    if not topic or (expires and expires - datetime.now(UTC) > WATCH_RENEW_BEFORE):
        return
    try:
        expires = gmail.watch(gmail_token, topic)
    except gmail.GmailError as e:  # the next poll tries again; importing still works
        log.warning(
            "Auto-import user %s: couldn't start Gmail push: %s.", user_id(user), _reason(e)
        )
        return
    store.save_user(user["sub"], gmail_watch_expires=expires)


def start_watch(user: dict) -> None:
    """Start Gmail push right away, when auto-import is turned on. Never raises.

    Otherwise the first statement would wait for the next poll to start it.
    """
    if not os.environ.get("GMAIL_PUSH_TOPIC", "").strip():
        return
    try:
        gmail_token = gmail.refresh_access_token(store.decrypt(user["gmail_refresh_token"]))
    except (gmail.GmailError, store.DecryptError) as e:
        log.warning(
            "Auto-import user %s: couldn't start Gmail push: %s.", user_id(user), _reason(e)
        )
        return
    _renew_watch({**user, "gmail_watch_expires": None}, gmail_token)


def pdf_password(user: dict, source, inputs: dict | None = None) -> str:
    """The PDF password from the user's stored inputs, or from `inputs` if given.

    pdf_password(user, gcash_source) -> "delacruz1234"
    """
    if inputs is None:
        inputs = json.loads(store.decrypt(user["password_inputs"][source.key]))
    return source.build_password(inputs)


# =============================================================================
# TEST BUTTON - try the password on the newest email, without importing
# =============================================================================


def test_password(user: dict, source_key: str, inputs: dict) -> tuple[bool, str]:
    """Try password `inputs` on the newest statement email for `source_key`. Imports nothing.

    For the page's Test button, before the inputs are saved:
    test_password(user, "gcash", {"last_name": "Dela Cruz", "mobile_last4": "1234"})
    -> (True, "The password opens your newest GCash statement.")
    -> (False, "Wrong password. Check the inputs.")
    Raises GmailError if Gmail can't be reached.
    """
    source = get_source(source_key)

    # 1. The newest statement email's PDF
    gmail_token = gmail.refresh_access_token(store.decrypt(user["gmail_refresh_token"]))
    messages = gmail.list_messages(gmail_token, source.email_query)
    if not messages:
        return False, f"No {source.name} statement email in Gmail yet. Request one in the app."
    attachment = gmail.get_pdf_attachment(gmail_token, messages[0]["id"], source.attachment_pattern)
    if attachment is None:
        return False, f"The newest {source.name} statement email has no PDF attached."

    # 2. and 3. Build the password and parse
    try:
        pipeline.parse_statement(
            io.BytesIO(attachment[1]), pdf_password(user, source, inputs), source.key
        )
    except PasswordError:
        return False, "Wrong password. Check the inputs."
    except StatementError as e:
        return False, f"The password works, but the statement couldn't be read: {e}"
    return True, f"The password opens your newest {source.name} statement."
