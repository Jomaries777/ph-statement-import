"""
ph-statement-import - Google OAuth and Gmail REST client, with no Flask session.

    oauth_configured()      - are GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET set?
    redirect_uri()          - <PUBLIC_URL>/google/callback (or localhost in dev)
    authorize_url()         - Google consent URL (openid, email, profile, gmail.readonly)
    exchange_code()         - swap ?code=... for access, refresh and ID tokens
    decode_id_token()       - read sub, email and family_name from Google's id_token
    refresh_access_token()  - a fresh access token from a stored refresh token
    list_messages()         - search messages (adding after:<epoch>), following nextPageToken
    get_message()           - fetch one Gmail message's metadata and MIME payload
    get_pdf_attachment()    - first attachment matching a filename regex, in memory only
    revoke()                - POST https://oauth2.googleapis.com/revoke

Uses plain HTTPS via `urllib`, like `statement_etl/web/ynab_oauth.py`, so there
is no Google API client dependency and both web routes and background polling
jobs can call these functions.

Environment (.env / APP_CONFIG):
    GOOGLE_CLIENT_ID      - from the Google Cloud OAuth 2.0 Web client
    GOOGLE_CLIENT_SECRET  - as GOOGLE_CLIENT_ID
    PUBLIC_URL            - hosted base URL (falls back to http://localhost:<APP_PORT>)

Tests:
    python -m unittest tests.test_autoimport_gmail
"""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
)
TIMEOUT_SECONDS = 30


class GmailError(RuntimeError):
    """A failed Google OAuth or Gmail API call, safe to log (contains no tokens)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# =============================================================================
# HTTP HELPERS - plain urllib calls with safe error messages
# =============================================================================


def _b64url_decode(data: str) -> bytes:
    """Decode padded or unpadded URL-safe base64 (`-` and `_`), as Google APIs send."""
    cleaned = data.strip()
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except ValueError:
        raise GmailError("Invalid base64url data from Google API.") from None


def _error_detail(raw_body: bytes, fallback: str) -> str:
    """Extract Google's error message from an OAuth or Gmail JSON error body."""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, str) and error:
        desc = payload.get("error_description")
        return f"{error} ({desc})" if isinstance(desc, str) and desc else error
    if isinstance(error, dict):
        msg = error.get("message") or error.get("status")
        if isinstance(msg, str) and msg:
            return msg
    return fallback


def _request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            raw_body = e.read()
        finally:
            e.close()
        detail = _error_detail(raw_body, e.reason or f"HTTP {e.code}")
        raise GmailError(f"Google API returned HTTP {e.code}: {detail}", status=e.code) from None
    except urllib.error.URLError as e:
        raise GmailError(f"Could not reach Google API: {e.reason}") from e


