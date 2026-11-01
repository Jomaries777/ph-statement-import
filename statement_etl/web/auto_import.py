"""
ph-statement-import - Gmail auto-import page, Google sign-in, and the scheduler
and Gmail push routes (Blueprint "auto_import").

Registered in web/app.py only when both config.HOSTED and GOOGLE_CLIENT_ID are
set, so local runs and hosted deploys without Google credentials expose none of
these routes:

    GET  /auto-import             - setup view (W2) when off, run log (W3) when on
    GET  /auto-import/runs        - just the Recent imports card, which the page
                                    re-fetches while an import runs
    GET  /google/login            - send the browser to Google's consent screen
    GET  /google/callback         - check state, a verified email on ALLOWED_GOOGLE_EMAILS,
                                    save token
    POST /api/auto-import         - validate and save plan, mappings, password inputs
    POST /api/auto-import/test    - try password inputs on the newest statement email
    POST /api/auto-import/ynab-session - connect this browser to YNAB with the saved
                                    refresh token, for "Edit mappings"
    POST /api/auto-import/delete  - delete stored data, revoke Gmail, sign out
    POST /api/auto-import/runs/clear - hide the run log so far (runs stay stored)
    POST /tasks/poll-gmail        - Cloud Scheduler hook, every 15 minutes (OIDC token
                                    verified first)
    POST /tasks/gmail-push        - Pub/Sub push when a user's inbox changes (OIDC token
                                    verified first)

Why YNAB is refreshed once on save: YNAB refresh tokens are single-use (each
refresh rotates the token and invalidates the old one). Keeping the same
refresh token in both the browser session and Firestore would break whichever
side refreshes second. POST /api/auto-import refreshes once, saves the new
refresh token in Firestore for background polls, and leaves only the short-lived
access token in the browser session.

Environment (APP_CONFIG / environment):
    GOOGLE_CLIENT_ID       - Google OAuth 2.0 Web client ID (turns this blueprint on)
    GOOGLE_CLIENT_SECRET   - from APP_CONFIG (checked at startup by web/config.py)
    ENCRYPTION_KEYS        - from APP_CONFIG (MultiFernet keys for autoimport/store.py)
    ALLOWED_GOOGLE_EMAILS  - comma-separated invite list in APP_CONFIG (never in
                             deploy.yml, so personal email addresses stay out of
                             public GitHub Actions logs)
    GCP_PROJECT_ID         - optional override for the task service accounts'
                             project (defaults to Cloud Run's metadata project ID)
    GMAIL_PUSH_TOPIC       - Pub/Sub topic Gmail publishes inbox changes to, e.g.
                             "projects/<project>/topics/gmail-push". Empty leaves
                             push off: the 15-minute poll alone still imports.
"""

import base64
import binascii
import contextlib
import http.client
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta, timezone

import google.auth
import google.auth.exceptions
import google.auth.transport.requests
import google.oauth2.id_token
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session

from statement_etl import pipeline
from statement_etl.autoimport import gmail, runner, store
from statement_etl.sources import SOURCES
from statement_etl.web import config, ynab_oauth
from statement_etl.web.limits import AUTO_IMPORT_LIMIT, limiter
from statement_etl.web.state import error_response, session_state
from statement_etl.web.ynab_auth import get_token

blueprint = Blueprint("auto_import", __name__)

# Display timestamps in Philippine Standard Time (UTC+8, matching Cloud Scheduler)
# without needing system tzdata files inside the slim container image.
PHT = timezone(timedelta(hours=8))

# Generic metadata keyed by input name from Source.password_inputs (no source
# special cases). Adding a new source input only adds one entry here.
INPUT_FIELDS = {
    "last_name": {
        "label": "Last name",
        "type": "text",
        "placeholder": "Last name",
        "autocomplete": "family-name",
        "prefill_user_field": "family_name",
        "prefill_note": "from Google · edit",
    },
    "mobile_last4": {
        "label": "Last 4 of mobile",
        "type": "text",
        "placeholder": "Last 4 of mobile",
        "inputmode": "numeric",
        "maxlength": "4",
        "autocomplete": "off",
        "pattern": re.compile(r"^\d{4}$"),
        "pattern_error": "Last 4 of mobile must be 4 digits.",
    },
    # A date field: the browser sends YYYY-MM-DD whatever format it shows
    "birth_date": {
        "label": "Date of birth",
        "type": "date",
        "autocomplete": "bday",
        "pattern": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
        "pattern_error": "Date of birth must be a date.",
    },
}

