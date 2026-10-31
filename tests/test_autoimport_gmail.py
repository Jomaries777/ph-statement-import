"""
Tests for autoimport/gmail.py: Google OAuth URLs, token exchange / refresh /
revocation, paginated Gmail search (`after:<epoch>`), and in-memory PDF
attachment selection.

Run from the project folder:  python -m unittest discover tests

All HTTP calls go through a fake `urlopen` layer; any unexpected request fails
the test. All values here are made up.
"""

import base64
import http.client
import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta, timezone
from email.message import Message
from unittest import mock

import tests  # noqa: F401 - loads the safety net (no YNAB, no keychain)
from statement_etl.autoimport import gmail
from statement_etl.autoimport.gmail import GmailError

CLIENT_ID = "test-google-client-id"
CLIENT_SECRET = "test-google-client-secret-do-not-leak"
PUBLIC_URL = "https://statements.example.test"
ACCESS_TOKEN = "test-google-access-token-do-not-leak"
REFRESH_TOKEN = "test-google-refresh-token-do-not-leak"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fake_id_token(claims: dict) -> str:
    header = b64url(b'{"alg":"RS256","typ":"JWT"}')
    return f"{header}.{b64url(json.dumps(claims).encode())}.{b64url(b'sig')}"


def mime_part(
    filename: str = "",
    *,
    mime: str = "application/pdf",
    att_id: str | None = None,
    data: str | None = None,
    parts: list[dict] | None = None,
) -> dict:
    body: dict[str, object] = {"size": 100}
    if att_id is not None:
        body["attachmentId"] = att_id
    if data is not None:
        body["data"] = data
    node: dict[str, object] = {"mimeType": mime, "filename": filename, "body": body}
    if parts is not None:
        node["parts"] = parts
    return node


def http_error(
    status: int, body: dict | bytes = b"", *, reason: str = "Error"
) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
    return urllib.error.HTTPError(
        "https://googleapis.example.test", status, reason, Message(), io.BytesIO(raw)
    )


