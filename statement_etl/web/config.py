"""
ph-statement-import - web app settings that more than one web module needs.

Read once from the environment (.env is loaded here, before anything reads it):
    APP_PORT    - optional, default 5050 (5000 is used by macOS AirPlay)
    PUBLIC_URL  - empty (the default) for local mode. Set it to the address
                  people open, e.g. https://statements.example.com, to turn on
                  hosted mode: HOSTED is then True.
    APP_CONFIG  - hosted mode only, optional: one JSON object of secrets, e.g.
                  {"FLASK_SECRET_KEY": "...", "YNAB_CLIENT_SECRET": "..."}, as
                  Cloud Run passes them from Secret Manager. Each key is copied
                  into the environment, so the rest of the app reads it as usual.

Local mode is the app on your own computer: a personal access token typed on
the page, optionally saved in the OS secure store (token_store.py).

Hosted mode is the app on a server, for other people. Only YNAB OAuth login
connects (YNAB's API terms don't allow asking other people for their personal
access tokens), and the OS secure store is never used: one token saved on the
server would connect every visitor. It needs FLASK_SECRET_KEY,
YNAB_CLIENT_ID and YNAB_CLIENT_SECRET; without one, the app stops at startup.

Auto-import from Gmail (hosted mode only, P5) is on when GOOGLE_CLIENT_ID is
set. It then also needs GOOGLE_CLIENT_SECRET and ENCRYPTION_KEYS (see
autoimport/store.py). Without GOOGLE_CLIENT_ID, neither is needed.

REDIRECT_URI is where YNAB sends the browser back after OAuth login. It must
match the redirect URI of your YNAB OAuth application exactly.
"""

import json
import os

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell

PORT = int(os.environ.get("APP_PORT", "5050"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
HOSTED = bool(PUBLIC_URL)  # hosted mode: OAuth only, no keychain, strict cookies

# Without these, hosted mode can't sign cookies or log anyone in to YNAB
HOSTED_REQUIRED = ("FLASK_SECRET_KEY", "YNAB_CLIENT_ID", "YNAB_CLIENT_SECRET")
# Without these, auto-import can't log in to Google or encrypt what it stores
AUTO_IMPORT_REQUIRED = ("GOOGLE_CLIENT_SECRET", "ENCRYPTION_KEYS")


def require(keys, needed_by):
    """Stop the app (SystemExit) naming each key in `keys` that isn't set."""
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"{needed_by} needs {', '.join(missing)} in APP_CONFIG or the environment."
        )


def load_hosted_config():
    """Copy APP_CONFIG's keys into the environment, then check nothing is missing.

    Stops the app (SystemExit) on a problem. The message names keys only,
    never their values, because it ends up in the server's logs.
    """
    if os.environ.get("APP_CONFIG"):
        try:
            secrets = json.loads(os.environ["APP_CONFIG"])
        except ValueError:
            raise SystemExit("APP_CONFIG is not valid JSON.") from None
        if not isinstance(secrets, dict):
            raise SystemExit('APP_CONFIG must be a JSON object, like {"FLASK_SECRET_KEY": "..."}.')
        for key, value in secrets.items():
            if not isinstance(value, str):
                raise SystemExit(f"APP_CONFIG: {key} must be a string.")
            os.environ[key] = value

    require(HOSTED_REQUIRED, "Hosted mode (PUBLIC_URL is set)")
    if os.environ.get("GOOGLE_CLIENT_ID"):
        require(AUTO_IMPORT_REQUIRED, "Auto-import (GOOGLE_CLIENT_ID is set)")


if HOSTED:
    load_hosted_config()
    REDIRECT_URI = f"{PUBLIC_URL}/oauth/callback"
else:
    REDIRECT_URI = f"http://localhost:{PORT}/oauth/callback"