GOOGLE_ERRORS = {
    "state_mismatch": "Google sign-in expired or didn't match this session. Try again.",
    "token_exchange_failed": "Could not finish Google sign-in. Try connecting Gmail again.",
    "missing_profile": "Google did not return an account ID and email address.",
    "email_not_verified": (
        "Google hasn't verified this account's email address. Verify it in your "
        "Google Account, then connect Gmail again."
    ),
    "missing_refresh_token": (
        "Google did not return a refresh token (consent wasn't granted). "
        "Remove this app in your Google Account → Security → Third-party access, "
        "then connect Gmail again."
    ),
}

# The steps an auto-import shows in Recent imports while it runs, in order:
# (step, while it runs, once done). See runner.Progress.
RUN_STEPS = (
    (runner.STEP_DOWNLOAD, "Downloading the PDF", "PDF downloaded"),
    (pipeline.STEP_READ, "Reading the statement", "Statement read"),
    (pipeline.STEP_UPLOAD, "Uploading to YNAB", "Uploaded to YNAB"),
    (pipeline.STEP_END_BALANCE, "Checking the balance", "Balance checked"),
)

# Progress not updated for this long belongs to a run that stopped part-way
# (e.g. Cloud Run restarted). Every step updates it, and a run ends within 60 s.
STALE_PROGRESS = timedelta(minutes=2)

# Failed runs say which step they stopped at
FAILED_STATUSES = (runner.PDF_LOCKED, runner.PARSE_ERROR, runner.YNAB_ERROR)

RUN_BADGES = {
    runner.OK: ("ok", "✓ Imported"),
    runner.BALANCE_MISMATCH: ("warn", "⚠ Balance"),
    runner.PDF_LOCKED: ("err", "✗ Password"),
    runner.PARSE_ERROR: ("err", "✗ Parse error"),
    runner.YNAB_ERROR: ("err", "✗ YNAB error"),
    runner.NO_ATTACHMENT: ("warn", "⚠ No PDF"),
}


# =============================================================================
# INVITE LIST & PROJECT ID
# =============================================================================


def is_email_allowed(email: str) -> bool:
    """True if `email` is in ALLOWED_GOOGLE_EMAILS (case-insensitive, trimmed).

    With ALLOWED_GOOGLE_EMAILS=" juan@example.test , Maria@Example.test ":
    is_email_allowed("MARIA@example.test ") -> True
    """
    candidate = email.strip().lower()
    if not candidate:
        return False
    allowed = {
        item.strip().lower()
        for item in os.environ.get("ALLOWED_GOOGLE_EMAILS", "").split(",")
        if item.strip()
    }
    return candidate in allowed


def _project_id() -> str | None:
    """GCP_PROJECT_ID if set, otherwise the Cloud Run metadata project ID."""
    explicit = os.environ.get("GCP_PROJECT_ID", "").strip()
    if explicit:
        return explicit
    try:
        _, project_id = google.auth.default()
    except google.auth.exceptions.GoogleAuthError:
        return None
    return project_id


# =============================================================================
# PAGE VIEW HELPERS - build generic source rows and formatted run-log entries
# =============================================================================


def auto_import_sources() -> list:
    """Every Source that declares Gmail auto-import settings (`email_query`).

    auto_import_sources() -> [Source(key="gcash", ...)]
    """
    return [source for source in SOURCES.values() if source.email_query]


def _input_spec(input_name: str, user: dict | None) -> dict:
    """Template metadata for one field in `Source.password_inputs`."""
    default_label = input_name.replace("_", " ").capitalize()
    base = INPUT_FIELDS.get(
        input_name,
        {
            "label": default_label,
            "type": "password",
            "placeholder": default_label,
            "autocomplete": "off",
        },
    )
    prefill_key = base.get("prefill_user_field")
    prefill_value = (user or {}).get(prefill_key, "") if prefill_key else ""
    return {
        "name": input_name,
        "label": base["label"],
        "type": base["type"],
        "placeholder": base.get("placeholder", base["label"]),
        "autocomplete": base.get("autocomplete", "off"),
        "inputmode": base.get("inputmode", ""),
        "maxlength": base.get("maxlength", ""),
        "value": prefill_value,
        "note": base.get("prefill_note", "") if prefill_value else "",
    }


