#!/usr/bin/env python3
"""
ph-statement-import - local web front-end (statement PDF -> YNAB).

Handles every e-wallet / bank in sources.SOURCES; the user picks one in step 1.

Steps on the page:
    1. Pick the source, upload the statement PDF and enter its password
                                                    -> preview of the parsed rows
    2. Connect to YNAB: type your personal access token on the page (or use OAuth)
    3. Pick the plan and account                    -> starting balance check vs. YNAB
    4. Upload to YNAB                               -> table of created transactions
                                                       and ending balance check vs. YNAB

Parsing, the starting balance check and the upload reuse
pipeline.parse_statement(), starting_balance_check() and import_statement(),
the same functions as the command line; plans, accounts and the connection
check reuse ynab_api/accounts.py.
A balance mismatch is only a warning: the upload is always allowed. A newly
created YNAB account gets its Starting Balance set to the statement's on upload.

Requirements:
    pip install flask pdfplumber ynab python-dotenv keyring

The YNAB personal access token is typed on the page (YNAB -> Account Settings
-> Developer Settings) and checked with YNAB. With "Remember this token on this
computer" ticked it is saved in the OS secure store (token_store.py: macOS
Keychain, Windows Credential Manager, Linux Secret Service), so later runs
connect by themselves; "Forget saved token" removes it. Otherwise it is kept in
this app's memory for the browser session only. It is never in the cookie,
.env or the project folder. BEARER_TOKEN in .env is used by the command line
(pipeline.py) only, not by the web app.

Environment (.env):
    YNAB_CLIENT_ID      - optional, from your YNAB OAuth application; adds a
                          "Connect with YNAB login" button
    YNAB_CLIENT_SECRET  - optional, as YNAB_CLIENT_ID
    FLASK_SECRET_KEY    - random string that signs the session cookie
    APP_PORT            - optional, default 5050 (5000 is used by macOS AirPlay)

Usage:
    python -m statement_etl.web  # then open http://localhost:5050
"""

import io
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ynab
from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]
from flask import Flask, jsonify, redirect, render_template, request, session

from statement_etl.parsers.common import StatementError
from statement_etl.pipeline import (
    import_statement,
    parse_statement,
    starting_balance_check,
)
from statement_etl.sources import DEFAULT_SOURCE, SOURCES
from statement_etl.web import token_store
from statement_etl.ynab_api.accounts import get_accounts, get_plans, get_user
from statement_etl.ynab_api.balance import (
    balance_check_message,
    new_account_message,
)

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell

PORT = int(os.environ.get("APP_PORT", "5050"))
REDIRECT_URI = f"http://localhost:{PORT}/oauth/callback"
YNAB_AUTHORIZE_URL = "https://app.ynab.com/oauth/authorize"
YNAB_TOKEN_URL = "https://app.ynab.com/oauth/token"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Server-side state, one entry per browser session:
#   {sid: {"access_token", "token_kind", "refresh_token", "expires_at",
#          "source", "rows", "transactions", "parsed"}}
#   source:     key in sources.SOURCES of the parsed statement
#   token_kind: "personal" (typed on the page, never expires), "saved" (loaded
#               from the OS secure store, never expires) or "oauth"
#   skip_saved: True after Disconnect, so the saved token isn't loaded again
#               until the next Connect
# Tokens and parsed transactions never go into the browser cookie; the cookie
# only holds the random session id. Everything is lost when the app stops.
STORE = {}


# =============================================================================
# SESSION STATE
# =============================================================================


def _state():
    """This browser's entry in STORE (created on first use)."""
    if "sid" not in session:
        session["sid"] = secrets.token_urlsafe(32)
    return STORE.setdefault(session["sid"], {})


def _error(message, status, **extra):
    """JSON error response the page shows inside the step that failed."""
    return jsonify({"error": message, **extra}), status


# =============================================================================
# AUTH - personal access token from .env, or OAuth (the YNAB SDK has no OAuth
# support, so OAuth uses plain HTTP calls)
# =============================================================================


