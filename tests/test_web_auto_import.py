"""
Tests for web/auto_import.py and its page (/auto-import): conditional blueprint
registration, Google sign-in and invite list, single-use YNAB refresh token
rotation, password testing, Turn off and delete, Cloud Scheduler OIDC checks,
rate limits, privacy disclosures, and headless Chromium page flow.

Run from the project folder:  python -m unittest discover tests

Firestore is replaced with FakeStore (tests/fakes.py); Google OAuth / Gmail,
autoimport.runner, google.oauth2.id_token and YNAB OAuth are mocked. All data
here is made up.
"""

import base64
import dataclasses
import json
import os
import re
import time
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from unittest import mock

import google.auth.exceptions

from statement_etl import sources
from statement_etl.autoimport import gmail, runner
from statement_etl.web import app as web
from statement_etl.web import auto_import, config, token_store, ynab_oauth
from statement_etl.web import state as web_state
from tests.e2e import fake_ynab
from tests.e2e.fake_server import FakeServer
from tests.fakes import ACCOUNT_ID, ENCRYPTION_KEY, PLAN_ID, FakeStore
from tests.hosted import HOSTED_ENV, PUBLIC_URL, SECRETS, hosted_mode

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

REQUIRE_E2E = os.environ.get("REQUIRE_E2E") == "1"

PROJECT_ID = "test-project"
SCHEDULER_EMAIL = f"scheduler-invoker@{PROJECT_ID}.iam.gserviceaccount.com"
PUBSUB_EMAIL = f"pubsub-push@{PROJECT_ID}.iam.gserviceaccount.com"
GOOGLE_CLIENT_ID = "test-google-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "test-google-client-secret"

SUB = "100000000000000000001"
ALLOWED_EMAIL = "juan@example.test"
FAMILY_NAME = "Dela Cruz"
UNINVITED_EMAIL = "stranger@example.test"
GMAIL_REFRESH_TOKEN = "1//made-up-gmail-refresh-token"
OLD_YNAB_REFRESH = "made-up-old-ynab-refresh"
NEW_YNAB_REFRESH = "made-up-new-ynab-refresh"
NEW_YNAB_ACCESS = "made-up-new-ynab-access"
OTHER_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"

AUTO_IMPORT_SECRETS = {
    **SECRETS,
    "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
    "ENCRYPTION_KEYS": ENCRYPTION_KEY,
    # Leading/trailing spaces and mixed case, read from APP_CONFIG (never deploy.yml)
    "ALLOWED_GOOGLE_EMAILS": "  Juan@Example.test , maria@example.test  ",
}
AUTO_IMPORT_ENV = {
    **HOSTED_ENV,
    "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
    "GCP_PROJECT_ID": PROJECT_ID,
    "APP_CONFIG": json.dumps(AUTO_IMPORT_SECRETS),
}

AUTO_IMPORT_ROUTES = (
    ("GET", "/auto-import"),
    ("GET", "/google/login"),
    ("GET", "/google/callback"),
    ("POST", "/api/auto-import"),
    ("POST", "/api/auto-import/test"),
    ("POST", "/api/auto-import/delete"),
    ("POST", "/tasks/poll-gmail"),
)

SECOND_SOURCE = dataclasses.replace(
    sources.SOURCES["gcash"],
    key="samplebank",
    name="SampleBank",
    email_query="from:statements@samplebank.example.test has:attachment filename:pdf",
    attachment_pattern=r"\.pdf$",
    password_inputs=("birth_date",),
    build_password=lambda inputs: inputs["birth_date"].replace("-", ""),
)

# A source without email_query: it works for manual upload but isn't on /auto-import
NO_EMAIL_SOURCE = dataclasses.replace(
    sources.SOURCES["gcash"], key="nomail", name="NoMailBank", email_query=None
)


def valid_settings_payload(
    *,
    account_id=ACCOUNT_ID,
    account_name="GCash Wallet",
    last_name="Dela Cruz",
    mobile_last4="1234",
):
    return {
        "plan_id": PLAN_ID,
        "plan_name": "Test Plan",
        "mappings": {
            "gcash": {
                "account_id": account_id,
                "account_name": account_name,
                "enabled": True,
            }
        },
        "password_inputs": {
            "gcash": {
                "last_name": last_name,
                "mobile_last4": mobile_last4,
            }
        },
    }


def gcash_and_gotyme_payload(*, gotyme_account_id=OTHER_ACCOUNT_ID):
    """Settings with GCash on ACCOUNT_ID and GoTyme on `gotyme_account_id`."""
    payload = valid_settings_payload()
    payload["mappings"]["gotyme"] = {
        "account_id": gotyme_account_id,
        "account_name": "GoTyme Everyday",
        "enabled": True,
    }
    payload["password_inputs"]["gotyme"] = {"birth_date": "1990-05-15"}
    return payload


# =============================================================================
# DISABLED MODES - live app starts unchanged when Google settings are absent
# =============================================================================


class DisabledRoutesTest(unittest.TestCase):
    """None of the auto-import routes exist unless HOSTED and GOOGLE_CLIENT_ID are both set."""

    def assert_auto_import_off(self, client):
        index = client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertNotIn(b"/auto-import", index.data)
        for method, path in AUTO_IMPORT_ROUTES:
            with self.subTest(method=method, path=path):
                response = client.open(path, method=method)
                self.assertEqual(response.status_code, 404)

    def test_off_in_local_mode(self):
        self.assertFalse(config.HOSTED)
        self.assert_auto_import_off(web.app.test_client())

    def test_off_in_local_mode_even_with_google_client_id(self):
        local_with_google = {
            "PUBLIC_URL": "",
            "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
            "ENCRYPTION_KEYS": ENCRYPTION_KEY,
        }
        with hosted_mode(local_with_google):
            self.assertFalse(config.HOSTED)
            self.assertFalse(web.AUTO_IMPORT)
            self.assert_auto_import_off(web.app.test_client())

    def test_off_in_hosted_mode_without_google_client_id(self):
        # Every merge to main deploys; GOOGLE_CLIENT_ID may still be empty then
        env = {**HOSTED_ENV, "GOOGLE_CLIENT_ID": ""}
        with hosted_mode(env):
            self.assertTrue(config.HOSTED)
            self.assertFalse(web.AUTO_IMPORT)
            self.assert_auto_import_off(web.app.test_client())

    def test_hosted_with_google_client_id_requires_secret_and_encryption_keys(self):
        bad_env = {
            **HOSTED_ENV,
            "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": "",
            "ENCRYPTION_KEYS": "",
        }
        with self.assertRaises(SystemExit) as caught, hosted_mode(bad_env):
            pass
        message = str(caught.exception)
        self.assertIn("GOOGLE_CLIENT_SECRET", message)
        self.assertIn("ENCRYPTION_KEYS", message)
        self.assertNotIn(GOOGLE_CLIENT_ID, message)


