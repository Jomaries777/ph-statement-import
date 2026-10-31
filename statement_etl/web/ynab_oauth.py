"""
ph-statement-import - YNAB OAuth token calls, with no Flask session.

    oauth_configured() - are YNAB_CLIENT_ID and YNAB_CLIENT_SECRET set?
    exchange_token()   - POST to YNAB's token endpoint (login code or refresh)
    refresh()          - a new access token from a refresh token

The YNAB SDK has no OAuth support, so these are plain HTTP calls. They take
and return plain values, so the web app (ynab_auth.py) and a background job
with no browser can both use them.

Environment (.env):
    YNAB_CLIENT_ID      - from your YNAB OAuth application
    YNAB_CLIENT_SECRET  - as YNAB_CLIENT_ID
"""

import json
import os
import urllib.parse
import urllib.request

from statement_etl.web.config import REDIRECT_URI

YNAB_TOKEN_URL = "https://app.ynab.com/oauth/token"


def oauth_configured():
    return bool(os.environ.get("YNAB_CLIENT_ID") and os.environ.get("YNAB_CLIENT_SECRET"))


def exchange_token(**params):
    """POST to YNAB's token endpoint (authorization_code or refresh_token grant).

    exchange_token(grant_type="authorization_code", code="abc")
    -> {"access_token": ..., "refresh_token": ..., "expires_in": 7200, ...}
    """
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


def refresh(refresh_token):
    """A new token from YNAB for this refresh token, as exchange_token() returns it.

    YNAB rotates refresh tokens, so save the "refresh_token" in the result.
    """
    return exchange_token(grant_type="refresh_token", refresh_token=refresh_token)