def _exchange_token(**params):
    """POST to YNAB's token endpoint (authorization_code or refresh_token grant)."""
    body = urllib.parse.urlencode(
        {
            "client_id": os.environ["YNAB_CLIENT_ID"],
            "client_secret": os.environ["YNAB_CLIENT_SECRET"],
            "redirect_uri": REDIRECT_URI,
            **params,
        }
    ).encode()
    with urllib.request.urlopen(YNAB_TOKEN_URL, data=body, timeout=30) as resp:
        return json.load(resp)


def _store_tokens(state, token):
    """Keep the OAuth tokens server-side; YNAB access tokens last 2 hours."""
    state["token_kind"] = "oauth"
    state["access_token"] = token["access_token"]
    state["refresh_token"] = token.get("refresh_token", state.get("refresh_token"))
    state["expires_at"] = time.time() + int(token.get("expires_in", 7200))


def _clear_tokens(state):
    for key in ("access_token", "token_kind", "refresh_token", "expires_at"):
        state.pop(key, None)


def _refresh(state):
    """Get a new access token with the refresh token. Returns False if that fails."""
    if not state.get("refresh_token"):
        return False
    try:
        token = _exchange_token(grant_type="refresh_token", refresh_token=state["refresh_token"])
    except (urllib.error.URLError, KeyError, ValueError):
        _clear_tokens(state)
        return False
    _store_tokens(state, token)
    return True


def get_token(state):
    """This session's token: the personal access token typed on the page, the
    token saved in the OS secure store, or the OAuth token (refreshed if it
    expires within 60 s). None if not connected."""
    if not state.get("access_token") and not state.get("skip_saved"):
        saved = token_store.load_token()  # remembered from an earlier run
        if saved:
            state["access_token"] = saved
            state["token_kind"] = "saved"
    if not state.get("access_token"):
        return None
    if state.get("token_kind") in ("personal", "saved"):
        return state["access_token"]  # personal access tokens don't expire
    if time.time() > state.get("expires_at", 0) - 60 and not _refresh(state):
        return None
    return state["access_token"]


def _oauth_configured():
    return bool(os.environ.get("YNAB_CLIENT_ID") and os.environ.get("YNAB_CLIENT_SECRET"))


@app.get("/oauth/login")
def oauth_login():
    """Send the browser to YNAB's authorize page."""
    if not _oauth_configured():
        return (
            "YNAB_CLIENT_ID and YNAB_CLIENT_SECRET are not set in .env. "
            "Create an OAuth application in YNAB Developer Settings first.",
            500,
        )
    session["oauth_state"] = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": os.environ["YNAB_CLIENT_ID"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": session["oauth_state"],
        }
    )
    return redirect(f"{YNAB_AUTHORIZE_URL}?{query}")


@app.get("/oauth/callback")
def oauth_callback():
    """YNAB redirects here with ?code=...; swap it for an access token."""
    if request.args.get("error"):
        return redirect("/?oauth_error=" + urllib.parse.quote(request.args["error"]))
    if not request.args.get("state") or request.args.get("state") != session.pop(
        "oauth_state", None
    ):
        return redirect("/?oauth_error=state_mismatch")
    try:
        token = _exchange_token(grant_type="authorization_code", code=request.args.get("code", ""))
    except (urllib.error.URLError, ValueError):
        return redirect("/?oauth_error=token_exchange_failed")
    _store_tokens(_state(), token)
    return redirect("/")


@app.post("/oauth/logout")
def oauth_logout():
    """Disconnect this session. With {"forget": true}, also remove the token saved
    in the OS secure store ("Forget saved token")."""
    state = _state()
    _clear_tokens(state)
    state["skip_saved"] = True  # don't reconnect from the saved token right away
    if (request.get_json(silent=True) or {}).get("forget"):
        token_store.delete_token()
    return jsonify({"connected": False})


# =============================================================================
# PERSONAL ACCESS TOKEN - typed on the page instead of BEARER_TOKEN in .env
# =============================================================================


