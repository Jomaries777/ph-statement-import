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

This file creates the Flask app, serves the /privacy and /terms pages, and
starts it. The other routes are in blueprints, one per job:
    upload.py      - the page, parsing, plans / accounts, balance checks, upload
    ynab_auth.py   - connecting to YNAB: personal access token or OAuth login
    auto_import.py - Gmail auto-import (hosted mode with GOOGLE_CLIENT_ID only)
and their helpers:
    state.py       - STORE, this browser's server-side state
    ynab_oauth.py  - YNAB OAuth token calls (no Flask session)
    config.py      - PORT, the OAuth REDIRECT_URI and HOSTED (hosted mode)
    limits.py      - rate limits (hosted mode only)
    token_store.py - the token saved in the OS secure store
The page is templates/index.html, with its CSS and JavaScript in static/app.css
and static/app.js (no inline code, so a strict Content-Security-Policy works).
Every response gets security headers (SECURITY_HEADERS below). Hosted mode
also gets HTTPS-only cookies, rate limits and idle expiry of STORE entries.
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
    PUBLIC_URL          - empty for local use. Set (e.g. https://statements.example.com),
                          it turns on hosted mode: YNAB OAuth login only, no
                          personal access tokens, no OS secure store. FLASK_SECRET_KEY,
                          YNAB_CLIENT_ID and YNAB_CLIENT_SECRET are then required,
                          and can come in one APP_CONFIG JSON (see config.py).

Usage:
    python -m statement_etl.web  # then open http://localhost:5050
"""

import os
import secrets
import sys

from flask import Flask, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from statement_etl.web import auto_import, token_store, upload, ynab_auth
from statement_etl.web.config import HOSTED, PORT, REDIRECT_URI
from statement_etl.web.limits import limiter
from statement_etl.web.state import drop_idle_states, error_response
from statement_etl.web.ynab_oauth import oauth_configured

# On every response, in both modes. The page has no inline script or style
# (static/app.js and app.css), so the Content-Security-Policy needs no
# 'unsafe-inline'. form-action allows the redirect to YNAB's login page.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self' https://app.ynab.com"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

# The privacy and terms pages: change the date with every change to them
LEGAL_LAST_UPDATED = "31 October 2026"
CONTACT_EMAIL = "froze.settle-1p@icloud.com"

AUTO_IMPORT = HOSTED and bool(os.environ.get("GOOGLE_CLIENT_ID"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.jinja_env.globals["auto_import_enabled"] = AUTO_IMPORT
app.register_blueprint(upload.blueprint)
app.register_blueprint(ynab_auth.blueprint)
if AUTO_IMPORT:
    app.register_blueprint(auto_import.blueprint)
limiter.init_app(app)  # does nothing locally: limits.limiter is on in hosted mode only

if HOSTED:
    app.config.update(
        SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax"
    )
    # Cloud Run terminates HTTPS and adds one X-Forwarded-For / -Proto hop.
    # Trust exactly that one: more would let a client spoof its IP past the rate limits.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.before_request(drop_idle_states)


@app.get("/privacy")
def privacy():
    return render_template(
        "privacy.html", last_updated=LEGAL_LAST_UPDATED, contact_email=CONTACT_EMAIL, hosted=HOSTED
    )


@app.get("/terms")
def terms():
    return render_template("terms.html", last_updated=LEGAL_LAST_UPDATED)


@app.after_request
def add_security_headers(response):
    response.headers.update(SECURITY_HEADERS)
    return response


@app.errorhandler(429)
def too_many_requests(_error):
    """A rate limit in limits.py was hit (hosted mode only)."""
    return error_response("Too many requests. Wait a minute, then try again.", 429)


def main():
    if not os.environ.get("FLASK_SECRET_KEY"):
        print("Note: FLASK_SECRET_KEY not set; using a random key for this run.", file=sys.stderr)
    if HOSTED:  # never reads the OS secure store
        print(
            f"Hosted mode: YNAB OAuth login only (redirect URI: {REDIRECT_URI}).", file=sys.stderr
        )
    else:
        if token_store.load_token():  # never print the token itself
            print(
                f"Using the saved YNAB token from the {token_store.store_name()}.",
                file=sys.stderr,
            )
        else:
            print("Enter your YNAB personal access token on the page to connect.", file=sys.stderr)
        if oauth_configured():
            print(f"YNAB OAuth also available (redirect URI: {REDIRECT_URI}).", file=sys.stderr)
    print(f"Open http://localhost:{PORT}", file=sys.stderr)
    # Bind to this Mac only; nothing is reachable from the network
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    main()