def _parse_json(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GmailError("Google API response was not valid JSON.") from None
    if not isinstance(payload, dict):
        raise GmailError("Google API response must be a JSON object.")
    return payload


def _post_form(url: str, params: dict[str, str]) -> bytes:
    return _request(
        url,
        method="POST",
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _gmail_get(access_token: str, path: str, params: dict[str, str] | None = None) -> dict:
    url = f"{GMAIL_API_BASE}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return _parse_json(_request(url, headers={"Authorization": f"Bearer {access_token}"}))


# =============================================================================
# OAUTH - Google sign-in, token exchange, refresh and revocation
# =============================================================================


def oauth_configured() -> bool:
    """True when `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set."""
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def redirect_uri() -> str:
    """Callback URL registered on the Google OAuth client (`<PUBLIC_URL>/google/callback`)."""
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if public_url:
        return f"{public_url}/google/callback"
    return f"http://localhost:{int(os.environ.get('APP_PORT', '5050'))}/google/callback"


def authorize_url(state: str) -> str:
    """Google OAuth consent URL (`openid email profile gmail.readonly`, offline consent).

    authorize_url("state-1") -> "https://accounts.google.com/o/oauth2/v2/auth?..."
    """
    query = urllib.parse.urlencode(
        {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTHORIZE_URL}?{query}"


def exchange_code(code: str) -> dict:
    """Swap an authorization code for Google OAuth tokens (including `id_token`).

    exchange_code("code-1") -> {"access_token": "...", "refresh_token": "...", "id_token": "..."}
    """
    tokens = _parse_json(
        _post_form(
            GOOGLE_TOKEN_URL,
            {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
                "code": code,
            },
        )
    )
    if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
        raise GmailError("Google token exchange did not include an access_token.")
    return tokens


def decode_id_token(id_token: str) -> dict:
    """Decode `sub`, `email` and `family_name` from an `id_token` returned by `exchange_code()`.

    Only for ID tokens just fetched over HTTPS from `GOOGLE_TOKEN_URL`; verify
    caller-supplied OIDC tokens with `google.oauth2.id_token.verify_oauth2_token`.

    decode_id_token("eyJ...payload...sig") -> {"sub": "123", "email": "a@example.test", ...}
    """
    parts = id_token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise GmailError("Invalid Google id_token format.")
    return _parse_json(_b64url_decode(parts[1]))


def refresh_access_token(refresh_token: str) -> str:
    """Exchange a stored Google `refresh_token` for a fresh `access_token`.

    refresh_access_token("test-refresh-token") -> "test-access-token"
    """
    tokens = _parse_json(
        _post_form(
            GOOGLE_TOKEN_URL,
            {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    )
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GmailError("Google token refresh did not include an access_token.")
    return access_token


def revoke(token: str) -> None:
    """Revoke a Google access or refresh token via `POST https://oauth2.googleapis.com/revoke`.

    revoke("test-refresh-token") -> None
    """
    _post_form(GOOGLE_REVOKE_URL, {"token": token})


# =============================================================================
# GMAIL API - search statement messages and download PDF attachments in memory
# =============================================================================


def _query_with_after(query: str, after: datetime | int | float | None) -> str:
    base = query.strip()
    if after is None:
        return base
    if isinstance(after, bool) or not isinstance(after, datetime | int | float):
        raise TypeError("after must be a datetime, epoch seconds, or None.")
    if isinstance(after, datetime):
        epoch = int((after if after.tzinfo else after.replace(tzinfo=UTC)).timestamp())
    else:
        epoch = int(after)
    return f"{base} after:{epoch}" if base else f"after:{epoch}"


def list_messages(
    access_token: str, query: str, after: datetime | int | float | None = None
) -> list[dict]:
    """List matching Gmail message stubs (`{"id", "threadId"}`), following `nextPageToken`.

    list_messages("token", "from:noreply@example.test", after=1730000000)
    -> [{"id": "msg-1", "threadId": "thread-1"}]
    """
    full_query = _query_with_after(query, after)
    messages: list[dict] = []
    page_token: str | None = None
    while True:
        params = {"q": full_query, **({"pageToken": page_token} if page_token else {})}
        data = _gmail_get(access_token, "messages", params)
        messages.extend(
            m for m in (data.get("messages") or ()) if isinstance(m, dict) and m.get("id")
        )
        next_token = data.get("nextPageToken")
        if not isinstance(next_token, str) or not next_token:
            return messages
        page_token = next_token


def get_message(access_token: str, msg_id: str) -> dict:
    """Fetch one Gmail message resource (`id`, `internalDate`, `payload`, ...).

    get_message("token", "msg-1") -> {"id": "msg-1", "payload": {...}}
    """
    return _gmail_get(access_token, f"messages/{urllib.parse.quote(msg_id, safe='')}")


def _iter_parts(part: dict) -> Iterator[dict]:
    yield part
    for child in part.get("parts") or ():
        if isinstance(child, dict):
            yield from _iter_parts(child)


def get_pdf_attachment(
    access_token: str, msg_id: str, pattern: str | re.Pattern[str]
) -> tuple[str, bytes] | None:
    """Download the first attachment on `msg_id` whose filename matches `pattern`, in memory.

    get_pdf_attachment("token", "msg-1", r"\\.pdf$") -> ("statement.pdf", b"%PDF-1.4...")
    """
    regex = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, re.IGNORECASE)
    payload = get_message(access_token, msg_id).get("payload")
    if not isinstance(payload, dict):
        return None

    quoted_msg = urllib.parse.quote(msg_id, safe="")
    for part in _iter_parts(payload):
        filename = part.get("filename") or ""
        body = part.get("body")
        if not filename or not regex.search(filename) or not isinstance(body, dict):
            continue
        if isinstance(body.get("data"), str) and body["data"]:
            return filename, _b64url_decode(body["data"])
        att_id = body.get("attachmentId")
        if isinstance(att_id, str) and att_id:
            quoted_att = urllib.parse.quote(att_id, safe="")
            attachment = _gmail_get(access_token, f"messages/{quoted_msg}/attachments/{quoted_att}")
            raw_data = attachment.get("data")
            if not isinstance(raw_data, str) or not raw_data:
                raise GmailError("Gmail attachment response did not include data.")
            return filename, _b64url_decode(raw_data)
    return None