@app.post("/api/token")
def api_token():
    """Connect with a YNAB personal access token typed on the page.

    Checked with UserApi.get_user() first; only a working token is kept, in
    STORE for this browser session. With {"remember": true} it is also saved in
    the OS secure store (token_store.py) for later runs.
    """
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return _error("Enter your YNAB personal access token.", 400)
    try:
        get_user(token)
    except ynab.exceptions.UnauthorizedException:
        return _error("YNAB rejected this token. Copy it again from YNAB Developer Settings.", 401)
    except ynab.exceptions.ApiException as e:
        return _error(f"YNAB returned {e.status}: {_error_detail(e)}", 502)

    # Remember it on this computer, if asked and a secure store exists
    saved, save_error = False, None
    if body.get("remember") and token_store.available():
        saved = token_store.save_token(token)
        if not saved:
            save_error = (
                f"Couldn't save the token in the {token_store.store_name()}; "
                "it will be used for this session only."
            )

    state = _state()
    _clear_tokens(state)
    state.pop("skip_saved", None)
    state["access_token"] = token
    state["token_kind"] = "saved" if saved else "personal"
    return jsonify(
        {
            "connected": True,
            "token_kind": state["token_kind"],
            "saved": saved,
            "save_error": save_error,
        }
    )


# =============================================================================
# YNAB CALLS - every call goes through the SDK via the ph-statement-import
# pipeline modules (ynab_api/accounts.py / pipeline.py)
# =============================================================================


def _error_detail(e):
    """YNAB's own error message from an SDK exception, e.g. 'account does not exist'."""
    detail = getattr(getattr(getattr(e, "data", None), "error", None), "detail", None)
    if detail:
        return detail
    try:
        return json.loads(e.body)["error"]["detail"]
    except (TypeError, ValueError, KeyError):
        return e.reason or "Unknown error"


def _ynab_call(fn):
    """Run fn(token) with this session's token and map SDK errors to JSON responses.

    Returns (result, None) on success or (None, error_response).
    On a 401 it refreshes an OAuth token once and retries; a rejected personal
    access token is forgotten, so the page asks for a new one.
    """
    state = _state()
    token = get_token(state)
    if not token:
        return None, _error("Connect to YNAB first.", 401, reconnect=True)

    for attempt in (1, 2):
        try:
            return fn(token), None
        except ynab.exceptions.UnauthorizedException:
            kind = state.get("token_kind")
            if kind in ("personal", "saved"):
                _clear_tokens(state)
                if kind == "saved":
                    # Revoked in YNAB: remove it so it isn't loaded again
                    token_store.delete_token()
                    message = (
                        "YNAB rejected your saved token (revoked?). It was "
                        "removed; enter a new one."
                    )
                else:
                    message = (
                        "YNAB rejected your personal access token (revoked?). Enter a new one."
                    )
                return None, _error(message, 401, reconnect=True)
            if attempt == 1 and _refresh(state):
                token = state["access_token"]
                continue
            _clear_tokens(state)
            return None, _error(
                "Your YNAB session expired. Reconnect to YNAB.", 401, reconnect=True
            )
        except ynab.exceptions.ApiException as e:
            return None, _error(*_api_error_message(e))
    return None, _error("YNAB request failed.", 502)


def _api_error_message(e):
    """The message and HTTP status the page shows for a YNAB SDK error other than 401.

    NotFoundException -> ("Plan or account not found.", 404)
    """
    if isinstance(e, ynab.exceptions.NotFoundException):
        return "Plan or account not found.", 404
    if isinstance(e, ynab.exceptions.BadRequestException):
        return f"YNAB: {_error_detail(e)}", 400
    return f"YNAB returned {e.status}: {_error_detail(e)}", 502


# =============================================================================
# PAGE AND API ROUTES
# =============================================================================


@app.get("/")
def index():
    return render_template("index.html", sources=SOURCES.values(), default_source=DEFAULT_SOURCE)