class FakeHTTP:
    """Queue-backed fake for `urllib.request.urlopen` that records every request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._queue: list[object] = []

    def enqueue_json(self, payload: object) -> None:
        self._queue.append(json.dumps(payload).encode("utf-8"))

    def enqueue_bytes(self, raw: bytes) -> None:
        self._queue.append(raw)

    def enqueue_error(self, error: BaseException) -> None:
        self._queue.append(error)

    def __call__(self, req: urllib.request.Request, timeout: int = 30) -> io.BytesIO:
        parsed = urllib.parse.urlsplit(req.full_url)
        raw_body = req.data.decode("utf-8") if req.data else ""
        self.calls.append(
            {
                "method": req.get_method(),
                "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                "query": urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                "form": urllib.parse.parse_qs(raw_body, keep_blank_values=True) if raw_body else {},
                "headers": {k.lower(): v for k, v in req.header_items()},
                "timeout": timeout,
            }
        )
        if not self._queue:
            raise AssertionError(f"Unexpected HTTP call: {req.get_method()} {req.full_url}")
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, bytes)
        return io.BytesIO(item)


class GmailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        env = {
            "GOOGLE_CLIENT_ID": CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
            "PUBLIC_URL": PUBLIC_URL,
        }
        env_patcher = mock.patch.dict(os.environ, env, clear=True)
        self.addCleanup(env_patcher.stop)
        env_patcher.start()

        self.http = FakeHTTP()
        url_patcher = mock.patch.object(gmail.urllib.request, "urlopen", side_effect=self.http)
        self.addCleanup(url_patcher.stop)
        url_patcher.start()


# =============================================================================
# OAUTH - authorize URL, code exchange, id_token decoding, refresh, revoke
# =============================================================================


class OAuthTest(GmailTestCase):
    def test_oauth_configured_and_redirect_uri(self) -> None:
        self.assertTrue(gmail.oauth_configured())
        self.assertEqual(gmail.redirect_uri(), "https://statements.example.test/google/callback")
        os.environ.pop("PUBLIC_URL")
        os.environ["APP_PORT"] = "5151"
        self.assertEqual(gmail.redirect_uri(), "http://localhost:5151/google/callback")
        os.environ.pop("GOOGLE_CLIENT_SECRET")
        self.assertFalse(gmail.oauth_configured())

    def test_authorize_url_requests_offline_consent_and_scopes(self) -> None:
        parsed = urllib.parse.urlsplit(gmail.authorize_url("state-abc-123"))
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}", gmail.GOOGLE_AUTHORIZE_URL
        )
        self.assertEqual(
            urllib.parse.parse_qs(parsed.query),
            {
                "client_id": [CLIENT_ID],
                "redirect_uri": ["https://statements.example.test/google/callback"],
                "response_type": ["code"],
                "scope": ["openid email profile https://www.googleapis.com/auth/gmail.readonly"],
                "access_type": ["offline"],
                "prompt": ["consent"],
                "state": ["state-abc-123"],
            },
        )
        self.assertEqual(self.http.calls, [])

    def test_exchange_code_and_decode_id_token(self) -> None:
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "109876543210",
            "email": "alex@example.test",
            "family_name": "Dela Cruz",
        }
        payload = {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "id_token": fake_id_token(claims),
        }
        self.http.enqueue_json(payload)

        tokens = gmail.exchange_code("auth-code-xyz")
        self.assertEqual(tokens, payload)
        self.assertEqual(gmail.decode_id_token(tokens["id_token"]), claims)
        alt_iss = {**claims, "iss": "accounts.google.com"}
        self.assertEqual(gmail.decode_id_token(fake_id_token(alt_iss)), alt_iss)
        [call] = self.http.calls
        self.assertEqual((call["method"], call["url"]), ("POST", gmail.GOOGLE_TOKEN_URL))
        self.assertEqual(
            call["form"],
            {
                "client_id": [CLIENT_ID],
                "client_secret": [CLIENT_SECRET],
                "redirect_uri": ["https://statements.example.test/google/callback"],
                "grant_type": ["authorization_code"],
                "code": ["auth-code-xyz"],
            },
        )

    def test_decode_id_token_rejects_malformed_or_wrong_aud_or_iss(self) -> None:
        for bad in ("not-a-jwt", "a..c", "a.!!!.c", f"a.{b64url(b'[]')}.c"):
            with self.subTest(bad=bad), self.assertRaises(GmailError):
                gmail.decode_id_token(bad)

        valid = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "123"}
        bad_claims = [
            ("wrong aud", {**valid, "aud": "other-client-id"}, "audience"),
            ("missing aud", {"iss": "https://accounts.google.com", "sub": "123"}, "audience"),
            ("wrong iss", {**valid, "iss": "https://evil.example.test"}, "issuer"),
            ("missing iss", {"aud": CLIENT_ID, "sub": "123"}, "issuer"),
        ]
        for label, claims, expected in bad_claims:
            with self.subTest(label), self.assertRaisesRegex(GmailError, expected):
                gmail.decode_id_token(fake_id_token(claims))

    def test_refresh_access_token_success(self) -> None:
        self.http.enqueue_json({"access_token": "fresh-access-token", "expires_in": 3599})
        self.assertEqual(gmail.refresh_access_token(REFRESH_TOKEN), "fresh-access-token")
        self.assertEqual(
            self.http.calls[0]["form"],
            {
                "client_id": [CLIENT_ID],
                "client_secret": [CLIENT_SECRET],
                "grant_type": ["refresh_token"],
                "refresh_token": [REFRESH_TOKEN],
            },
        )

    def test_token_refresh_errors_are_surfaced_without_leaking_secrets(self) -> None:
        cases = [
            (
                http_error(400, {"error": "invalid_grant", "error_description": "Token revoked."}),
                400,
                "invalid_grant (Token revoked.)",
            ),
            (http_error(503, b"Unavailable", reason="Service Unavailable"), 503, "HTTP 503"),
            (urllib.error.URLError("DNS failure"), None, "Could not reach Google API: DNS failure"),
            (
                TimeoutError("read timed out"),
                None,
                "Could not reach Google API: read timed out",
            ),
            (
                http.client.RemoteDisconnected("Remote end closed connection"),
                None,
                "Could not reach Google API: Remote end closed connection",
            ),
        ]
        for failure, expected_status, expected_text in cases:
            with self.subTest(expected_text):
                self.http.enqueue_error(failure)
                with self.assertRaises(GmailError) as ctx:
                    gmail.refresh_access_token(REFRESH_TOKEN)
                self.assertEqual(ctx.exception.status, expected_status)
                self.assertIn(expected_text, str(ctx.exception))
                self.assertNotIn(REFRESH_TOKEN, str(ctx.exception))
                self.assertNotIn(CLIENT_SECRET, str(ctx.exception))

        self.http.enqueue_json({"expires_in": 3599})
        with self.assertRaisesRegex(GmailError, "access_token"):
            gmail.refresh_access_token(REFRESH_TOKEN)

    def test_revoke_posts_token_in_form_body(self) -> None:
        self.http.enqueue_bytes(b"")  # Google returns an empty 200 body on success
        gmail.revoke(REFRESH_TOKEN)
        [call] = self.http.calls
        self.assertEqual((call["method"], call["url"]), ("POST", gmail.GOOGLE_REVOKE_URL))
        self.assertEqual((call["query"], call["form"]), ({}, {"token": [REFRESH_TOKEN]}))

        self.http.enqueue_error(http_error(400, {"error": "invalid_token"}))
        with self.assertRaises(GmailError) as ctx:
            gmail.revoke(REFRESH_TOKEN)
        self.assertEqual(ctx.exception.status, 400)
        self.assertNotIn(REFRESH_TOKEN, str(ctx.exception))


# =============================================================================
# LIST MESSAGES - after:<epoch> query building and nextPageToken pagination
# =============================================================================


class ListMessagesTest(GmailTestCase):
    QUERY = 'from:noreply-transhistory@gcash.com subject:"Your GCash Transaction History"'

    def test_query_gets_after_epoch_seconds_added(self) -> None:
        manila = timezone(timedelta(hours=8))
        for after_val in (
            1730332800,
            1730332800.9,
            datetime(2024, 10, 31, 0, 0, tzinfo=UTC),
            datetime(2024, 10, 31, 8, 0, tzinfo=manila),
            datetime(2024, 10, 31, 0, 0),
        ):
            with self.subTest(after=after_val):
                self.http.calls.clear()
                self.http.enqueue_json({"messages": [{"id": "m1", "threadId": "t1"}]})
                self.assertEqual(
                    gmail.list_messages(ACCESS_TOKEN, self.QUERY, after=after_val),
                    [{"id": "m1", "threadId": "t1"}],
                )
                [call] = self.http.calls
                self.assertEqual(call["headers"]["authorization"], f"Bearer {ACCESS_TOKEN}")
                self.assertEqual(call["query"], {"q": [f"{self.QUERY} after:1730332800"]})

    def test_query_without_after_and_invalid_after(self) -> None:
        self.http.enqueue_json({"resultSizeEstimate": 0})
        self.assertEqual(gmail.list_messages(ACCESS_TOKEN, self.QUERY, after=None), [])
        self.assertEqual(self.http.calls[0]["query"], {"q": [self.QUERY]})
        with self.assertRaises(TypeError):
            gmail.list_messages(ACCESS_TOKEN, self.QUERY, after=True)

    def test_pagination_follows_next_page_token(self) -> None:
        self.http.enqueue_json(
            {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "page-2"}
        )
        self.http.enqueue_json({"messages": [{"id": "m3"}], "nextPageToken": "page-3"})
        self.http.enqueue_json({"messages": [{"id": "m4"}]})

        messages = gmail.list_messages(ACCESS_TOKEN, self.QUERY, after=1730000000)
        self.assertEqual([m["id"] for m in messages], ["m1", "m2", "m3", "m4"])
        self.assertNotIn("pageToken", self.http.calls[0]["query"])
        self.assertEqual(self.http.calls[1]["query"]["pageToken"], ["page-2"])
        self.assertEqual(self.http.calls[2]["query"]["pageToken"], ["page-3"])


# =============================================================================
# ATTACHMENTS - filename regex matching and in-memory base64url download
# =============================================================================


class GetPdfAttachmentTest(GmailTestCase):
    PATTERN = r"^GoTymeStatementForEverydayDepositAccount_\d{8}_\d{6}\.pdf$"

    def test_picks_first_matching_filename_without_downloading_other_parts(self) -> None:
        pdf_bytes = b"%PDF-1.4\xfb\xff\xfe fake statement bytes"  # uses '-' and '_' in base64url
        everyday_1 = "GoTymeStatementForEverydayDepositAccount_20261031_100000.pdf"
        everyday_2 = "GoTymeStatementForEverydayDepositAccount_20261031_100001.pdf"
        savings = "GoTyme_BankStatement_SavingsAccounts_20261031_100000.pdf"
        self.http.enqueue_json(
            {
                "id": "msg-42",
                "payload": mime_part(
                    mime="multipart/mixed",
                    parts=[
                        mime_part(
                            mime="multipart/alternative",
                            parts=[mime_part(mime="text/plain"), mime_part(mime="text/html")],
                        ),
                        mime_part("logo.png", mime="image/png", att_id="att-logo"),
                        mime_part(savings, att_id="att-savings"),
                        mime_part(everyday_1, att_id="att-everyday-1"),
                        mime_part(everyday_2, att_id="att-everyday-2"),
                    ],
                ),
            }
        )
        self.http.enqueue_json({"size": len(pdf_bytes), "data": b64url(pdf_bytes)})

        self.assertEqual(
            gmail.get_pdf_attachment(ACCESS_TOKEN, "msg-42", self.PATTERN),
            (everyday_1, pdf_bytes),
        )
        self.assertEqual(
            [c["url"] for c in self.http.calls],
            [
                f"{gmail.GMAIL_API_BASE}/messages/msg-42",
                f"{gmail.GMAIL_API_BASE}/messages/msg-42/attachments/att-everyday-1",
            ],
        )

    def test_inline_attachment_body_data_needs_no_second_request(self) -> None:
        pdf_bytes = b"%PDF-1.4 inline pdf"
        self.http.enqueue_json(
            {"id": "m-in", "payload": mime_part("GCash.PDF", data=b64url(pdf_bytes))}
        )
        self.assertEqual(
            gmail.get_pdf_attachment(ACCESS_TOKEN, "m-in", r"\.pdf$"), ("GCash.PDF", pdf_bytes)
        )
        self.assertEqual(len(self.http.calls), 1)

    def test_no_matching_pdf_returns_none(self) -> None:
        messages = [
            {"id": "m-text", "payload": mime_part(mime="text/plain")},
            {
                "id": "m-other",
                "payload": mime_part(
                    mime="multipart/mixed",
                    parts=[
                        mime_part("banner.png", mime="image/png", att_id="att-1"),
                        mime_part("Savings.pdf", att_id="att-2"),
                    ],
                ),
            },
            {"id": "m-empty"},
        ]
        for msg in messages:
            with self.subTest(msg_id=msg["id"]):
                self.http.calls.clear()
                self.http.enqueue_json(msg)
                self.assertIsNone(gmail.get_pdf_attachment(ACCESS_TOKEN, msg["id"], self.PATTERN))
                self.assertEqual(len(self.http.calls), 1)

    def test_gmail_api_errors_and_missing_data_raise_gmail_error(self) -> None:
        self.http.enqueue_error(
            http_error(403, {"error": {"code": 403, "message": "Gmail API disabled."}})
        )
        with self.assertRaises(GmailError) as ctx:
            gmail.get_pdf_attachment(ACCESS_TOKEN, "msg-1", r"\.pdf$")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("Gmail API disabled.", str(ctx.exception))
        self.assertNotIn(ACCESS_TOKEN, str(ctx.exception))

        self.http.enqueue_bytes(b"<html>bad gateway</html>")
        with self.assertRaisesRegex(GmailError, "not valid JSON"):
            gmail.get_message(ACCESS_TOKEN, "msg-1")

        self.http.enqueue_json({"id": "m1", "payload": mime_part("a.pdf", att_id="att/1")})
        self.http.enqueue_json({"size": 10})
        with self.assertRaisesRegex(GmailError, "did not include data"):
            gmail.get_pdf_attachment(ACCESS_TOKEN, "m1", r"\.pdf$")
        self.assertTrue(self.http.calls[-1]["url"].endswith("/attachments/att%2F1"))


class SafetyNetIntegrationTest(unittest.TestCase):
    def test_unmocked_gmail_calls_hit_googleapis_safety_net(self) -> None:
        env = {"GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": CLIENT_SECRET}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, r"oauth2\.googleapis\.com"):
                gmail.refresh_access_token(REFRESH_TOKEN)
            with self.assertRaisesRegex(RuntimeError, r"gmail\.googleapis\.com"):
                gmail.list_messages(ACCESS_TOKEN, "from:test@example.test")


if __name__ == "__main__":
    unittest.main()
