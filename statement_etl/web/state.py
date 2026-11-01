"""
ph-statement-import - the web app's server-side state, one entry per browser.

    STORE               - every browser session's state, in this process's memory
    session_state()     - this browser's entry in STORE
    drop_idle_states()  - hosted mode: forget browsers idle for IDLE_SECONDS
    error_response()    - the JSON error the page shows inside the step that failed

Used by both blueprints (ynab_auth.py and upload.py).

In hosted mode (config.HOSTED) a browser's entry, with its YNAB tokens and
parsed statement, is dropped after 30 minutes without a request. Locally it
stays until the app stops.
"""

import secrets
import time

from flask import jsonify, session

from statement_etl.web.config import HOSTED

# Server-side state, one entry per browser session:
#   {sid: {"access_token", "token_kind", "refresh_token", "expires_at",
#          "source", "rows", "transactions", "parsed", "progress"}}
#   source:     key in sources.SOURCES of the parsed statement
#   token_kind: "personal" (typed on the page, never expires), "saved" (loaded
#               from the OS secure store, never expires) or "oauth"
#   skip_saved: True after Disconnect, so the saved token isn't loaded again
#               until the next Connect
#   progress:   the step /api/parse or /api/upload is on while it runs (upload.py)
#   last_seen:  hosted mode only: time.time() of this browser's last request
# Tokens and parsed transactions never go into the browser cookie; the cookie
# only holds the random session id. Everything is lost when the app stops.
STORE = {}

IDLE_SECONDS = 30 * 60  # hosted mode: forget a browser after 30 minutes idle


def session_state():
    """This browser's entry in STORE (created on first use)."""
    if "sid" not in session:
        session["sid"] = secrets.token_urlsafe(32)
    state = STORE.setdefault(session["sid"], {})
    if HOSTED:
        state["last_seen"] = time.time()
    return state


def drop_idle_states():
    """Forget every browser with no request for more than IDLE_SECONDS.

    Run before each request in hosted mode (app.py), so nothing a visitor
    left behind stays on the server.
    """
    cutoff = time.time() - IDLE_SECONDS
    for sid, state in list(STORE.items()):  # a copy: other threads change STORE
        if state.get("last_seen", 0) < cutoff:
            STORE.pop(sid, None)


def error_response(message, status, **extra):
    """JSON error response the page shows inside the step that failed.

    error_response("Choose a PDF file.", 400) -> ({"error": "Choose a PDF file."}, 400)
    """
    return jsonify({"error": message, **extra}), status