def _password_hint(source) -> str:
    """One-line password rule from `source.build_password`'s docstring, if any."""
    doc = getattr(source.build_password, "__doc__", None) or ""
    first_line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return f"Asked once. {first_line}" if first_line else ""


def _source_rows(user: dict | None) -> list[dict]:
    """Template rows for Step 4, built solely from `Source` metadata."""
    mappings = (user or {}).get("mappings") or {}
    rows = []
    for source in auto_import_sources():
        mapping = mappings.get(source.key) or {}
        enabled = bool(mapping.get("enabled"))
        rows.append(
            {
                "key": source.key,
                "name": source.name,
                "enabled": enabled,
                "account_id": mapping.get("account_id", "") if enabled else "",
                "account_name": mapping.get("account_name", "") if enabled else "",
                "inputs": [_input_spec(name, user) for name in source.password_inputs],
                "password_hint": _password_hint(source),
            }
        )
    return rows


def _format_time(when: datetime | None, fmt: str = "%b %d, %I:%M %p") -> str:
    """Format a UTC datetime in Philippine time (`Oct 30, 09:02 AM`), or '—'."""
    if not isinstance(when, datetime):
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(PHT).strftime(fmt)


def format_run(run: dict, mappings: dict) -> dict:
    """Turn one stored run record into display strings for the Recent imports table.

    format_run(
        {"source": "gcash", "account_name": "GCash", "status": "ok", "created": 12, ...},
        {"gcash": {"account_name": "GCash1"}},
    ) -> {"received": "...", "wallet": "GCash", "account": "GCash", "badge_kind": "ok", ...}

    The account is the one the run imported into, even if the mapping has
    changed since. Runs saved before runs kept their account name fall back
    to the current mapping.
    """
    source_key = run.get("source", "")
    source_name = SOURCES[source_key].name if source_key in SOURCES else source_key
    account_name = (
        run.get("account_name")
        or (mappings.get(source_key) or {}).get("account_name")
        or run.get("account_id")
        or "—"
    )
    status = run.get("status", "")
    badge_kind, badge_text = RUN_BADGES.get(status, ("warn", status or "Unknown"))
    created = int(run.get("created") or 0)
    skipped = int(run.get("skipped") or 0)
    note = (run.get("message") or "").strip()

    if status in (runner.OK, runner.BALANCE_MISMATCH):
        counts = f"{created} created · {skipped} skipped"
        result = f"{counts} · {note}" if note else counts
    elif status == runner.NO_ATTACHMENT:
        result = note or "No matching PDF attachment"
    else:
        result = note or "Nothing uploaded"
    stopped_at = _step_name(run.get("step")) if status in FAILED_STATUSES else ""
    if stopped_at:
        result = f"Stopped at: {stopped_at}. {result}"

    seconds = run.get("duration_seconds")
    return {
        "received": _format_time(run.get("received_at") or run.get("ran_at")),
        "wallet": source_name,
        "account": account_name,
        "badge_kind": badge_kind,
        "badge_text": badge_text,
        "result": result,
        "duration": f"Took {seconds}s" if isinstance(seconds, int) else "",
    }


def _step_name(step: str | None) -> str:
    """A step's name while it runs: "upload" -> "Uploading to YNAB"; "" if unknown."""
    return next((running for name, running, _done in RUN_STEPS if name == step), "")