# =============================================================================
# BASE TEST CASE - hosted mode with GOOGLE_CLIENT_ID and FakeStore
# =============================================================================


class AutoImportWebTest(unittest.TestCase):
    """Hosted mode with auto-import turned on and FakeStore in place of Firestore."""

    def setUp(self):
        self.patch_dict(web_state.STORE, {}, clear=True)
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("YNAB_CLIENT_", "GOOGLE_", "ALLOWED_GOOGLE_", "GCP_"))
        }
        self.patch_dict(os.environ, clean_env, clear=True)
        self.enterContext(hosted_mode(AUTO_IMPORT_ENV))

        for name in ("available", "store_name", "load_token", "save_token", "delete_token"):
            self.patch(
                token_store,
                name,
                side_effect=AssertionError(f"hosted mode called token_store.{name}()"),
            )

        self.store = FakeStore()
        self.patch(auto_import, "store", new=self.store)
        self.client = web.app.test_client()

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_dict(self, target, values, **kwargs):
        patcher = mock.patch.dict(target, values, **kwargs)
        self.addCleanup(patcher.stop)
        patcher.start()

    def set_ynab_state(self, **fields):
        fields.setdefault("last_seen", time.time())
        with self.client.session_transaction() as sess:
            sess["sid"] = "test-sid"
        web_state.STORE["test-sid"] = fields
        return fields

    def ynab_state(self):
        with self.client.session_transaction() as sess:
            return web_state.STORE.get(sess.get("sid"), {})

    def connect_ynab_oauth(self, refresh_token=OLD_YNAB_REFRESH):
        return self.set_ynab_state(
            access_token="old-ynab-access",
            refresh_token=refresh_token,
            token_kind="oauth",
            expires_at=time.time() + 3600,
        )

    def sign_in_google(
        self,
        *,
        sub=SUB,
        email=ALLOWED_EMAIL,
        family_name=FAMILY_NAME,
        enabled=False,
        **extra_user_fields,
    ):
        self.store.save_user(
            sub,
            email=email,
            family_name=family_name,
            gmail_refresh_token=GMAIL_REFRESH_TOKEN,
            enabled=enabled,
            **extra_user_fields,
        )
        with self.client.session_transaction() as sess:
            sess["google_sub"] = sub


# =============================================================================
# PAGE RENDERING, GENERIC SOURCE ROWS & STRICT CSP
# =============================================================================


class PageViewTest(AutoImportWebTest):
    def test_main_page_links_to_auto_import_in_hosted_mode(self):
        page = self.client.get("/").data
        self.assertIn(b'id="auto-import-link"', page)
        self.assertIn(b'href="/auto-import"', page)

    def test_no_inline_scripts_or_styles_and_security_headers_present(self):
        self.sign_in_google(
            enabled=True,
            plan_id=PLAN_ID,
            plan_name="Test Plan",
            mappings={
                "gcash": {"account_id": ACCOUNT_ID, "account_name": "GCash", "enabled": True}
            },
        )
        self.store.write_run(
            SUB,
            "msg-1",
            "gcash",
            account_id=ACCOUNT_ID,
            status=runner.OK,
            created=4,
            skipped=1,
            message="",
            received_at=datetime(2026, 10, 30, 1, 2, tzinfo=UTC),
        )
        for path, expected_status in (
            ("/auto-import", 200),
            ("/auto-import?not_invited=1", 403),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, expected_status)
                for header, value in web.SECURITY_HEADERS.items():
                    self.assertEqual(response.headers[header], value)
                self.assertIn(b'<script src="/static/auto_import.js">', response.data)
                self.assertIn(b'<link rel="stylesheet" href="/static/app.css">', response.data)
                self.assertNotIn(b"<script>", response.data)
                self.assertNotIn(b"<style", response.data)
                self.assertNotIn(b"style=", response.data)
                self.assertIn(b'href="/privacy"', response.data)

        with self.client.get("/static/auto_import.js") as js_resp:
            self.assertEqual(js_resp.status_code, 200)

    def test_lists_only_sources_with_email_query_and_renders_generic_inputs(self):
        # A Source without email_query must not appear; any Source with one,
        # like the patched-in SampleBank, renders with no source special cases.
        self.patch_dict(sources.SOURCES, {"samplebank": SECOND_SOURCE, "nomail": NO_EMAIL_SOURCE})
        self.sign_in_google()
        html = self.client.get("/auto-import").data.decode()

        self.assertIn('data-source="gcash"', html)
        self.assertIn('data-source="gotyme"', html)
        self.assertIn('data-source="samplebank"', html)
        self.assertNotIn('data-source="nomail"', html)
        self.assertNotIn("NoMailBank", html)

        # Generic inputs from each Source.password_inputs, with family_name prefilled
        self.assertIn('data-input="last_name"', html)
        self.assertIn('value="Dela Cruz"', html)
        self.assertIn("from Google · edit", html)
        self.assertIn('data-input="mobile_last4"', html)
        self.assertIn('data-input="birth_date"', html)

    def test_gotyme_row_asks_for_the_date_of_birth_in_a_date_field(self):
        self.sign_in_google()
        html = self.client.get("/auto-import").data.decode()
        row = html.split('<tr data-source="gotyme"', 1)[1].split("</tr>", 1)[0]

        self.assertRegex(row, r'type="date"\s+data-input="birth_date"\s+aria-label="Date of birth"')
        self.assertIn('autocomplete="bday"', row)
        self.assertIn("Asked once. GoTyme PDF password: date of birth as MMDDYYYY.", row)
        self.assertNotIn('data-input="last_name"', row)  # GCash's inputs stay on GCash's row

    def test_active_view_shows_newest_20_runs_and_status_badges(self):
        self.sign_in_google(
            enabled=True,
            plan_id=PLAN_ID,
            plan_name="My Budget",
            last_checked=datetime(2026, 10, 31, 1, 15, tzinfo=UTC),
            mappings={
                "gcash": {
                    "account_id": ACCOUNT_ID,
                    "account_name": "GCash Wallet",
                    "enabled": True,
                }
            },
        )
        # Write 22 runs; only the newest 20 appear on the page
        for i in range(22):
            status = runner.OK if i != 21 else runner.BALANCE_MISMATCH
            self.store.write_run(
                SUB,
                f"msg-{i:02d}",
                "gcash",
                account_id=ACCOUNT_ID,
                status=status,
                created=i,
                skipped=1,
                message="Balance note" if status == runner.BALANCE_MISMATCH else "",
                received_at=datetime(2026, 10, 30, 1, 2, tzinfo=UTC),
            )
            # Distinct ran_at timestamps so ordering is deterministic
            self.store.runs[SUB][f"msg-{i:02d}:gcash"]["ran_at"] = datetime(
                2026, 10, 30, 1, i, tzinfo=UTC
            )

        html = self.client.get("/auto-import").data.decode()
        self.assertIn('id="auto-on-view">', html)
        self.assertIn('id="auto-setup-view" class="hidden">', html)
        self.assertIn("Oct 31, 09:15 AM PHT", html)
        self.assertIn("My Budget ✓", html)
        self.assertIn("GCash → GCash Wallet", html)
        self.assertIn("⚠ Balance", html)
        self.assertIn("21 created · 1 skipped · Balance note", html)
        self.assertIn("✓ Imported", html)
        self.assertIn(">20 created · 1 skipped<", html)
        self.assertIn(">2 created · 1 skipped<", html)
        self.assertNotIn(">1 created · 1 skipped<", html)  # oldest two dropped at limit=20
        self.assertNotIn(">0 created · 1 skipped<", html)

    def test_ynab_oauth_next_returns_to_auto_import_and_blocks_open_redirects(self):
        login = self.client.get("/oauth/login?next=/auto-import")
        self.assertEqual(login.status_code, 302)
        with self.client.session_transaction() as sess:
            state = sess["oauth_state"]
            self.assertEqual(sess["oauth_next"], "/auto-import")
        tokens = {"access_token": "tok", "refresh_token": "rt", "expires_in": 7200}
        with mock.patch.object(ynab_oauth, "exchange_token", return_value=tokens):
            cb = self.client.get(f"/oauth/callback?code=c1&state={state}")
        self.assertEqual(cb.headers["Location"], "/auto-import")

        # An external URL in ?next=... falls back to "/"
        self.client.get("/oauth/login?next=https://evil.example.test")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["oauth_next"], "/")


