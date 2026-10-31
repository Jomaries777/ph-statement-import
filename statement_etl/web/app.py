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

This file only creates the Flask app and starts it. The routes are in two
blueprints, one per job:
    upload.py      - the page, parsing, plans / accounts, balance checks, upload
    ynab_auth.py   - connecting to YNAB: personal access token or OAuth login
and their helpers:
    state.py       - STORE, this browser's server-side state
    ynab_oauth.py  - YNAB OAuth token calls (no Flask session)
    config.py      - PORT and the OAuth REDIRECT_URI
    token_store.py - the token saved in the OS secure store
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

import os
import secrets
import sys

from flask import Flask

from statement_etl.web import token_store, upload, ynab_auth
from statement_etl.web.config import PORT, REDIRECT_URI
from statement_etl.web.ynab_oauth import oauth_configured

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.register_blueprint(upload.blueprint)
app.register_blueprint(ynab_auth.blueprint)


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
    if oauth_configured():
        print(f"YNAB OAuth also available (redirect URI: {REDIRECT_URI}).", file=sys.stderr)
    print(f"Open http://localhost:{PORT}", file=sys.stderr)
    # Bind to this Mac only; nothing is reachable from the network
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    main()