def format_progress(progress: dict | None, now: datetime | None = None) -> dict | None:
    """The import running now (runner.Progress), for the top of Recent imports.

    format_progress({"emails": [{"source": "gcash", ...}, ...], "index": 0,
                     "step": "upload", "rows": 72, ...})
    -> {"running": True, "summary": "Importing 1 of 2 new statement emails",
        "rows": [{"state": "running", "steps": [{"label": "PDF downloaded",
                  "state": "done"}, ...], ...}, {"state": "waiting", ...}], ...}

    Emails already done aren't listed: they're in the run log. None if no
    import is running.
    """
    if not progress or progress.get("index", -1) < 0:
        return None
    now = now or datetime.now(UTC)
    index, emails = progress["index"], progress["emails"]
    updated_at = progress.get("updated_at") or progress.get("started_at")
    running = isinstance(updated_at, datetime) and now - updated_at < STALE_PROGRESS

    # The current email's steps: done, the one running now, still to do
    position = next((n for n, step in enumerate(RUN_STEPS) if step[0] == progress["step"]), 0)
    steps = []
    for n, (name, while_running, once_done) in enumerate(RUN_STEPS):
        if name == pipeline.STEP_READ and progress.get("rows") and n < position:
            once_done = f"{progress['rows']} rows read"
        state = "done" if n < position else "now" if n == position else "todo"
        steps.append({"label": once_done if state == "done" else while_running, "state": state})

    rows = []
    for n, email in enumerate(emails[index:], start=index):
        source_key = email.get("source", "")
        row = {
            "received": _format_time(email.get("received_at")),
            "wallet": SOURCES[source_key].name if source_key in SOURCES else source_key,
            "account": email.get("account_name") or "—",
            "state": "waiting",
            "steps": [],
            "note": "Next in line",
        }
        if n == index:
            started_at = progress.get("started_at")
            seconds = int((now - started_at).total_seconds()) if started_at else 0
            row.update(state="running", steps=steps, note=f"started {seconds}s ago")
            if not running:
                row.update(
                    state="stopped", steps=[], note="Stopped. It's tried again at the next check."
                )
        rows.append(row)

    return {
        "running": running,
        "summary": f"Importing {index + 1} of {len(emails)} new statement emails"
        if running
        else "",
        "rows": rows,
        "done_steps": position,
        "total_steps": len(RUN_STEPS),
    }


def _recent_imports(user: dict | None) -> dict:
    """What the Recent imports card shows (templates/auto_import_runs.html)."""
    if not user:
        return {"runs": [], "runs_cleared": False, "progress": None}
    mappings = user.get("mappings") or {}
    runs_cleared_at = user.get("runs_cleared_at")
    runs = store.list_runs(user["sub"], limit=20, after=runs_cleared_at)
    return {
        "runs": [format_run(r, mappings) for r in runs],
        "runs_cleared": bool(runs_cleared_at),
        "progress": format_progress(user.get("progress")),
    }


def _current_user() -> dict | None:
    """The signed-in Google user's Firestore record (secrets still encrypted), or None."""
    sub = session.get("google_sub")
    if not sub:
        return None
    user = store.get_user(sub)
    if not user:
        session.pop("google_sub", None)
        return None
    return user


def _google_error_text(code: str | None) -> str:
    if not code:
        return ""
    if code in GOOGLE_ERRORS:
        return GOOGLE_ERRORS[code]
    return f"Could not connect Gmail ({code}). Try again."


# =============================================================================
# PAGE & GOOGLE SIGN-IN ROUTES
# =============================================================================


@blueprint.get("/auto-import")
def page():
    """Render the auto-import setup view (W2) or active run-log view (W3).

    GET /auto-import -> 200 HTML (or 403 invite-only page when ?not_invited=1)
    """
    if request.args.get("not_invited") == "1":
        return render_template("auto_import.html", not_invited=True), 403

    user = _current_user()
    gmail_connected = bool(user and user.get("gmail_refresh_token"))
    enabled = bool(gmail_connected and user.get("enabled"))
    last_checked = (
        _format_time(user.get("last_checked"), "%b %d, %I:%M %p PHT")
        if user and user.get("last_checked")
        else "not checked yet"
    )

    return render_template(
        "auto_import.html",
        not_invited=False,
        gmail_connected=gmail_connected,
        gmail_email=(user or {}).get("email", ""),
        has_stored_data=user is not None,
        has_saved_ynab=bool(user and user.get("ynab_refresh_token")),
        enabled=enabled,
        plan_id=(user or {}).get("plan_id", ""),
        plan_name=(user or {}).get("plan_name", ""),
        last_checked=last_checked,
        sources=_source_rows(user),
        google_error=_google_error_text(request.args.get("google_error")),
        **_recent_imports(user),
    )


@blueprint.get("/auto-import/runs")
def recent_imports():
    """Just the Recent imports card, so the page can refresh it while an import runs.

    GET /auto-import/runs -> 200 HTML (templates/auto_import_runs.html)
    """
    user = _current_user()
    if not user:
        return "Connect Gmail first.", 401
    if not is_email_allowed(user.get("email", "")):
        return "This Google account is not on the invite list.", 403
    return render_template("auto_import_runs.html", **_recent_imports(user))


@blueprint.get("/google/login")
@limiter.limit(AUTO_IMPORT_LIMIT)
def google_login():
    """Redirect the browser to Google's consent screen with a fresh CSRF state.

    GET /google/login -> 302 https://accounts.google.com/o/oauth2/v2/auth?...
    """
    if not gmail.oauth_configured():
        return "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set.", 500
    session["google_oauth_state"] = secrets.token_urlsafe(16)
    return redirect(gmail.authorize_url(session["google_oauth_state"]))