# =============================================================================
# GOOGLE SIGN-IN & INVITE LIST
# =============================================================================


class GoogleSignInTest(AutoImportWebTest):
    def callback(self, query, expected_state="google-state-1"):
        with self.client.session_transaction() as sess:
            sess.pop("google_oauth_state", None)
            if expected_state is not None:
                sess["google_oauth_state"] = expected_state
        return self.client.get("/google/callback?" + urllib.parse.urlencode(query))

    def test_login_redirects_to_google_consent_with_offline_prompt(self):
        response = self.client.get("/google/login")
        self.assertEqual(response.status_code, 302)
        parsed = urllib.parse.urlparse(response.headers["Location"])
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}", gmail.GOOGLE_AUTHORIZE_URL
        )
        query = urllib.parse.parse_qs(parsed.query)
        with self.client.session_transaction() as sess:
            saved_state = sess["google_oauth_state"]
        self.assertEqual(query["client_id"], [GOOGLE_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [f"{PUBLIC_URL}/google/callback"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["state"], [saved_state])
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", query["scope"][0])

    def test_callback_checks_error_and_state(self):
        err_resp = self.callback({"error": "access_denied"})
        self.assertEqual(err_resp.headers["Location"], "/auto-import?google_error=access_denied")

        for query, saved_state in (
            ({"code": "c", "state": "wrong"}, "google-state-1"),
            ({"code": "c"}, "google-state-1"),
            ({"code": "c", "state": "google-state-1"}, None),
        ):
            with (
                self.subTest(query=query, saved_state=saved_state),
                mock.patch.object(gmail, "exchange_code") as exchange,
            ):
                response = self.callback(query, saved_state)
                self.assertEqual(
                    response.headers["Location"], "/auto-import?google_error=state_mismatch"
                )
                exchange.assert_not_called()

    def test_callback_handles_exchange_failure(self):
        with mock.patch.object(gmail, "exchange_code", side_effect=gmail.GmailError("bad code")):
            response = self.callback({"code": "c", "state": "google-state-1"})
        self.assertEqual(
            response.headers["Location"], "/auto-import?google_error=token_exchange_failed"
        )
        self.assertEqual(self.store.users, {})

    def test_callback_rejects_missing_refresh_token_with_clear_message(self):
        tokens = {"access_token": "ga", "id_token": "id-tok"}  # no refresh_token
        claims = {
            "sub": SUB,
            "email": ALLOWED_EMAIL,
            "email_verified": True,
            "family_name": FAMILY_NAME,
        }
        with (
            mock.patch.object(gmail, "exchange_code", return_value=tokens),
            mock.patch.object(gmail, "decode_id_token", return_value=claims),
        ):
            response = self.callback({"code": "c", "state": "google-state-1"})
        self.assertEqual(
            response.headers["Location"], "/auto-import?google_error=missing_refresh_token"
        )
        self.assertEqual(self.store.users, {})
        with self.client.session_transaction() as sess:
            self.assertNotIn("google_sub", sess)

        page = self.client.get(response.headers["Location"]).data.decode()
        self.assertIn("did not return a refresh token", page)
        self.assertIn("Third-party access", page)

    def test_unverified_email_is_rejected_before_the_invite_list(self):
        tokens = {
            "access_token": "ga",
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "id_token": "id-tok",
        }
        base = {"sub": SUB, "email": ALLOWED_EMAIL, "family_name": FAMILY_NAME}
        for verified in ({"email_verified": False}, {"email_verified": "true"}, {}):
            with (
                self.subTest(verified=verified),
                mock.patch.object(gmail, "exchange_code", return_value=tokens),
                mock.patch.object(gmail, "decode_id_token", return_value={**base, **verified}),
            ):
                response = self.callback({"code": "c", "state": "google-state-1"})
                self.assertEqual(
                    response.headers["Location"], "/auto-import?google_error=email_not_verified"
                )
                self.assertEqual(self.store.users, {})
                with self.client.session_transaction() as sess:
                    self.assertNotIn("google_sub", sess)

        page = self.client.get(response.headers["Location"]).data.decode()
        self.assertIn("hasn&#39;t verified this account&#39;s email address", page)

    def test_non_allowlisted_email_shows_friendly_page_and_stores_nothing(self):
        tokens = {
            "access_token": "ga",
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "id_token": "id-tok",
        }
        claims = {
            "sub": SUB,
            "email": UNINVITED_EMAIL,
            "email_verified": True,
            "family_name": "Visitor",
        }
        with (
            mock.patch.object(gmail, "exchange_code", return_value=tokens),
            mock.patch.object(gmail, "decode_id_token", return_value=claims),
        ):
            response = self.callback({"code": "c", "state": "google-state-1"})

        self.assertEqual(response.headers["Location"], "/auto-import?not_invited=1")
        self.assertEqual(self.store.users, {})
        with self.client.session_transaction() as sess:
            self.assertNotIn("google_sub", sess)

        friendly = self.client.get(response.headers["Location"])
        self.assertEqual(friendly.status_code, 403)
        friendly_text = " ".join(friendly.data.decode().split())
        self.assertIn("Invite only", friendly_text)
        self.assertIn("Ask the owner for an invite", friendly_text)

        # Cannot turn auto-import on via the API either
        self.connect_ynab_oauth()
        save_resp = self.client.post("/api/auto-import", json=valid_settings_payload())
        self.assertEqual(save_resp.status_code, 401)
        self.assertEqual(self.store.users, {})

    def test_allowlisted_email_matches_case_insensitively_after_trimming(self):
        tokens = {
            "access_token": "ga",
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "id_token": "id-tok",
        }
        claims = {
            "sub": SUB,
            "email": "  JUAN@EXAMPLE.TEST  ",
            "email_verified": True,
            "family_name": f"  {FAMILY_NAME}  ",
        }
        with (
            mock.patch.object(gmail, "exchange_code", return_value=tokens),
            mock.patch.object(gmail, "decode_id_token", return_value=claims),
        ):
            response = self.callback({"code": "c", "state": "google-state-1"})

        self.assertEqual(response.headers["Location"], "/auto-import")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["google_sub"], SUB)
            self.assertNotIn("google_oauth_state", sess)

        saved = self.store.get_user(SUB)
        self.assertIsNotNone(saved)
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["email"], "JUAN@EXAMPLE.TEST")
        self.assertEqual(saved["family_name"], FAMILY_NAME)
        self.assertNotEqual(saved["gmail_refresh_token"], GMAIL_REFRESH_TOKEN)
        self.assertEqual(self.store.decrypt(saved["gmail_refresh_token"]), GMAIL_REFRESH_TOKEN)


