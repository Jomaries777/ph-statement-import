"""
ph-statement-import - Gmail auto-import page, Google sign-in and scheduler route
(Blueprint "auto_import").

Registered in web/app.py only when both config.HOSTED and GOOGLE_CLIENT_ID are
set, so local runs and hosted deploys without Google credentials expose none of
these routes:

    GET  /auto-import             - setup view (W2) when off, run log (W3) when on
    GET  /google/login            - send the browser to Google's consent screen
    GET  /google/callback         - check state, a verified email on ALLOWED_GOOGLE_EMAILS,
                                    save token
    POST /api/auto-import         - validate and save plan, mappings, password inputs
    POST /api/auto-import/test    - try password inputs on the newest statement email
    POST /api/auto-import/delete  - delete stored data, revoke Gmail, sign out
    POST /tasks/poll-gmail        - Cloud Scheduler hook (OIDC token verified first)

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
    GCP_PROJECT_ID         - optional override for the scheduler service account's
                             project (defaults to Cloud Run's metadata project ID)
"""

import http.client
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

from statement_etl.autoimport import gmail, runner, store
from statement_etl.sources import SOURCES
from statement_etl.web import config, ynab_oauth
from statement_etl.web.limits import AUTO_IMPORT_LIMIT, limiter
from statement_etl.web.state import error_response, session_state

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
        {"source": "gcash", "status": "ok", "created": 12, "skipped": 0, ...},
        {"gcash": {"account_name": "GCash"}},
    ) -> {"received": "...", "target": "GCash → GCash", "badge_kind": "ok", ...}
    """
    source_key = run.get("source", "")
    source_name = SOURCES[source_key].name if source_key in SOURCES else source_key
    account_name = (
        (mappings.get(source_key) or {}).get("account_name") or run.get("account_id") or "—"
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

    return {
        "received": _format_time(run.get("received_at") or run.get("ran_at")),
        "target": f"{source_name} → {account_name}",
        "badge_kind": badge_kind,
        "badge_text": badge_text,
        "result": result,
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
    mappings = (user or {}).get("mappings") or {}
    runs = [format_run(r, mappings) for r in store.list_runs(user["sub"], limit=20)] if user else []
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
        runs=runs,
        google_error=_google_error_text(request.args.get("google_error")),
    )


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
            new_access = (tokens.get("access_token") or "").strip()
            if not new_refresh or not new_access:
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
        # Keep only the short-lived access token in the browser session so the
        # session and the background runner never race on the same refresh token.
        state["token_kind"] = "oauth"
        state["access_token"] = new_access
        state["expires_at"] = time.time() + int(tokens.get("expires_in", 7200))
        state.pop("refresh_token", None)
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
    return jsonify({"enabled": True})


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

    # 2. Revoke the Gmail token with Google; finish the delete even if revoke fails
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

    # 3. Clear the Google part of the Flask session
    session.pop("google_sub", None)
    session.pop("google_oauth_state", None)
    return jsonify({"deleted": True, "revoked": revoked, "warning": warning})


# =============================================================================
# SCHEDULER ROUTE - Cloud Scheduler invokes every 15 minutes with an OIDC token
# =============================================================================


@blueprint.post("/tasks/poll-gmail")
def poll_gmail():
    """Verify Cloud Scheduler's OIDC bearer token, then poll all enabled users.

    POST /tasks/poll-gmail (Authorization: Bearer <OIDC token>)
    -> {"users": 1, "runs": 1, "failed": 0, "deferred": 0}
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not token.strip():
        return error_response("Forbidden.", 403)

    project_id = _project_id()
    if not project_id:
        return error_response("Forbidden.", 403)

    # Always build the audience from config.PUBLIC_URL, never from request.url
    # (a caller controls the Host header).
    audience = f"{config.PUBLIC_URL}/tasks/poll-gmail"
    try:
        claims = google.oauth2.id_token.verify_oauth2_token(
            token.strip(),
            google.auth.transport.requests.Request(),
            audience=audience,
        )
    except Exception:  # noqa: BLE001 - any verification failure is a 403
        return error_response("Forbidden.", 403)

    expected_email = f"scheduler-invoker@{project_id}.iam.gserviceaccount.com"
    if claims.get("email") != expected_email or claims.get("email_verified") is not True:
        return error_response("Forbidden.", 403)

    return jsonify(runner.poll_all())