@blueprint.get("/google/callback")
@limiter.limit(AUTO_IMPORT_LIMIT)
def google_callback():
    """Verify state and invite list, require a refresh token, and save the Google user.

    GET /google/callback?code=...&state=... -> 302 /auto-import
    """
    if request.args.get("error"):
        session.pop("google_oauth_state", None)
        return redirect("/auto-import?google_error=" + urllib.parse.quote(request.args["error"]))

    expected_state = session.pop("google_oauth_state", None)
    if not request.args.get("state") or request.args.get("state") != expected_state:
        return redirect("/auto-import?google_error=state_mismatch")

    try:
        tokens = gmail.exchange_code(request.args.get("code", ""))
        claims = gmail.decode_id_token(tokens.get("id_token", ""))
    except gmail.GmailError:
        return redirect("/auto-import?google_error=token_exchange_failed")

    sub = (claims.get("sub") or "").strip()
    email = (claims.get("email") or "").strip()
    family_name = (claims.get("family_name") or "").strip()
    if not sub or not email:
        return redirect("/auto-import?google_error=missing_profile")

    # The invite list matches email addresses, so only trust one Google has verified
    if claims.get("email_verified") is not True:
        session.pop("google_sub", None)
        return redirect("/auto-import?google_error=email_not_verified")

    # Reject non-invited accounts before writing anything to Firestore or session
    if not is_email_allowed(email):
        session.pop("google_sub", None)
        return redirect("/auto-import?not_invited=1")

    refresh_token = (tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        return redirect("/auto-import?google_error=missing_refresh_token")

    existing = store.get_user(sub)
    fields = {
        "email": email,
        "family_name": family_name,
        "gmail_refresh_token": refresh_token,
    }
    if existing is None:
        fields["enabled"] = False
    store.save_user(sub, **fields)
    session["google_sub"] = sub
    return redirect("/auto-import")


# =============================================================================
# SETTINGS, TEST & DELETE API ROUTES
# =============================================================================


def _clean_source_inputs(source, raw_inputs: object) -> tuple[dict | None, str | None]:
    """Validate every input in `source.password_inputs` and test `build_password`."""
    if not isinstance(raw_inputs, dict):
        return None, f"Missing password inputs for {source.name}."
    cleaned = {}
    for name in source.password_inputs:
        spec = INPUT_FIELDS.get(name, {})
        value = raw_inputs.get(name)
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            label = spec.get("label", name)
            return None, f"Enter {label} for {source.name}."
        pattern = spec.get("pattern")
        if pattern and not pattern.fullmatch(text):
            return None, spec.get("pattern_error", f"Invalid {spec.get('label', name)}.")
        cleaned[name] = text
    if source.build_password is not None:
        try:
            source.build_password(cleaned)
        except ValueError:
            return None, f"Invalid password input for {source.name}."
    return cleaned, None


def _validate_settings(body: object) -> tuple[dict | None, str | None]:
    """Validate a POST /api/auto-import payload; returns `(cleaned, error_message)`."""
    if not isinstance(body, dict):
        return None, "Expected a JSON object."
    plan_id = (body.get("plan_id") or "").strip()
    plan_name = (body.get("plan_name") or "").strip()
    if not plan_id:
        return None, "Choose a YNAB plan."

    raw_mappings = body.get("mappings")
    raw_inputs = body.get("password_inputs")
    if not isinstance(raw_mappings, dict) or not raw_mappings:
        return None, "Map at least one e-wallet to a YNAB account."
    if raw_inputs is not None and not isinstance(raw_inputs, dict):
        return None, "Invalid password inputs."
    raw_inputs = raw_inputs or {}

    allowed_sources = {s.key: s for s in auto_import_sources()}
    for source_key in set(raw_mappings) | set(raw_inputs):
        if source_key not in SOURCES:
            return None, f"Unknown source {source_key!r}."
        if source_key not in allowed_sources:
            return None, f"Auto-import is not available for {SOURCES[source_key].name}."

    clean_mappings = {}
    clean_inputs = {}
    seen_accounts: dict[str, str] = {}
    for source_key, source in allowed_sources.items():
        entry = raw_mappings.get(source_key) or {}
        if not isinstance(entry, dict):
            return None, f"Invalid mapping for {source.name}."
        enabled = bool(entry.get("enabled"))
        account_id = (entry.get("account_id") or "").strip() if enabled else ""
        account_name = (entry.get("account_name") or "").strip() if enabled else ""
        if enabled:
            if not account_id:
                return None, f"Choose a YNAB account for {source.name}."
            if account_id in seen_accounts:
                other = seen_accounts[account_id]
                return None, (
                    f"{other} and {source.name} cannot use the same YNAB account, "
                    "because their balance checks would conflict."
                )
            seen_accounts[account_id] = source.name
            inputs, err = _clean_source_inputs(source, raw_inputs.get(source_key))
            if err:
                return None, err
            clean_inputs[source_key] = inputs
        clean_mappings[source_key] = {
            "account_id": account_id,
            "account_name": account_name or account_id,
            "enabled": enabled,
        }

    if not seen_accounts:
        return None, "Map at least one e-wallet to a YNAB account."

    return {
        "plan_id": plan_id,
        "plan_name": plan_name or plan_id,
        "mappings": clean_mappings,
        "password_inputs": clean_inputs,
    }, None


def _keep_access_token_only(state: dict, tokens: dict) -> None:
    """Keep only YNAB's short-lived access token in the browser session.

    The refresh token lives in Firestore alone, so the session and the
    background runner never race on the same single-use refresh token.
    """
    state["token_kind"] = "oauth"
    state["access_token"] = tokens["access_token"]
    state["expires_at"] = time.time() + int(tokens.get("expires_in", 7200))
    state.pop("refresh_token", None)


def _rotate_ynab_refresh_token(sub: str, user: dict):
    """Rotate the session's single-use YNAB refresh token into Firestore once.

    Returns None on success or an `error_response` tuple if YNAB isn't connected.
    """
    state = session_state()
    refresh_token = state.get("refresh_token")
    if refresh_token:
        try:
            tokens = ynab_oauth.refresh(refresh_token)
            new_refresh = (tokens.get("refresh_token") or "").strip()
            if not new_refresh or not (tokens.get("access_token") or "").strip():
                raise ValueError("missing token")
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            ValueError,
            KeyError,
        ):
            for key in ("access_token", "token_kind", "refresh_token", "expires_at"):
                state.pop(key, None)
            return error_response(
                "Your YNAB session expired. Reconnect to YNAB.", 401, reconnect=True
            )
        store.save_ynab_tokens(sub, {"refresh_token": new_refresh})
        _keep_access_token_only(state, tokens)
        return None

    if not user.get("ynab_refresh_token"):
        return error_response("Connect to YNAB first.", 401, reconnect=True)
    return None