# =============================================================================
# SAVING SETTINGS & SINGLE-USE YNAB REFRESH TOKEN ROTATION
# =============================================================================


class SaveSettingsTest(AutoImportWebTest):
    def post(self, payload):
        return self.client.post("/api/auto-import", json=payload)

    def test_requires_google_and_ynab_and_invite_list(self):
        self.assertEqual(self.post(valid_settings_payload()).status_code, 401)

        # Signed in earlier, then removed from ALLOWED_GOOGLE_EMAILS
        self.sign_in_google(email="removed@example.test")
        self.connect_ynab_oauth()
        self.assertEqual(self.post(valid_settings_payload()).status_code, 403)

        # Invited Google user, but YNAB not connected
        self.sign_in_google(email=ALLOWED_EMAIL)
        web_state.STORE.clear()
        no_ynab = self.post(valid_settings_payload())
        self.assertEqual(no_ynab.status_code, 401)
        self.assertTrue(no_ynab.get_json()["reconnect"])

    def test_rejects_two_enabled_sources_mapped_to_same_account(self):
        self.sign_in_google()
        self.connect_ynab_oauth()

        with mock.patch.object(ynab_oauth, "refresh") as refresh:
            response = self.post(gcash_and_gotyme_payload(gotyme_account_id=ACCOUNT_ID))
        self.assertEqual(response.status_code, 400)
        self.assertIn("same YNAB account", response.get_json()["error"])
        refresh.assert_not_called()  # validated before rotating the YNAB token

    def test_saves_gcash_and_gotyme_to_their_own_accounts(self):
        self.sign_in_google()
        self.connect_ynab_oauth()
        rotated = {"access_token": NEW_YNAB_ACCESS, "refresh_token": NEW_YNAB_REFRESH}

        with mock.patch.object(ynab_oauth, "refresh", return_value=rotated):
            response = self.post(gcash_and_gotyme_payload())

        self.assertEqual(response.status_code, 200)
        saved = self.store.get_user(SUB)
        accounts = {key: m["account_id"] for key, m in saved["mappings"].items()}
        self.assertEqual(accounts, {"gcash": ACCOUNT_ID, "gotyme": OTHER_ACCOUNT_ID})
        self.assertEqual(
            json.loads(self.store.decrypt(saved["password_inputs"]["gotyme"])),
            {"birth_date": "1990-05-15"},
        )

    def test_rejects_a_date_of_birth_that_is_not_a_date(self):
        self.sign_in_google()
        self.connect_ynab_oauth()

        for bad_date in ("05151990", "1990-02-30", ""):  # MMDDYYYY typed in; no Feb 30; blank
            payload = gcash_and_gotyme_payload()
            payload["password_inputs"]["gotyme"] = {"birth_date": bad_date}
            with self.subTest(birth_date=bad_date):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("mappings", self.store.users[SUB])  # nothing saved

    def test_rejects_missing_or_invalid_password_inputs(self):
        self.sign_in_google()
        self.connect_ynab_oauth()

        for bad_inputs in (
            {"last_name": "Dela Cruz"},  # missing mobile_last4
            {"last_name": "   ", "mobile_last4": "1234"},  # blank last_name
            {"last_name": "Dela Cruz", "mobile_last4": "12ab"},  # fails gcash.build_password
            None,
        ):
            payload = valid_settings_payload()
            payload["password_inputs"]["gcash"] = bad_inputs
            with self.subTest(bad_inputs=bad_inputs):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)

    def test_rejects_unknown_or_unsupported_source_key(self):
        self.sign_in_google()
        self.connect_ynab_oauth()

        self.patch_dict(sources.SOURCES, {"nomail": NO_EMAIL_SOURCE})
        for bad_key in ("unknown_wallet", "nomail"):  # nomail has no email_query
            payload = {
                "plan_id": PLAN_ID,
                "plan_name": "Test Plan",
                "mappings": {
                    bad_key: {"account_id": ACCOUNT_ID, "account_name": "Acct", "enabled": True}
                },
                "password_inputs": {bad_key: {}},
            }
            with self.subTest(bad_key=bad_key):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)

    def test_rotates_single_use_ynab_refresh_token_and_drops_it_from_session(self):
        self.sign_in_google()
        self.connect_ynab_oauth(refresh_token=OLD_YNAB_REFRESH)
        rotated = {
            "access_token": NEW_YNAB_ACCESS,
            "refresh_token": NEW_YNAB_REFRESH,
            "expires_in": 7200,
        }

        with mock.patch.object(ynab_oauth, "refresh", return_value=rotated) as refresh:
            response = self.post(valid_settings_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"enabled": True})

        # 1. Refreshed once using the session's old refresh token
        refresh.assert_called_once_with(OLD_YNAB_REFRESH)

        # 2. Stored the NEW refresh token encrypted in Firestore (never double-encrypted)
        saved = self.store.get_user(SUB)
        self.assertTrue(saved["enabled"])
        self.assertIsInstance(saved["last_checked"], datetime)
        self.assertNotEqual(saved["ynab_refresh_token"], NEW_YNAB_REFRESH)
        self.assertEqual(self.store.decrypt(saved["ynab_refresh_token"]), NEW_YNAB_REFRESH)
        self.assertEqual(self.store.decrypt(saved["gmail_refresh_token"]), GMAIL_REFRESH_TOKEN)
        self.assertEqual(
            json.loads(self.store.decrypt(saved["password_inputs"]["gcash"])),
            {"last_name": "Dela Cruz", "mobile_last4": "1234"},
        )

        # 3. Browser session keeps only the new short-lived access token; refresh_token is gone
        session_ynab = self.ynab_state()
        self.assertEqual(session_ynab["access_token"], NEW_YNAB_ACCESS)
        self.assertNotIn("refresh_token", session_ynab)

        # Editing mappings in the same session reuses the stored YNAB refresh token
        # without re-encrypting secrets or overwriting last_checked
        first_checked = saved["last_checked"]
        updated = valid_settings_payload(account_id=OTHER_ACCOUNT_ID, account_name="Other Wallet")
        with mock.patch.object(ynab_oauth, "refresh") as second_refresh:
            edit_resp = self.post(updated)
        self.assertEqual(edit_resp.status_code, 200)
        second_refresh.assert_not_called()

        after_edit = self.store.get_user(SUB)
        self.assertEqual(after_edit["last_checked"], first_checked)
        self.assertEqual(after_edit["mappings"]["gcash"]["account_id"], OTHER_ACCOUNT_ID)
        self.assertEqual(self.store.decrypt(after_edit["ynab_refresh_token"]), NEW_YNAB_REFRESH)
        self.assertEqual(self.store.decrypt(after_edit["gmail_refresh_token"]), GMAIL_REFRESH_TOKEN)

    def test_saving_starts_gmail_push_for_the_user(self):
        self.sign_in_google()
        self.connect_ynab_oauth()
        rotated = {"access_token": NEW_YNAB_ACCESS, "refresh_token": NEW_YNAB_REFRESH}

        with (
            mock.patch.object(ynab_oauth, "refresh", return_value=rotated),
            mock.patch.object(runner, "start_watch") as start_watch,
        ):
            response = self.post(valid_settings_payload())

        self.assertEqual(response.status_code, 200)
        start_watch.assert_called_once()
        user = start_watch.call_args.args[0]
        self.assertEqual(user["sub"], SUB)
        self.assertEqual(self.store.decrypt(user["gmail_refresh_token"]), GMAIL_REFRESH_TOKEN)

    def test_failed_ynab_refresh_returns_401_and_clears_session_ynab(self):
        self.sign_in_google()
        self.connect_ynab_oauth()
        with mock.patch.object(
            ynab_oauth, "refresh", side_effect=urllib.error.URLError("ynab unreachable")
        ):
            response = self.post(valid_settings_payload())
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.get_json()["reconnect"])
        self.assertNotIn("access_token", self.ynab_state())
        self.assertNotIn("refresh_token", self.ynab_state())
        self.assertFalse(self.store.get_user(SUB)["enabled"])