@app.get("/api/status")
def api_status():
    """Is YNAB connected, and is a parsed statement waiting? Restores the page
    after a reload or the OAuth redirect."""
    state = _state()
    connected, connect_error = False, None
    get_token(state)  # loads the saved token, if there is one
    if state.get("access_token"):
        _, err = _ynab_call(get_user)  # confirms the token still works
        connected = err is None
        if err:
            connect_error = err[0].get_json()["error"]
    return jsonify(
        {
            "connected": connected,
            "connect_error": connect_error,
            "token_kind": state.get("token_kind") if connected else None,
            "oauth_configured": _oauth_configured(),
            # show "Remember this token on this computer" only with a secure store
            "remember_available": token_store.available(),
            "store_name": token_store.store_name(),
            "parsed": state.get("parsed"),
        }
    )


@app.post("/api/parse")
def api_parse():
    """Parse the uploaded PDF (kept in memory only) and keep the transactions for upload."""
    upload = request.files.get("pdf")
    password = request.form.get("password", "")
    source = request.form.get("source") or DEFAULT_SOURCE
    if source not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        return _error(f"Unknown source {source!r}. Known sources: {known}", 400)
    src = SOURCES[source]
    if not upload or not upload.filename:
        return _error("Choose a PDF file.", 400)

    try:
        rows, transactions = parse_statement(io.BytesIO(upload.read()), password, source)
    except StatementError as e:
        # Written for the user (wrong password, totals off), so shown as it is
        return _error(str(e), 400)
    except Exception as e:  # noqa: BLE001 - e.g. not a PDF at all
        # Any other error can carry internal details, so the page gets a fixed message
        # and the log gets only the error type (never the PDF or its contents).
        app.logger.warning("Could not read the uploaded PDF: %s", type(e).__name__)
        return _error("Could not read the PDF. Is it the statement PDF?", 400)

    # Statement rows vs. balance-correction rows added by the parser
    real_rows = [r for r in rows if not src.is_correction_row(r)]
    balances = src.statement_balances(rows)
    parsed = {
        "source": source,
        "source_name": src.name,
        "filename": upload.filename,
        # the statement's own columns; the page styles them from the lists below
        "columns": list(src.columns),
        "rows": [[row[c] for c in src.columns] for row in rows],
        "amount_columns": list(src.amount_columns),
        "money_in_columns": list(src.money_in_columns),
        "money_out_columns": list(src.money_out_columns),
        "text_column": src.text_column,
        # indexes in "rows" of the balance-correction rows (highlighted on the page)
        "correction_rows": [i for i, r in enumerate(rows) if src.is_correction_row(r)],
        # one line under the table, e.g. the statement's totals
        "summary": src.summary(rows),
        "statement_count": len(real_rows),
        "count": len(transactions),  # what will be uploaded, corrections included
        # running-balance check; empty / None when the statement adds up
        "corrections": [
            {**c, "amount": f"{c['amount']:+,.2f}"} for c in src.balance_corrections(rows)
        ],
        "balance_warning": src.balance_warning(rows),
        # compared with the YNAB account balance before / after the upload
        "starting_balance": f"{balances['starting']:,.2f}",
        "ending_balance": f"{balances['ending']:,.2f}",
    }
    state = _state()
    state["source"] = source
    state["rows"] = rows
    state["transactions"] = transactions
    state["parsed"] = parsed
    return jsonify(parsed)


@app.get("/api/plans")
def api_plans():
    plans, err = _ynab_call(get_plans)
    return err or jsonify(plans)


@app.get("/api/plans/<plan_id>/accounts")
def api_accounts(plan_id):
    accounts, err = _ynab_call(lambda token: get_accounts(plan_id, token))
    return err or jsonify(accounts)