@blueprint.post("/api/auto-import")
@limiter.limit(AUTO_IMPORT_LIMIT)
def save_settings():
    """Save the chosen plan, mappings and password inputs, and turn auto-import on.

    POST /api/auto-import {"plan_id": "...", "mappings": {...}, "password_inputs": {...}}
    -> {"enabled": True}
    """
    user = _current_user()
    if not user or not user.get("gmail_refresh_token"):
        return error_response("Connect Gmail first.", 401)
    if not is_email_allowed(user.get("email", "")):
        return error_response("This Google account is not on the invite list.", 403)

    cleaned, validation_error = _validate_settings(request.get_json(silent=True))
    if validation_error:
        return error_response(validation_error, 400)

    ynab_err = _rotate_ynab_refresh_token(user["sub"], user)
    if ynab_err:
        return ynab_err

    fields = {"enabled": True, **cleaned}
    # Stamp last_checked on first activation so old historical emails aren't imported
    if not user.get("last_checked"):
        fields["last_checked"] = datetime.now(UTC)
    store.save_user(user["sub"], **fields)

    # Gmail push, so a statement is imported within seconds, not at the next poll.
    # Never raises: without push, the 15-minute poll still imports.
    runner.start_watch(user)
    return jsonify({"enabled": True})


@blueprint.post("/api/auto-import/ynab-session")
@limiter.limit(AUTO_IMPORT_LIMIT)
def connect_saved_ynab():
    """Connect this browser to YNAB with auto-import's saved YNAB connection.

    The browser's own YNAB session ends after 30 minutes idle (state.py), and
    saving the settings moves its refresh token to Firestore. Without this,
    "Edit mappings" would ask to log in to YNAB although auto-import is
    still connected.

    POST /api/auto-import/ynab-session -> {"connected": True}
    -> {"connected": False} if there's no saved connection or YNAB rejects it
    """
    user = _current_user()
    if not user or not user.get("gmail_refresh_token"):
        return error_response("Connect Gmail first.", 401)
    if not is_email_allowed(user.get("email", "")):
        return error_response("This Google account is not on the invite list.", 403)

    state = session_state()
    if get_token(state):
        return jsonify({"connected": True})

    with runner.run_lock_held() as held:
        if not held:
            return error_response("Auto-import is importing right now. Try again in a minute.", 503)
        # Read it again: a run may have just replaced the refresh token
        user = store.get_user(user["sub"]) or {}
        if not user.get("ynab_refresh_token"):
            return jsonify({"connected": False})
        try:
            tokens = ynab_oauth.refresh(store.decrypt(user["ynab_refresh_token"]))
            if not tokens.get("access_token") or not tokens.get("refresh_token"):
                raise ValueError("missing token")
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            ValueError,
            KeyError,
            store.DecryptError,
        ):
            return jsonify({"connected": False})
        # YNAB has already replaced the old refresh token: save the new one first
        store.save_ynab_tokens(user["sub"], tokens)

    _keep_access_token_only(state, tokens)
    return jsonify({"connected": True})