# =============================================================================
# TEST PASSWORD & TURN OFF AND DELETE
# =============================================================================


class TestPasswordRouteTest(AutoImportWebTest):
    def test_runs_runner_test_password_with_typed_inputs(self):
        self.sign_in_google()
        inputs = {"last_name": "Dela Cruz", "mobile_last4": "1234"}
        with mock.patch.object(
            runner,
            "test_password",
            return_value=(True, "The password opens your newest GCash statement."),
        ) as test_pw:
            response = self.client.post(
                "/api/auto-import/test", json={"source": "gcash", "inputs": inputs}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"ok": True, "message": "The password opens your newest GCash statement."},
        )
        called_user, called_source, called_inputs = test_pw.call_args.args
        self.assertEqual(called_user["sub"], SUB)
        self.assertEqual(called_source, "gcash")
        self.assertEqual(called_inputs, inputs)

    def test_validates_source_inputs_and_surfaces_gmail_error(self):
        self.sign_in_google()
        self.assertEqual(
            self.client.post(
                "/api/auto-import/test", json={"source": "nope", "inputs": {}}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/auto-import/test", json={"source": "gotyme", "inputs": {}}
            ).status_code,
            400,  # no birth_date
        )
        self.assertEqual(
            self.client.post(
                "/api/auto-import/test",
                json={"source": "gcash", "inputs": {"last_name": "Dela Cruz"}},
            ).status_code,
            400,
        )

        with mock.patch.object(
            runner, "test_password", side_effect=gmail.GmailError("Google API returned HTTP 503")
        ):
            response = self.client.post(
                "/api/auto-import/test",
                json={
                    "source": "gcash",
                    "inputs": {"last_name": "Dela Cruz", "mobile_last4": "1234"},
                },
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("Could not reach Gmail", response.get_json()["error"])


class DeleteSettingsTest(AutoImportWebTest):
    def test_deletes_user_and_runs_then_revokes_decrypted_token_and_clears_google_session(self):
        self.sign_in_google(enabled=True)
        self.store.write_run(SUB, "msg-1", "gcash", status=runner.OK, created=2, skipped=0)
        self.set_ynab_state(access_token="keep-ynab-session", token_kind="oauth")

        events = []
        real_delete = self.store.delete_user

        def track_delete(sub):
            events.append(("delete_user", sub))
            real_delete(sub)

        def track_revoke(token):
            events.append(("revoke", token))

        with (
            mock.patch.object(self.store, "delete_user", side_effect=track_delete),
            mock.patch.object(gmail, "revoke", side_effect=track_revoke),
        ):
            response = self.client.post("/api/auto-import/delete")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"deleted": True, "revoked": True, "warning": None})
        self.assertEqual(
            events,
            [("delete_user", SUB), ("revoke", GMAIL_REFRESH_TOKEN)],
        )
        self.assertIsNone(self.store.get_user(SUB))
        self.assertEqual(self.store.list_runs(SUB), [])
        with self.client.session_transaction() as sess:
            self.assertNotIn("google_sub", sess)
        self.assertEqual(self.ynab_state()["access_token"], "keep-ynab-session")

    def test_stops_gmail_push_before_revoking_when_a_watch_was_started(self):
        self.sign_in_google(enabled=True, gmail_watch_expires=datetime.now(UTC))
        events = []

        with (
            mock.patch.object(gmail, "refresh_access_token", return_value="gmail-access") as access,
            mock.patch.object(gmail, "stop", side_effect=lambda t: events.append(("stop", t))),
            mock.patch.object(gmail, "revoke", side_effect=lambda t: events.append(("revoke", t))),
        ):
            response = self.client.post("/api/auto-import/delete")

        self.assertEqual(response.status_code, 200)
        access.assert_called_once_with(GMAIL_REFRESH_TOKEN)
        self.assertEqual(events, [("stop", "gmail-access"), ("revoke", GMAIL_REFRESH_TOKEN)])

    def test_a_failed_stop_still_revokes_and_deletes(self):
        self.sign_in_google(enabled=True, gmail_watch_expires=datetime.now(UTC))

        with (
            mock.patch.object(gmail, "refresh_access_token", side_effect=gmail.GmailError("down")),
            mock.patch.object(gmail, "revoke") as revoke,
        ):
            response = self.client.post("/api/auto-import/delete")

        self.assertEqual(response.get_json()["revoked"], True)
        revoke.assert_called_once_with(GMAIL_REFRESH_TOKEN)
        self.assertIsNone(self.store.get_user(SUB))

    def test_revoke_failure_still_finishes_delete_and_returns_warning(self):
        self.sign_in_google(enabled=True)
        self.store.write_run(SUB, "msg-1", "gcash", status=runner.OK, created=1, skipped=0)

        with mock.patch.object(gmail, "revoke", side_effect=gmail.GmailError("network down")):
            response = self.client.post("/api/auto-import/delete")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["deleted"])
        self.assertFalse(body["revoked"])
        self.assertIn("Third-party access", body["warning"])
        self.assertIsNone(self.store.get_user(SUB))
        self.assertEqual(self.store.list_runs(SUB), [])
        with self.client.session_transaction() as sess:
            self.assertNotIn("google_sub", sess)


