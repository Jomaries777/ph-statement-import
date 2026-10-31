"""
ph-statement-import - the web app's server-side state, one entry per browser.

    STORE            - every browser session's state, in this process's memory
    session_state()  - this browser's entry in STORE
    error_response() - the JSON error the page shows inside the step that failed

Used by both blueprints (ynab_auth.py and upload.py).
"""

import secrets

from flask import jsonify, session

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


def session_state():
    """This browser's entry in STORE (created on first use)."""
    if "sid" not in session:
        session["sid"] = secrets.token_urlsafe(32)
    return STORE.setdefault(session["sid"], {})


def error_response(message, status, **extra):
    """JSON error response the page shows inside the step that failed.

    error_response("Choose a PDF file.", 400) -> ({"error": "Choose a PDF file."}, 400)
    """
    return jsonify({"error": message, **extra}), status