@blueprint.post("/api/auto-import/test")
@limiter.limit(AUTO_IMPORT_LIMIT)
def test_source_password():
    """Try the typed password inputs on the newest statement email without importing.

    POST /api/auto-import/test {"source": "gcash", "inputs": {...}}
    -> {"ok": True, "message": "The password opens your newest GCash statement."}
    """
    user = _current_user()
    if not user or not user.get("gmail_refresh_token"):
        return error_response("Connect Gmail first.", 401)
    if not is_email_allowed(user.get("email", "")):
        return error_response("This Google account is not on the invite list.", 403)

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("Expected a JSON object.", 400)
    source_key = body.get("source")
    if not isinstance(source_key, str) or source_key not in SOURCES:
        return error_response(f"Unknown source {source_key!r}.", 400)
    source = SOURCES[source_key]
    if not source.email_query:
        return error_response(f"Auto-import is not available for {source.name}.", 400)

    inputs, err = _clean_source_inputs(source, body.get("inputs"))
    if err:
        return error_response(err, 400)

    try:
        ok, message = runner.test_password(user, source_key, inputs)
    except gmail.GmailError:
        return error_response("Could not reach Gmail right now. Try again in a minute.", 502)
    except store.DecryptError:
        return error_response("Stored Gmail connection could not be read. Reconnect Gmail.", 500)
    except Exception as e:  # noqa: BLE001 - PDF library errors can carry internal details
        current_app.logger.warning("Could not test statement PDF: %s", type(e).__name__)
        return error_response("Could not read the statement PDF.", 400)
    return jsonify({"ok": ok, "message": message})


@blueprint.post("/api/auto-import/runs/clear")
@limiter.limit(AUTO_IMPORT_LIMIT)
def clear_runs():
    """Hide the runs so far from Recent imports.

    The runs stay stored, so an email already imported is never imported
    again (see store.clear_runs()).

    POST /api/auto-import/runs/clear -> {"cleared": True}
    """
    user = _current_user()
    if not user:
        return error_response("Connect Gmail first.", 401)
    if not is_email_allowed(user.get("email", "")):
        return error_response("This Google account is not on the invite list.", 403)
    store.clear_runs(user["sub"])
    return jsonify({"cleared": True})


@blueprint.post("/api/auto-import/delete")
@limiter.limit(AUTO_IMPORT_LIMIT)
def delete_settings():
    """Delete the user's Firestore record and runs, revoke Gmail, and clear session.

    POST /api/auto-import/delete -> {"deleted": True, "revoked": True, "warning": None}
    """
    sub = session.get("google_sub")
    if not sub:
        return error_response("Not signed in to Google.", 401)

    user = store.get_user(sub)
    gmail_token = None
    decrypt_failed = False
    if user and user.get("gmail_refresh_token"):
        try:
            gmail_token = store.decrypt(user["gmail_refresh_token"])
        except store.DecryptError:
            decrypt_failed = True

    # 1. Remove the user document and all of its runs first
    store.delete_user(sub)

    # 2. Stop Gmail push, so Google stops telling the app about this inbox
    if gmail_token and user.get("gmail_watch_expires"):
        _stop_gmail_push(gmail_token)

    # 3. Revoke the Gmail token with Google; finish the delete even if revoke fails
    revoked = not decrypt_failed
    warning = None
    if gmail_token:
        try:
            gmail.revoke(gmail_token)
        except gmail.GmailError:
            revoked = False
    if not revoked:
        warning = (
            "Your stored data was deleted, but Google could not be reached to revoke "
            "Gmail access. Remove this app in your Google Account → Security → "
            "Third-party access."
        )

    # 4. Clear the Google part of the Flask session
    session.pop("google_sub", None)
    session.pop("google_oauth_state", None)
    return jsonify({"deleted": True, "revoked": revoked, "warning": warning})