# =============================================================================
# CLOUD SCHEDULER ROUTE - /tasks/poll-gmail
# =============================================================================


class PollTaskTest(AutoImportWebTest):
    def test_rejects_missing_or_invalid_oidc_token_before_any_work(self):
        with mock.patch.object(runner, "poll_all") as poll_all:
            # Missing / wrong scheme
            self.assertEqual(self.client.post("/tasks/poll-gmail").status_code, 403)
            self.assertEqual(
                self.client.post(
                    "/tasks/poll-gmail", headers={"Authorization": "Basic abc"}
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/tasks/poll-gmail", headers={"Authorization": "Bearer   "}
                ).status_code,
                403,
            )

            # Exception from verify_oauth2_token -> 403
            with mock.patch.object(
                auto_import.google.oauth2.id_token,
                "verify_oauth2_token",
                side_effect=ValueError("Wrong audience"),
            ):
                resp = self.client.post(
                    "/tasks/poll-gmail", headers={"Authorization": "Bearer forged-token"}
                )
                self.assertEqual(resp.status_code, 403)

            # Wrong service account email -> 403
            for bad_claims in (
                {"email": f"other-sa@{PROJECT_ID}.iam.gserviceaccount.com", "email_verified": True},
                {"email": ALLOWED_EMAIL, "email_verified": True},
                {"email": SCHEDULER_EMAIL, "email_verified": False},
                {"email": SCHEDULER_EMAIL},
            ):
                with (
                    self.subTest(bad_claims=bad_claims),
                    mock.patch.object(
                        auto_import.google.oauth2.id_token,
                        "verify_oauth2_token",
                        return_value=bad_claims,
                    ),
                ):
                    resp = self.client.post(
                        "/tasks/poll-gmail", headers={"Authorization": "Bearer valid-but-wrong-sa"}
                    )
                    self.assertEqual(resp.status_code, 403)

            # Missing project ID -> 403 before verify
            with (
                mock.patch.dict(os.environ, {"GCP_PROJECT_ID": ""}),
                mock.patch.object(
                    auto_import.google.auth,
                    "default",
                    side_effect=google.auth.exceptions.DefaultCredentialsError("no metadata"),
                ),
            ):
                resp = self.client.post(
                    "/tasks/poll-gmail", headers={"Authorization": "Bearer some-token"}
                )
                self.assertEqual(resp.status_code, 403)

            poll_all.assert_not_called()

    def test_verifies_audience_from_public_url_never_request_host_and_runs_poll(self):
        counts = {"users": 1, "runs": 2, "failed": 0, "deferred": 0}
        valid_claims = {"email": SCHEDULER_EMAIL, "email_verified": True}

        with (
            mock.patch.object(
                auto_import.google.oauth2.id_token,
                "verify_oauth2_token",
                return_value=valid_claims,
            ) as verify,
            mock.patch.object(runner, "poll_all", return_value=counts) as poll_all,
        ):
            response = self.client.post(
                "/tasks/poll-gmail",
                headers={
                    "Authorization": "Bearer oidc-jwt-token",
                    "Host": "attacker.example.test",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), counts)
        poll_all.assert_called_once_with()
        token_arg, request_arg = verify.call_args.args
        self.assertEqual(token_arg, "oidc-jwt-token")
        self.assertIsInstance(request_arg, auto_import.google.auth.transport.requests.Request)
        self.assertEqual(verify.call_args.kwargs["audience"], f"{PUBLIC_URL}/tasks/poll-gmail")

    def test_falls_back_to_google_auth_default_project_id_on_cloud_run(self):
        counts = {"users": 0, "runs": 0, "failed": 0, "deferred": 0}
        claims = {
            "email": "scheduler-invoker@metadata-project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        with (
            mock.patch.dict(os.environ, {"GCP_PROJECT_ID": ""}),
            mock.patch.object(
                auto_import.google.auth, "default", return_value=(None, "metadata-project")
            ),
            mock.patch.object(
                auto_import.google.oauth2.id_token, "verify_oauth2_token", return_value=claims
            ),
            mock.patch.object(runner, "poll_all", return_value=counts),
        ):
            response = self.client.post(
                "/tasks/poll-gmail", headers={"Authorization": "Bearer oidc-jwt"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), counts)


# =============================================================================
# GMAIL PUSH ROUTE - /tasks/gmail-push
# =============================================================================


def push_body(data):
    """A Pub/Sub push request body; `data` is the message's payload (dict, or raw text)."""
    raw = json.dumps(data).encode() if isinstance(data, dict) else data.encode()
    return {
        "message": {"data": base64.b64encode(raw).decode(), "messageId": "1"},
        "subscription": f"projects/{PROJECT_ID}/subscriptions/gmail-push",
    }