def _check_json(check):
    """A balance check (from ynab_api.balance / ynab_api.client) as JSON for the page."""
    return {
        "kind": check["kind"],
        "matches": check["matches"],
        "new_account": check["new_account"] is not None,
        "statement": f"{check['statement']:,.2f}",
        "ynab_before": _money(check["ynab_before"]),
        "ynab_after": _money(check["ynab_after"]),
        "difference": f"{check['difference']:+,.2f}",
        # starting check: transactions YNAB already has (skipped on upload)
        "already_in_ynab": check.get("already_in_ynab") or 0,
        "to_upload": check.get("to_upload"),
        "message": balance_check_message(check),
    }


def _money(amount):
    """Decimal -> "1,234.56"; None stays None."""
    return None if amount is None else f"{amount:,.2f}"


@app.post("/api/starting-balance")
def api_starting_balance():
    """Before the upload: YNAB account balance vs. the statement's starting balance.

    A mismatch is returned as a warning; the page still allows the upload.
    """
    state = _state()
    rows, transactions = state.get("rows"), state.get("transactions")
    body = request.get_json(silent=True) or {}
    plan_id, account_id = body.get("plan_id"), body.get("account_id")
    if not rows:
        return _error("Parse a statement first.", 400)
    if not plan_id or not account_id:
        return _error("Choose a plan and an account.", 400)

    check, err = _ynab_call(
        lambda token: starting_balance_check(
            rows, transactions, account_id, plan_id, token, source=state["source"]
        )
    )
    return err or jsonify(_check_json(check))


@app.post("/api/upload")
def api_upload():
    """Send the parsed transactions to the chosen plan / account.

    Before: a newly created account gets its Starting Balance set to the
    statement's. After: the YNAB balance is compared with the statement's
    ending balance (a warning only).
    """
    state = _state()
    transactions = state.get("transactions")
    body = request.get_json(silent=True) or {}
    plan_id, account_id = body.get("plan_id"), body.get("account_id")
    if not transactions:
        return _error("Parse a statement first.", 400)
    if not plan_id or not account_id:
        return _error("Choose a plan and an account.", 400)

    rows = state["rows"]

    # New account's Starting Balance, upload, ending balance check. After a
    # token refresh _ynab_call() runs it again, which is safe: YNAB skips
    # import_ids it already has, and the Starting Balance already matches.
    imported, err = _ynab_call(
        lambda token: import_statement(
            rows, transactions, account_id, plan_id, token, source=state["source"]
        )
    )
    if err:
        return err

    created = [
        {
            "date": str(t.var_date),
            "payee": t.payee_name or "",
            "category": t.category_name or "Uncategorized",
            "memo": t.memo or "",
            "amount": t.amount,  # milliunits; the page formats it
            # "red" on balance-correction rows, so the page can highlight them
            "flag": t.flag_color.value if t.flag_color else "",
        }
        for t in imported.created
    ]

    # The upload is already done, so a failed ending check is reported, not an error
    ending_check = _check_json(imported.ending_check) if imported.ending_check else None
    ending_error = _api_error_message(imported.ending_error)[0] if imported.ending_error else None

    return jsonify(
        {
            "created": created,
            "skipped": imported.skipped,
            "ynab_url": f"https://app.ynab.com/{plan_id}/accounts/{account_id}",
            "ending_check": ending_check,
            "ending_check_error": ending_error,
            # set when a new account's Starting Balance was set (or already matched)
            "new_account_message": (
                new_account_message(imported.new_account) if imported.new_account else None
            ),
        }
    )


def main():
    if not os.environ.get("FLASK_SECRET_KEY"):
        print("Note: FLASK_SECRET_KEY not set; using a random key for this run.", file=sys.stderr)
    if token_store.load_token():  # never print the token itself
        print(
            f"Using the saved YNAB token from the {token_store.store_name()}.",
            file=sys.stderr,
        )
    else:
        print("Enter your YNAB personal access token on the page to connect.", file=sys.stderr)
    if _oauth_configured():
        print(f"YNAB OAuth also available (redirect URI: {REDIRECT_URI}).", file=sys.stderr)
    print(f"Open http://localhost:{PORT}", file=sys.stderr)
    # Bind to this Mac only; nothing is reachable from the network
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    main()