def _stop_gmail_push(gmail_refresh_token: str) -> None:
    """Stop Gmail push for this token.

    A failure is ignored: revoking the token next ends push too, and pushes for
    a deleted user are ignored anyway.
    """
    with contextlib.suppress(gmail.GmailError):
        gmail.stop(gmail.refresh_access_token(gmail_refresh_token))


# =============================================================================
# TASK ROUTES - called by Google Cloud, never by a browser, each with an OIDC token
# =============================================================================


def _is_from_google_task(path: str, account: str) -> bool:
    """True if the request carries a Google-signed OIDC token for this route,
    from the service account `account`@<project>.iam.gserviceaccount.com.

    _is_from_google_task("/tasks/poll-gmail", "scheduler-invoker") -> True
    Each route checks its own audience and account, so a token for one route
    can't be used on the other.
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not token.strip():
        return False

    project_id = _project_id()
    if not project_id:
        return False

    # Always build the audience from config.PUBLIC_URL, never from request.url
    # (a caller controls the Host header).
    try:
        claims = google.oauth2.id_token.verify_oauth2_token(
            token.strip(),
            google.auth.transport.requests.Request(),
            audience=f"{config.PUBLIC_URL}{path}",
        )
    except Exception:  # noqa: BLE001 - any verification failure is a refusal
        return False

    expected_email = f"{account}@{project_id}.iam.gserviceaccount.com"
    return claims.get("email") == expected_email and claims.get("email_verified") is True


@blueprint.post("/tasks/poll-gmail")
def poll_gmail():
    """Verify Cloud Scheduler's OIDC bearer token, then poll all enabled users.

    POST /tasks/poll-gmail (Authorization: Bearer <OIDC token>)
    -> {"users": 1, "runs": 1, "failed": 0, "deferred": 0}
    """
    if not _is_from_google_task("/tasks/poll-gmail", "scheduler-invoker"):
        return error_response("Forbidden.", 403)
    return jsonify(runner.poll_all())


@blueprint.post("/tasks/gmail-push")
def gmail_push():
    """Verify Pub/Sub's OIDC bearer token, then import for the user whose inbox changed.

    POST /tasks/gmail-push (Authorization: Bearer <OIDC token>)
        {"message": {"data": base64 of {"emailAddress": "...", "historyId": "..."}}}
    -> 204; or 503 when another run is going, so Pub/Sub sends it again

    Everything but "busy" gets 204, failures too: Pub/Sub would otherwise
    resend a push that can't work (say, revoked Gmail access) until the message
    expires. The next 15-minute poll tries again instead.
    """
    if not _is_from_google_task("/tasks/gmail-push", "pubsub-push"):
        return error_response("Forbidden.", 403)

    email_address = _pushed_email_address(request.get_json(silent=True))
    if email_address and runner.process_push(email_address) == runner.PUSH_BUSY:
        return "", 503
    return "", 204


def _pushed_email_address(body: object) -> str:
    """The inbox's address from a Gmail push message, or "" if it has none.

    {"message": {"data": "eyJlbWFpbEFkZHJlc3MiOiAianVhbkBleGFtcGxlLnRlc3QifQ=="}}
    -> "juan@example.test"
    """
    message = body.get("message") if isinstance(body, dict) else None
    data = message.get("data") if isinstance(message, dict) else None
    if not isinstance(data, str):
        return ""
    try:
        decoded = json.loads(base64.b64decode(data, validate=True))
    except (ValueError, binascii.Error):  # not base64, or not JSON
        return ""
    address = decoded.get("emailAddress") if isinstance(decoded, dict) else None
    return address.strip() if isinstance(address, str) else ""