class PushTaskTest(AutoImportWebTest):
    BODY = push_body({"emailAddress": ALLOWED_EMAIL, "historyId": "12345"})

    def push(self, body=BODY, claims=None, token="oidc-jwt-token"):
        """POST /tasks/gmail-push with a token that verifies to `claims` (Pub/Sub's by default)."""
        claims = claims or {"email": PUBSUB_EMAIL, "email_verified": True}
        with mock.patch.object(
            auto_import.google.oauth2.id_token, "verify_oauth2_token", return_value=claims
        ) as verify:
            response = self.client.post(
                "/tasks/gmail-push",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Host": "attacker.example.test"},
            )
        return response, verify

    def test_imports_for_the_pushed_address_and_answers_204(self):
        with mock.patch.object(runner, "process_push", return_value=runner.PUSH_DONE) as process:
            response, verify = self.push()

        self.assertEqual((response.status_code, response.data), (204, b""))
        process.assert_called_once_with(ALLOWED_EMAIL)
        # The audience comes from PUBLIC_URL, never from the request's Host header
        self.assertEqual(verify.call_args.kwargs["audience"], f"{PUBLIC_URL}/tasks/gmail-push")

    def test_busy_answers_503_so_pubsub_sends_it_again(self):
        with mock.patch.object(runner, "process_push", return_value=runner.PUSH_BUSY):
            self.assertEqual(self.push()[0].status_code, 503)

    def test_ignored_and_failed_pushes_answer_204_so_pubsub_does_not_retry(self):
        for status in (runner.PUSH_IGNORED, runner.PUSH_FAILED):
            with (
                self.subTest(status=status),
                mock.patch.object(runner, "process_push", return_value=status),
            ):
                self.assertEqual(self.push()[0].status_code, 204)

    def test_a_message_without_an_address_answers_204_and_imports_nothing(self):
        bad_bodies = [
            None,
            {},
            {"message": {}},
            {"message": {"data": "not base64!"}},
            push_body("not json"),
            push_body({"historyId": "12345"}),
            push_body({"emailAddress": 42}),
            push_body({"emailAddress": "   "}),
            {"message": {"data": base64.b64encode(b'["a list"]').decode()}},
        ]
        with mock.patch.object(runner, "process_push") as process:
            for body in bad_bodies:
                with self.subTest(body=body):
                    self.assertEqual(self.push(body=body)[0].status_code, 204)
        process.assert_not_called()

    def test_rejects_a_missing_or_wrong_token_before_any_work(self):
        with mock.patch.object(runner, "process_push") as process:
            # No token, or not a bearer token
            self.assertEqual(self.client.post("/tasks/gmail-push", json=self.BODY).status_code, 403)
            self.assertEqual(self.push(token="")[0].status_code, 403)

            # Signed for another account: the scheduler's token doesn't work here
            for bad_claims in (
                {"email": SCHEDULER_EMAIL, "email_verified": True},
                {"email": ALLOWED_EMAIL, "email_verified": True},
                {"email": PUBSUB_EMAIL, "email_verified": False},
            ):
                with self.subTest(bad_claims=bad_claims):
                    self.assertEqual(self.push(claims=bad_claims)[0].status_code, 403)

            # A token that doesn't verify (wrong audience, expired, forged)
            with mock.patch.object(
                auto_import.google.oauth2.id_token,
                "verify_oauth2_token",
                side_effect=ValueError("Wrong audience"),
            ):
                response = self.client.post(
                    "/tasks/gmail-push", json=self.BODY, headers={"Authorization": "Bearer forged"}
                )
                self.assertEqual(response.status_code, 403)
        process.assert_not_called()

    def test_pubsub_token_does_not_work_on_the_poll_route(self):
        claims = {"email": PUBSUB_EMAIL, "email_verified": True}
        with (
            mock.patch.object(
                auto_import.google.oauth2.id_token, "verify_oauth2_token", return_value=claims
            ),
            mock.patch.object(runner, "poll_all") as poll_all,
        ):
            response = self.client.post(
                "/tasks/poll-gmail", headers={"Authorization": "Bearer pubsub-token"}
            )
        self.assertEqual(response.status_code, 403)
        poll_all.assert_not_called()


# =============================================================================
# RATE LIMITS, PRIVACY PAGE & NO PERSONAL DATA IN LOGS
# =============================================================================


class LimitsPrivacyAndLoggingTest(AutoImportWebTest):
    def test_google_and_auto_import_api_routes_are_rate_limited(self):
        for _ in range(20):
            self.assertEqual(
                self.client.get(
                    "/google/login", headers={"X-Forwarded-For": "203.0.113.10"}
                ).status_code,
                302,
            )
        limited = self.client.get("/google/login", headers={"X-Forwarded-For": "203.0.113.10"})
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Too many requests", limited.get_json()["error"])

        for _ in range(20):
            self.assertEqual(
                self.client.post(
                    "/api/auto-import", headers={"X-Forwarded-For": "203.0.113.11"}
                ).status_code,
                401,
            )
        self.assertEqual(
            self.client.post(
                "/api/auto-import", headers={"X-Forwarded-For": "203.0.113.11"}
            ).status_code,
            429,
        )

    def test_privacy_page_includes_limited_use_and_auto_import_disclosures(self):
        privacy = self.client.get("/privacy").data.decode()
        # 1. Google API Services User Data Policy Limited Use statement
        self.assertIn("Google API Services User Data Policy", privacy)
        self.assertIn("Limited Use requirements", privacy)
        self.assertIn("https://developers.google.com/terms/api-services-user-data-policy", privacy)
        # 2. What auto-import stores and for how long
        self.assertIn("encrypted in\n      the app before being written to Firestore", privacy)
        self.assertIn("until you turn auto-import off", privacy)
        # 3. Statement-only search and no stored email content beyond run log
        self.assertIn("gmail.readonly", privacy)
        self.assertIn("No email body, PDF or statement transaction is\n      stored", privacy)
        # 4. Turn off and delete
        self.assertIn("Turn off and delete my data", privacy)
        self.assertIn("revokes the app's Google token", privacy)

    def test_never_logs_email_token_or_password_input(self):
        sensitive = [
            ALLOWED_EMAIL,
            UNINVITED_EMAIL,
            FAMILY_NAME,
            "1234",
            "delacruz1234",
            GMAIL_REFRESH_TOKEN,
            OLD_YNAB_REFRESH,
            NEW_YNAB_REFRESH,
            NEW_YNAB_ACCESS,
            "secret-oidc-token",
        ]
        tokens = {
            "access_token": "secret-google-access",
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "id_token": "id-tok",
        }
        claims = {
            "sub": SUB,
            "email": ALLOWED_EMAIL,
            "email_verified": True,
            "family_name": FAMILY_NAME,
        }
        rotated = {
            "access_token": NEW_YNAB_ACCESS,
            "refresh_token": NEW_YNAB_REFRESH,
            "expires_in": 7200,
        }

        with self.assertLogs(level="DEBUG") as logs:
            with self.client.session_transaction() as sess:
                sess["google_oauth_state"] = "s1"
            with (
                mock.patch.object(gmail, "exchange_code", return_value=tokens),
                mock.patch.object(gmail, "decode_id_token", return_value=claims),
            ):
                self.client.get("/google/callback?code=c1&state=s1")

            self.connect_ynab_oauth()
            with mock.patch.object(ynab_oauth, "refresh", return_value=rotated):
                self.assertEqual(
                    self.client.post("/api/auto-import", json=valid_settings_payload()).status_code,
                    200,
                )

            # Force the unexpected-PDF-error branch, which logs only the exception class
            with mock.patch.object(
                runner,
                "test_password",
                side_effect=RuntimeError(f"failed for {ALLOWED_EMAIL} {FAMILY_NAME} 1234"),
            ):
                self.assertEqual(
                    self.client.post(
                        "/api/auto-import/test",
                        json={
                            "source": "gcash",
                            "inputs": {"last_name": FAMILY_NAME, "mobile_last4": "1234"},
                        },
                    ).status_code,
                    400,
                )

            with mock.patch.object(gmail, "revoke"):
                self.assertEqual(self.client.post("/api/auto-import/delete").status_code, 200)

        combined = "\n".join(logs.output)
        self.assertIn("RuntimeError", combined)
        for item in sensitive:
            self.assertNotIn(item, combined)


