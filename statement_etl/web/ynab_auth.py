"""
ph-statement-import - connecting the web app to YNAB (Blueprint "ynab_auth").

Routes:
    GET  /oauth/login     - send the browser to YNAB's login page
    GET  /oauth/callback  - YNAB sends it back here; swap the code for a token
    POST /oauth/logout    - disconnect (and optionally forget the saved token)
    POST /api/token       - connect with a personal access token typed on the page

Used by upload.py for every YNAB call:
    get_token()          - this session's token (saved, typed or OAuth, refreshed)
    ynab_call()          - run one YNAB call and turn SDK errors into page errors
    api_error_message()  - the message and status for a YNAB SDK error

The token is kept server-side in STORE (state.py), never in the cookie. A
token typed with "Remember this token on this computer" is also saved in the
OS secure store (token_store.py). The OAuth HTTP calls are in ynab_oauth.py.
"""

import json
import os
import secrets
import time
import urllib.error
import urllib.parse

import ynab
from flask import Blueprint, jsonify, redirect, request, session

from statement_etl.web import token_store, ynab_oauth
from statement_etl.web.config import REDIRECT_URI
from statement_etl.web.state import error_response, session_state
from statement_etl.ynab_api.accounts import get_user

YNAB_AUTHORIZE_URL = "https://app.ynab.com/oauth/authorize"

blueprint = Blueprint("ynab_auth", __name__)


# =============================================================================
# TOKENS - personal access token, saved token, or OAuth
# =============================================================================


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
        token = ynab_oauth.refresh(state["refresh_token"])
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


# =============================================================================
# OAUTH ROUTES
# =============================================================================


@blueprint.get("/oauth/login")
def oauth_login():
    """Send the browser to YNAB's authorize page."""
    if not ynab_oauth.oauth_configured():
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


@blueprint.get("/oauth/callback")
def oauth_callback():
    """YNAB redirects here with ?code=...; swap it for an access token."""
    if request.args.get("error"):
        return redirect("/?oauth_error=" + urllib.parse.quote(request.args["error"]))
    if not request.args.get("state") or request.args.get("state") != session.pop(
        "oauth_state", None
    ):
        return redirect("/?oauth_error=state_mismatch")
    try:
        token = ynab_oauth.exchange_token(
            grant_type="authorization_code", code=request.args.get("code", "")
        )
    except (urllib.error.URLError, ValueError):
        return redirect("/?oauth_error=token_exchange_failed")
    _store_tokens(session_state(), token)
    return redirect("/")


@blueprint.post("/oauth/logout")
def oauth_logout():
    """Disconnect this session. With {"forget": true}, also remove the token saved
    in the OS secure store ("Forget saved token")."""
    state = session_state()
    _clear_tokens(state)
    state["skip_saved"] = True  # don't reconnect from the saved token right away
    if (request.get_json(silent=True) or {}).get("forget"):
        token_store.delete_token()
    return jsonify({"connected": False})


# =============================================================================
# PERSONAL ACCESS TOKEN - typed on the page instead of BEARER_TOKEN in .env
# =============================================================================


@blueprint.post("/api/token")
def api_token():
    """Connect with a YNAB personal access token typed on the page.

    Checked with UserApi.get_user() first; only a working token is kept, in
    STORE for this browser session. With {"remember": true} it is also saved in
    the OS secure store (token_store.py) for later runs.
    """
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return error_response("Enter your YNAB personal access token.", 400)
    try:
        get_user(token)
    except ynab.exceptions.UnauthorizedException:
        return error_response(
            "YNAB rejected this token. Copy it again from YNAB Developer Settings.", 401
        )
    except ynab.exceptions.ApiException as e:
        return error_response(f"YNAB returned {e.status}: {_error_detail(e)}", 502)

    # Remember it on this computer, if asked and a secure store exists
    saved, save_error = False, None
    if body.get("remember") and token_store.available():
        saved = token_store.save_token(token)
        if not saved:
            save_error = (
                f"Couldn't save the token in the {token_store.store_name()}; "
                "it will be used for this session only."
            )

    state = session_state()
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


def ynab_call(fn):
    """Run fn(token) with this session's token and map SDK errors to JSON responses.

    Returns (result, None) on success or (None, error_response).
    On a 401 it refreshes an OAuth token once and retries; a rejected personal
    access token is forgotten, so the page asks for a new one.
    """
    state = session_state()
    token = get_token(state)
    if not token:
        return None, error_response("Connect to YNAB first.", 401, reconnect=True)

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
                return None, error_response(message, 401, reconnect=True)
            if attempt == 1 and _refresh(state):
                token = state["access_token"]
                continue
            _clear_tokens(state)
            return None, error_response(
                "Your YNAB session expired. Reconnect to YNAB.", 401, reconnect=True
            )
        except ynab.exceptions.ApiException as e:
            return None, error_response(*api_error_message(e))
    return None, error_response("YNAB request failed.", 502)


def api_error_message(e):
    """The message and HTTP status the page shows for a YNAB SDK error other than 401.

    NotFoundException -> ("Plan or account not found.", 404)
    """
    if isinstance(e, ynab.exceptions.NotFoundException):
        return "Plan or account not found.", 404
    if isinstance(e, ynab.exceptions.BadRequestException):
        return f"YNAB: {_error_detail(e)}", 400
    return f"YNAB returned {e.status}: {_error_detail(e)}", 502