# =============================================================================
# HEADLESS CHROMIUM FLOW - strict CSP, Test button, Save -> W3, Delete
# =============================================================================


@unittest.skipIf(
    sync_playwright is None and not REQUIRE_E2E,
    "Playwright not installed (pip install -r requirements-dev.txt)",
)
class AutoImportBrowserTest(unittest.TestCase):
    """Drive /auto-import in headless Chromium to verify JS, DOM and strict CSP."""

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise RuntimeError("REQUIRE_E2E=1 but Playwright is not installed")
        cls.enterClassContext(hosted_mode(AUTO_IMPORT_ENV))
        cls.store = FakeStore()
        cls.enterClassContext(mock.patch.object(auto_import, "store", cls.store))
        cls.server = FakeServer().start()
        cls.addClassCleanup(cls.server.stop)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch()
        except PlaywrightError as e:
            if REQUIRE_E2E:
                raise
            raise unittest.SkipTest(
                f"Chromium not installed (python -m playwright install chromium): {e}"
            ) from None
        cls.addClassCleanup(cls.browser.close)
        expect.set_options(timeout=10_000)

    def setUp(self):
        self.server.reset()
        self.store.users.clear()
        self.store.runs.clear()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.console_errors = []
        self.page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text))

    def tearDown(self):
        self.context.close()

    def test_setup_test_turn_on_and_delete_in_browser(self):
        page = self.page

        # Main page shows the Auto-import header link
        page.goto(self.server.url, wait_until="networkidle")
        expect(page.locator("#auto-import-link")).to_be_visible()
        page.click("#auto-import-link")
        expect(page.locator("h1")).to_have_text("Auto-import from Gmail")

        # 1. Simulate Google OAuth sign-in via /google/login -> /google/callback
        tokens = {
            "access_token": "ga",
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "id_token": "id-tok",
        }
        claims = {
            "sub": SUB,
            "email": ALLOWED_EMAIL,
            "email_verified": True,
            "family_name": FAMILY_NAME,
        }
        page.route(
            "https://accounts.google.com/**",
            lambda route: route.fulfill(body="google-consent"),
        )
        with (
            mock.patch.object(gmail, "exchange_code", return_value=tokens),
            mock.patch.object(gmail, "decode_id_token", return_value=claims),
        ):
            login_resp = page.request.get(f"{self.server.url}/google/login", max_redirects=0)
            state = urllib.parse.parse_qs(
                urllib.parse.urlparse(login_resp.headers["location"]).query
            )["state"][0]
            page.goto(
                f"{self.server.url}/google/callback?code=c1&state={state}",
                wait_until="networkidle",
            )

        expect(page.locator("#gmail-on-label")).to_contain_text(ALLOWED_EMAIL)
        last_name_input = page.locator('tr[data-source="gcash"] input[data-input="last_name"]')
        expect(last_name_input).to_have_value(FAMILY_NAME)

        # 2. Simulate YNAB OAuth sign-in returning to /auto-import
        ynab_tokens = {
            "access_token": fake_ynab.TOKEN,
            "refresh_token": OLD_YNAB_REFRESH,
            "expires_in": 7200,
        }
        with mock.patch.object(ynab_oauth, "exchange_token", return_value=ynab_tokens):
            ynab_login = page.request.get(
                f"{self.server.url}/oauth/login?next=/auto-import", max_redirects=0
            )
            ynab_state = urllib.parse.parse_qs(
                urllib.parse.urlparse(ynab_login.headers["location"]).query
            )["state"][0]
            page.goto(
                f"{self.server.url}/oauth/callback?code=yc1&state={ynab_state}",
                wait_until="networkidle",
            )

        # 3. Single plan auto-selects; pick YNAB account and fill mobile_last4
        expect(page.locator("#plan")).to_have_value(fake_ynab.PLAN_ID)
        page.select_option('tr[data-source="gcash"] .account-select', fake_ynab.EXISTING_ACCOUNT_ID)
        page.fill('tr[data-source="gcash"] input[data-input="mobile_last4"]', "1234")

        # 4. Test password button
        with mock.patch.object(
            runner,
            "test_password",
            return_value=(True, "The password opens your newest GCash statement."),
        ):
            page.click('tr[data-source="gcash"] .test-btn')
            expect(page.locator("#test-msg-gcash")).to_contain_text(
                "✓ The password opens your newest GCash statement."
            )

        # 5. Turn on -> switches to W3 active view
        rotated = {
            "access_token": fake_ynab.TOKEN,
            "refresh_token": NEW_YNAB_REFRESH,
            "expires_in": 7200,
        }
        with mock.patch.object(ynab_oauth, "refresh", return_value=rotated):
            expect(page.locator("#save-btn")).to_be_enabled()
            # Wait for the save's response before the mock is undone. A click
            # doesn't navigate, so waiting for "networkidle" returned at once,
            # and the POST could reach the real refresh() and fail.
            with page.expect_response(
                lambda r: r.url.endswith("/api/auto-import") and r.request.method == "POST"
            ) as save:
                page.click("#save-btn")
            self.assertEqual(save.value.status, 200)

        expect(page.locator("#auto-on-view")).to_be_visible()
        expect(page.locator("#card-active")).to_contain_text("On")
        expect(page.locator("#card-active")).to_contain_text(fake_ynab.EXISTING_ACCOUNT_NAME)

        # 6. Turn off and delete -> back to setup view with confirmation
        with mock.patch.object(gmail, "revoke"):
            page.click("#delete-btn")
            expect(page.locator("#page-msg")).to_contain_text("deleted")
            expect(page.locator("#auto-setup-view")).to_be_visible()
            expect(page.locator("#card-gmail")).not_to_have_class(re.compile(r"\bcomplete\b"))

        # Strict CSP allowed every script and stylesheet on the page
        self.assertEqual(self.console_errors, [])


if __name__ == "__main__":
    unittest.main()
