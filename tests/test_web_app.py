"""
Tests for the web app (web/app.py and its blueprints): every route, the
token handling and the YNAB error mapping, through Flask's test client.

Run from the project folder:  python -m unittest discover tests

YNAB, OAuth and the OS secure store are mocked. All data here is made up.
"""

import contextlib
import io
import json
import logging
import os
import runpy
import time
import unittest
import urllib.error
import urllib.parse
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from ynab import exceptions

from statement_etl import pipeline
from statement_etl.convert import rows_to_transactions
from statement_etl.gemini import GeminiError
from statement_etl.parsers.common import StatementError
from statement_etl.web import app as web
from statement_etl.web import config, token_store, upload, ynab_auth, ynab_oauth
from statement_etl.web import state as web_state
from statement_etl.ynab_api.balance import compare_balance
from statement_etl.ynab_api.format import to_ynab
from tests.fakes import ACCOUNT_ID, PLAN_ID, gcash_row, ynab_txn
from tests.hosted import HOSTED_ENV, PUBLIC_URL, SECRETS, hosted_mode
from tests.test_parsers_gotyme_goalsave import TWO_ACCOUNT_ROWS

ROWS = [
    gcash_row(
        "2026-10-01 09:00 AM",
        "Received GCash from Test Sender",
        "1000000000001",
        credit="1,000.00",
        balance="1,500.00",
    ),
    gcash_row(
        "2026-10-02 06:50 PM",
        "Refund from Test Shop",
        "R1000000000002",
        credit="50.00",
        balance="1,550.00",
    ),
    gcash_row(
        "2026-10-02 06:50 PM",
        "Payment to Test Shop",
        "1000000000002",
        debit="250.50",
        balance="1,299.50",
    ),
]
TRANSACTIONS = [to_ynab(t) for t in rows_to_transactions(ROWS, "gcash")]
TARGET = {"plan_id": PLAN_ID, "account_id": ACCOUNT_ID}


def unauthorized():
    return exceptions.UnauthorizedException(status=401, reason="Unauthorized")


class WebTest(unittest.TestCase):
    """A test client with no saved token, no OAuth settings and an empty STORE."""

    GEMINI_API_KEY = ""  # none; empty, so a .env file can't add one

    def setUp(self):
        self.patch_dict(web_state.STORE, {}, clear=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("YNAB_CLIENT_")}
        env["GEMINI_API_KEY"] = self.GEMINI_API_KEY
        self.patch_dict(os.environ, env, clear=True)
        self.available = self.patch(token_store, "available", return_value=False)
        self.store_name = self.patch(token_store, "store_name", return_value=None)
        self.load_token = self.patch(token_store, "load_token", return_value=None)
        self.save_token = self.patch(token_store, "save_token", return_value=True)
        self.delete_token = self.patch(token_store, "delete_token")
        self.client = web.app.test_client()

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_dict(self, target, values, **kwargs):
        patcher = mock.patch.dict(target, values, **kwargs)
        self.addCleanup(patcher.stop)
        patcher.start()

    def set_state(self, **state):
        """This browser's server-side state."""
        with self.client.session_transaction() as session:
            session["sid"] = "test-sid"
        web_state.STORE["test-sid"] = state
        return state

    def state(self):
        with self.client.session_transaction() as session:
            return web_state.STORE[session["sid"]]

    def connected(self, kind="personal", **state):
        return self.set_state(access_token="test-token", token_kind=kind, **state)

    def parsed(self, **state):
        return self.connected(source="gcash", rows=ROWS, transactions=TRANSACTIONS, **state)

    def oauth_env(self):
        self.patch_dict(
            os.environ, {"YNAB_CLIENT_ID": "client-id", "YNAB_CLIENT_SECRET": "client-secret"}
        )


# =============================================================================
# ROUTES - the same URLs and methods as before the split into blueprints
# =============================================================================


class RoutesTest(unittest.TestCase):
    def test_every_route_is_still_there(self):
        routes = {
            (rule.rule, method)
            for rule in web.app.url_map.iter_rules()
            for method in rule.methods - {"HEAD", "OPTIONS"}
        }
        self.assertEqual(
            routes,
            {
                ("/", "GET"),
                ("/api/status", "GET"),
                ("/api/progress", "GET"),
                ("/api/parse", "POST"),
                ("/api/statement-account", "POST"),
                ("/api/plans", "GET"),
                ("/api/plans/<plan_id>/accounts", "GET"),
                ("/api/starting-balance", "POST"),
                ("/api/upload", "POST"),
                ("/api/categorize", "POST"),
                ("/api/token", "POST"),
                ("/oauth/login", "GET"),
                ("/oauth/callback", "GET"),
                ("/oauth/logout", "POST"),
                ("/static/<path:filename>", "GET"),
                ("/privacy", "GET"),
                ("/terms", "GET"),
                ("/transfer-rules", "GET"),
                ("/api/transfer-rules", "POST"),
            },
        )


class StartupWarningTest(unittest.TestCase):
    """Hosted without a Gemini key, the log says why the opt-in boxes are missing."""

    def test_hosted_without_gemini(self):
        with self.assertLogs("statement_etl", "WARNING") as logs:
            web.warn_if_categorize_off(hosted=True, available=False)
        self.assertIn("GEMINI_API_KEY isn't set", logs.output[0])

    def test_quiet_otherwise(self):
        for hosted, available in ((True, True), (False, False)):
            with self.subTest(hosted=hosted), self.assertNoLogs("statement_etl", "WARNING"):
                web.warn_if_categorize_off(hosted=hosted, available=available)


# =============================================================================
# PAGE AND STATUS
# =============================================================================


class IndexTest(WebTest):
    def test_page_lists_the_sources(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"GCash", response.data)
        self.assertIn(b'value="gcash"', response.data)

    def test_no_inline_script_or_style(self):
        # A strict Content-Security-Policy blocks inline code: it lives in static/
        page = self.client.get("/").data
        self.assertIn(b'<script src="/static/app.js">', page)
        self.assertIn(b'<link rel="stylesheet" href="/static/app.css">', page)
        self.assertNotIn(b"<script>", page)
        self.assertNotIn(b"<style", page)
        self.assertNotIn(b"style=", page)
        for path in ("/static/app.js", "/static/app.css"):
            with self.subTest(path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)


class StatusTest(WebTest):
    def test_not_connected(self):
        self.assertEqual(
            self.client.get("/api/status").get_json(),
            {
                "connected": False,
                "connect_error": None,
                "token_kind": None,
                "oauth_configured": False,
                "token_allowed": True,
                "remember_available": False,
                "store_name": None,
                "categorize_available": False,  # local mode: never
                "parsed": None,
            },
        )

    def test_saved_token_connects_by_itself(self):
        self.load_token.return_value = "saved-token"
        self.available.return_value = True
        self.store_name.return_value = "Test Store"
        with mock.patch.object(upload, "get_user") as get_user:
            status = self.client.get("/api/status").get_json()
        get_user.assert_called_once_with("saved-token")
        self.assertTrue(status["connected"])
        self.assertEqual(status["token_kind"], "saved")
        self.assertTrue(status["remember_available"])
        self.assertEqual(status["store_name"], "Test Store")

    def test_revoked_saved_token_is_removed(self):
        self.load_token.return_value = "saved-token"
        with mock.patch.object(upload, "get_user", side_effect=unauthorized()):
            status = self.client.get("/api/status").get_json()
        self.assertFalse(status["connected"])
        self.assertIn("saved token", status["connect_error"])
        self.assertIsNone(status["token_kind"])
        self.delete_token.assert_called_once()

    def test_oauth_configured(self):
        self.oauth_env()
        self.assertTrue(self.client.get("/api/status").get_json()["oauth_configured"])

    def test_parsed_statement_is_restored(self):
        self.set_state(parsed={"filename": "statement.pdf"})
        self.assertEqual(
            self.client.get("/api/status").get_json()["parsed"], {"filename": "statement.pdf"}
        )


# =============================================================================
# PERSONAL ACCESS TOKEN
# =============================================================================


class TokenTest(WebTest):
    def post(self, body):
        return self.client.post("/api/token", json=body)

    def test_empty_token(self):
        for body in ({"token": "   "}, {}, None):
            with self.subTest(body=body):
                response = self.post(body) if body is not None else self.client.post("/api/token")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json()["error"], "Enter your YNAB personal access token."
                )

    def test_rejected_token(self):
        with mock.patch.object(ynab_auth, "get_user", side_effect=unauthorized()):
            response = self.post({"token": "bad-token"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("YNAB rejected this token", response.get_json()["error"])

    def test_ynab_down(self):
        error = exceptions.ApiException(
            status=500, reason="Server Error", body='{"error": {"detail": "try later"}}'
        )
        with mock.patch.object(ynab_auth, "get_user", side_effect=error):
            response = self.post({"token": "test-token"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "YNAB returned 500: try later")

    def test_connects_for_this_session(self):
        with mock.patch.object(ynab_auth, "get_user") as get_user:
            response = self.post({"token": "  test-token  ", "remember": True})
        get_user.assert_called_once_with("test-token")
        self.assertEqual(
            response.get_json(),
            {"connected": True, "token_kind": "personal", "saved": False, "save_error": None},
        )
        self.save_token.assert_not_called()  # no secure store
        state = self.state()
        self.assertEqual((state["access_token"], state["token_kind"]), ("test-token", "personal"))

    def test_remembered(self):
        self.available.return_value = True
        with mock.patch.object(ynab_auth, "get_user"):
            response = self.post({"token": "test-token", "remember": True})
        self.save_token.assert_called_once_with("test-token")
        self.assertEqual(response.get_json()["token_kind"], "saved")
        self.assertTrue(response.get_json()["saved"])

    def test_not_remembered_unless_asked(self):
        self.available.return_value = True
        with mock.patch.object(ynab_auth, "get_user"):
            self.post({"token": "test-token"})
        self.save_token.assert_not_called()

    def test_save_fails(self):
        self.available.return_value = True
        self.store_name.return_value = "Test Store"
        self.save_token.return_value = False
        with mock.patch.object(ynab_auth, "get_user"):
            body = self.post({"token": "test-token", "remember": True}).get_json()
        self.assertEqual(body["token_kind"], "personal")
        self.assertEqual(
            body["save_error"],
            "Couldn't save the token in the Test Store; it will be used for this session only.",
        )

    def test_replaces_oauth_and_disconnect(self):
        self.set_state(
            access_token="oauth-token",
            token_kind="oauth",
            refresh_token="rt",
            expires_at=1,
            skip_saved=True,
        )
        with mock.patch.object(ynab_auth, "get_user"):
            self.post({"token": "test-token"})
        state = self.state()
        self.assertEqual(state, {"access_token": "test-token", "token_kind": "personal"})


# =============================================================================
# OAUTH
# =============================================================================


class OAuthTest(WebTest):
    def test_login_without_settings(self):
        response = self.client.get("/oauth/login")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"YNAB_CLIENT_ID and YNAB_CLIENT_SECRET are not set", response.data)

    def test_login_redirects_to_ynab(self):
        self.oauth_env()
        response = self.client.get("/oauth/login")
        self.assertEqual(response.status_code, 302)
        url = urllib.parse.urlparse(response.location)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", ynab_auth.YNAB_AUTHORIZE_URL)
        query = urllib.parse.parse_qs(url.query)
        with self.client.session_transaction() as session:
            state = session["oauth_state"]
        self.assertEqual(
            query,
            {
                "client_id": ["client-id"],
                "redirect_uri": [config.REDIRECT_URI],
                "response_type": ["code"],
                "state": [state],
            },
        )

    def callback(self, query, session_state="state-1"):
        with self.client.session_transaction() as session:
            session.pop("oauth_state", None)
            if session_state:
                session["oauth_state"] = session_state
        return self.client.get("/oauth/callback?" + urllib.parse.urlencode(query))

    def test_callback_error(self):
        response = self.callback({"error": "access denied"})
        self.assertEqual(response.location, "/?oauth_error=access%20denied")

    def test_callback_state_mismatch(self):
        for query, session_state in (
            ({"state": "other", "code": "c"}, "state-1"),
            ({"code": "c"}, "state-1"),
            ({"state": "state-1", "code": "c"}, None),
        ):
            with self.subTest(query=query, session_state=session_state):
                with mock.patch.object(ynab_oauth, "exchange_token") as exchange:
                    response = self.callback(query, session_state)
                self.assertEqual(response.location, "/?oauth_error=state_mismatch")
                exchange.assert_not_called()

    def test_callback_exchange_fails(self):
        with mock.patch.object(
            ynab_oauth, "exchange_token", side_effect=urllib.error.URLError("down")
        ):
            response = self.callback({"state": "state-1", "code": "c"})
        self.assertEqual(response.location, "/?oauth_error=token_exchange_failed")

    def test_callback_stores_tokens(self):
        token = {"access_token": "oauth-token", "refresh_token": "rt", "expires_in": 7200}
        with mock.patch.object(ynab_oauth, "exchange_token", return_value=token) as exchange:
            response = self.callback({"state": "state-1", "code": "code-1"})
        self.assertEqual(response.location, "/")
        exchange.assert_called_once_with(grant_type="authorization_code", code="code-1")
        state = self.state()
        self.assertEqual(
            (state["access_token"], state["refresh_token"], state["token_kind"]),
            ("oauth-token", "rt", "oauth"),
        )
        self.assertAlmostEqual(state["expires_at"], time.time() + 7200, delta=5)
        with self.client.session_transaction() as session:
            self.assertNotIn("oauth_state", session)  # used once

    def test_exchange_token_request(self):
        self.oauth_env()
        with mock.patch.object(
            ynab_oauth.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"access_token": "oauth-token"}'),
        ) as urlopen:
            token = ynab_oauth.exchange_token(grant_type="refresh_token", refresh_token="rt")
        self.assertEqual(token, {"access_token": "oauth-token"})
        url = urlopen.call_args.args[0]
        body = urllib.parse.parse_qs(urlopen.call_args.kwargs["data"].decode())
        self.assertEqual(url, ynab_oauth.YNAB_TOKEN_URL)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)
        self.assertEqual(
            body,
            {
                "client_id": ["client-id"],
                "client_secret": ["client-secret"],
                "redirect_uri": [config.REDIRECT_URI],
                "grant_type": ["refresh_token"],
                "refresh_token": ["rt"],
            },
        )


class LogoutTest(WebTest):
    def test_disconnect_keeps_the_saved_token(self):
        self.connected("saved")
        response = self.client.post("/oauth/logout")
        self.assertEqual(response.get_json(), {"connected": False})
        self.assertEqual(self.state(), {"skip_saved": True})
        self.delete_token.assert_not_called()
        # the saved token isn't loaded again until the next Connect
        self.load_token.return_value = "saved-token"
        self.assertFalse(self.client.get("/api/status").get_json()["connected"])
        self.load_token.assert_not_called()

    def test_forget_saved_token(self):
        self.connected("saved")
        self.client.post("/oauth/logout", json={"forget": True})
        self.delete_token.assert_called_once()


class GetTokenTest(WebTest):
    def test_personal_and_saved_tokens_never_refresh(self):
        for kind in ("personal", "saved"):
            with self.subTest(kind), mock.patch.object(ynab_oauth, "exchange_token") as exchange:
                self.assertEqual(
                    ynab_auth.get_token({"access_token": "t", "token_kind": kind, "expires_at": 0}),
                    "t",
                )
                exchange.assert_not_called()

    def test_loads_the_saved_token(self):
        self.load_token.return_value = "saved-token"
        state = {}
        self.assertEqual(ynab_auth.get_token(state), "saved-token")
        self.assertEqual(state, {"access_token": "saved-token", "token_kind": "saved"})
        self.assertIsNone(ynab_auth.get_token({"skip_saved": True}))

    def test_not_connected(self):
        self.assertIsNone(ynab_auth.get_token({}))

    def test_oauth_token_still_valid(self):
        state = {
            "access_token": "oauth-token",
            "token_kind": "oauth",
            "expires_at": time.time() + 3600,
        }
        with mock.patch.object(ynab_oauth, "exchange_token") as exchange:
            self.assertEqual(ynab_auth.get_token(state), "oauth-token")
        exchange.assert_not_called()

    def test_oauth_token_about_to_expire_is_refreshed(self):
        state = {
            "access_token": "old",
            "token_kind": "oauth",
            "refresh_token": "rt",
            "expires_at": time.time() + 30,
        }
        with mock.patch.object(
            ynab_oauth, "exchange_token", return_value={"access_token": "new", "expires_in": 60}
        ) as exchange:
            self.assertEqual(ynab_auth.get_token(state), "new")
        exchange.assert_called_once_with(grant_type="refresh_token", refresh_token="rt")
        self.assertEqual(state["refresh_token"], "rt")  # kept when YNAB doesn't send a new one

    def test_failed_refresh_disconnects(self):
        state = {
            "access_token": "old",
            "token_kind": "oauth",
            "refresh_token": "rt",
            "expires_at": 0,
        }
        with mock.patch.object(
            ynab_oauth, "exchange_token", side_effect=urllib.error.URLError("down")
        ):
            self.assertIsNone(ynab_auth.get_token(state))
        self.assertEqual(state, {})

    def test_no_refresh_token(self):
        state = {"access_token": "old", "token_kind": "oauth", "expires_at": 0}
        self.assertIsNone(ynab_auth.get_token(state))


# =============================================================================
# YNAB CALLS AND ERRORS
# =============================================================================


class YnabCallTest(WebTest):
    def plans(self, side_effect):
        with mock.patch.object(upload, "get_plans", side_effect=side_effect) as get_plans:
            response = self.client.get("/api/plans")
        return response, get_plans

    def test_not_connected(self):
        response, get_plans = self.plans([[]])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(), {"error": "Connect to YNAB first.", "reconnect": True}
        )
        get_plans.assert_not_called()

    def test_success(self):
        self.connected()
        plans = [{"id": PLAN_ID, "name": "Test Plan", "currency_format": None}]
        response, get_plans = self.plans([plans])
        self.assertEqual(response.get_json(), plans)
        get_plans.assert_called_once_with("test-token")

    def test_rejected_personal_token(self):
        self.connected("personal")
        response, _ = self.plans(unauthorized())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {
                "error": "YNAB rejected your personal access token (revoked?). Enter a new one.",
                "reconnect": True,
            },
        )
        self.assertEqual(self.state(), {})
        self.delete_token.assert_not_called()

    def test_rejected_saved_token_is_removed(self):
        self.connected("saved")
        response, _ = self.plans(unauthorized())
        self.assertIn("saved token", response.get_json()["error"])
        self.delete_token.assert_called_once()

    def test_oauth_token_refreshed_once_and_retried(self):
        self.connected("oauth", refresh_token="rt", expires_at=time.time() + 3600)
        with mock.patch.object(
            ynab_oauth, "exchange_token", return_value={"access_token": "new-token"}
        ):
            response, get_plans = self.plans([unauthorized(), ["plan"]])
        self.assertEqual(response.get_json(), ["plan"])
        self.assertEqual(
            get_plans.call_args_list, [mock.call("test-token"), mock.call("new-token")]
        )

    def test_oauth_rejected_after_refresh(self):
        self.connected("oauth", refresh_token="rt", expires_at=time.time() + 3600)
        with mock.patch.object(
            ynab_oauth, "exchange_token", return_value={"access_token": "new-token"}
        ):
            response, get_plans = self.plans([unauthorized(), unauthorized()])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json()["error"], "Your YNAB session expired. Reconnect to YNAB."
        )
        self.assertEqual(get_plans.call_count, 2)
        self.assertNotIn("access_token", self.state())

    def test_other_errors(self):
        self.connected()
        cases = [
            (
                exceptions.NotFoundException(status=404, reason="Not Found"),
                404,
                "Plan or account not found.",
            ),
            (
                exceptions.BadRequestException(
                    status=400,
                    reason="Bad Request",
                    data=SimpleNamespace(error=SimpleNamespace(detail="bad date")),
                ),
                400,
                "YNAB: bad date",
            ),
            (
                exceptions.ApiException(status=503, reason="Unavailable"),
                502,
                "YNAB returned 503: Unavailable",
            ),
        ]
        for error, status, message in cases:
            with self.subTest(status=status):
                response, _ = self.plans(error)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json()["error"], message)

    def test_error_detail(self):
        with_data = exceptions.ApiException(
            status=400, reason="r", data=SimpleNamespace(error=SimpleNamespace(detail="from data"))
        )
        with_body = exceptions.ApiException(
            status=400, reason="r", body='{"error": {"detail": "from body"}}'
        )
        bad_body = exceptions.ApiException(status=400, reason="the reason", body="<html>")
        nothing = exceptions.ApiException(status=400)
        self.assertEqual(ynab_auth._error_detail(with_data), "from data")
        self.assertEqual(ynab_auth._error_detail(with_body), "from body")
        self.assertEqual(ynab_auth._error_detail(bad_body), "the reason")
        self.assertEqual(ynab_auth._error_detail(nothing), "Unknown error")

    def test_accounts(self):
        self.connected()
        accounts = [
            {"id": ACCOUNT_ID, "name": "Test Wallet", "type": "checking", "balance": "₱0.00"}
        ]
        with mock.patch.object(upload, "get_accounts", return_value=accounts) as get_accounts:
            response = self.client.get(f"/api/plans/{PLAN_ID}/accounts")
        self.assertEqual(response.get_json(), accounts)
        get_accounts.assert_called_once_with(PLAN_ID, "test-token")


# =============================================================================
# PARSE
class StatementAccountTest(WebTest):
    """A statement that holds several accounts (GoTyme GoalSave): the page shows
    and uploads one at a time, each into its own YNAB account."""

    TRANSACTIONS = [to_ynab(t) for t in rows_to_transactions(TWO_ACCOUNT_ROWS, "gotyme_goalsave")]

    def setUp(self):
        super().setUp()
        self.patch(upload, "parse_statement", return_value=(TWO_ACCOUNT_ROWS, self.TRANSACTIONS))
        form = {
            "source": "gotyme_goalsave",
            "password": "secret",
            "pdf": (io.BytesIO(b"%PDF-test"), "savings.pdf"),
        }
        self.parsed = self.client.post(
            "/api/parse", data=form, content_type="multipart/form-data"
        ).get_json()

    def switch(self, account):
        return self.client.post("/api/statement-account", json={"account": account})

    def test_parse_shows_the_first_account(self):
        self.assertEqual(self.parsed["account_label"], "GoalSave No.")
        self.assertEqual(self.parsed["account"], "11111")
        self.assertEqual(
            self.parsed["accounts"],
            [{"account": "11111", "count": 1}, {"account": "22222", "count": 2}],
        )
        self.assertEqual(
            self.parsed["rows"], [[TWO_ACCOUNT_ROWS[0][c] for c in self.parsed["columns"]]]
        )
        self.assertEqual(self.parsed["starting_balance"], "0.00")
        state = self.state()
        self.assertEqual(state["rows"], TWO_ACCOUNT_ROWS[:1])
        self.assertEqual(state["transactions"], self.TRANSACTIONS[:1])

    def test_switch_to_another_account(self):
        response = self.switch("22222")
        self.assertEqual(response.status_code, 200)
        parsed = response.get_json()
        self.assertEqual(parsed["account"], "22222")
        self.assertEqual(parsed["statement_count"], 2)
        self.assertEqual(parsed["starting_balance"], "50,000.00")
        self.assertEqual(parsed["ending_balance"], "42,200.00")
        self.assertEqual(self.state()["rows"], TWO_ACCOUNT_ROWS[1:])
        self.assertEqual(self.client.get("/api/status").get_json()["parsed"], parsed)

    def test_an_account_the_statement_does_not_have(self):
        response = self.switch("99999")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "This statement has no account '99999'.")
        self.assertEqual(self.state()["rows"], TWO_ACCOUNT_ROWS[:1])  # unchanged

    def test_needs_a_parsed_statement(self):
        web_state.STORE.clear()
        response = self.switch("22222")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Parse a statement first.")


# =============================================================================


class ParseTest(WebTest):
    def post(self, data):
        return self.client.post("/api/parse", data=data, content_type="multipart/form-data")

    def pdf(self, **fields):
        return {"password": "secret", "pdf": (io.BytesIO(b"%PDF-test"), "statement.pdf"), **fields}

    def test_unknown_source(self):
        response = self.post(self.pdf(source="nope"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "Unknown source 'nope'. Known sources: gcash, gotyme, gotyme_goalsave",
        )

    def test_no_file(self):
        response = self.post({"password": "secret"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Choose a PDF file.")

    def test_parse_errors(self):
        cases = [
            (StatementError("Incorrect PDF password."), "Incorrect PDF password."),
            (StatementError("Total Credit doesn't match"), "Total Credit doesn't match"),
            # other errors never reach the page: they can carry internal details
            (
                ValueError("time data 'x' does not match format"),
                "Could not read the PDF. Is it the statement PDF?",
            ),
            (
                RuntimeError("/internal/path/x.py"),
                "Could not read the PDF. Is it the statement PDF?",
            ),
        ]
        for error, message in cases:
            with (
                self.subTest(message),
                mock.patch.object(upload, "parse_statement", side_effect=error),
            ):
                response = self.post(self.pdf())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], message)

    def test_unexpected_parse_error_logs_only_its_type(self):
        error = RuntimeError("/internal/path/x.py")
        with (
            mock.patch.object(upload, "parse_statement", side_effect=error),
            self.assertLogs(web.app.logger, level="WARNING") as logs,
        ):
            self.post(self.pdf())
        self.assertEqual(
            logs.output,
            [f"WARNING:{web.app.logger.name}:Could not read the uploaded PDF: RuntimeError"],
        )

    def test_parsed_statement(self):
        with mock.patch.object(
            upload, "parse_statement", return_value=(ROWS, TRANSACTIONS)
        ) as parse:
            response = self.post(self.pdf())
        self.assertEqual(response.status_code, 200)
        pdf, password, source = parse.call_args.args
        self.assertEqual(
            (pdf.read(), password, source), (b"%PDF-test", "secret", "gcash")
        )  # default source

        parsed = response.get_json()
        self.assertEqual(
            parsed,
            {
                "source": "gcash",
                "source_name": "GCash",
                "filename": "statement.pdf",
                "columns": [
                    "Date and Time",
                    "Description",
                    "Reference No.",
                    "Debit",
                    "Credit",
                    "Balance",
                ],
                "rows": [[row[c] for c in parsed["columns"]] for row in ROWS],
                "amount_columns": ["Debit", "Credit", "Balance"],
                "money_in_columns": ["Credit"],
                "money_out_columns": ["Debit"],
                "text_column": "Description",
                "correction_rows": [1],
                "summary": "Total debit 250.50 · Total credit 1,000.00 · matches statement ✓",
                "statement_count": 2,
                "count": 3,
                "corrections": [
                    {
                        "date_time": "2026-10-02 06:50 PM",
                        "description": "Refund from Test Shop",
                        "reference": "R1000000000002",
                        "amount": "+50.00",
                    }
                ],
                "balance_warning": parsed["balance_warning"],
                "starting_balance": "500.00",
                "ending_balance": "1,299.50",
                # one account: no GoalSave No. picker
                "account_label": None,
                "account": "",
                "accounts": [],
                "own_transfers": None,  # GCash rows aren't always transfers
            },
        )
        self.assertTrue(
            parsed["balance_warning"].startswith("Balance does not add up: 1 correction row(s)")
        )

        state = self.state()
        self.assertEqual(
            (state["source"], state["rows"], state["transactions"], state["parsed"]),
            ("gcash", ROWS, TRANSACTIONS, parsed),
        )
        self.assertEqual(self.client.get("/api/status").get_json()["parsed"], parsed)

    def test_keeps_the_page_it_is_reading_while_it_runs(self):
        self.set_state()  # the page's first request (/api/status) gave it a session
        seen = []

        def parse(pdf, password, source, on_step):
            for page in (1, 2):
                on_step("read", {"page": page, "pages": 2})
                seen.append(self.client.get("/api/progress").get_json())
            return ROWS, TRANSACTIONS

        with mock.patch.object(upload, "parse_statement", side_effect=parse):
            self.assertEqual(self.post(self.pdf()).status_code, 200)
        self.assertEqual(
            seen,
            [
                {"step": "read", "detail": {"page": 1, "pages": 2}},
                {"step": "read", "detail": {"page": 2, "pages": 2}},
            ],
        )
        self.assertIsNone(self.client.get("/api/progress").get_json())

    def test_forgets_the_step_when_the_parse_fails(self):
        def parse(pdf, password, source, on_step):
            on_step("read", {"page": 1, "pages": 2})
            raise StatementError("Total Credit doesn't match")

        with mock.patch.object(upload, "parse_statement", side_effect=parse):
            self.assertEqual(self.post(self.pdf()).status_code, 400)
        self.assertIsNone(self.client.get("/api/progress").get_json())


# =============================================================================
# BALANCE CHECK AND UPLOAD
# =============================================================================


def starting_check(**kwargs):
    return compare_balance(
        "starting", Decimal("500"), Decimal("1234.5"), Decimal("2033.5"), **kwargs
    )


class CheckJsonTest(unittest.TestCase):
    def test_money(self):
        self.assertIsNone(upload._money(None))
        self.assertEqual(upload._money(Decimal("1234.5")), "1,234.50")
        self.assertEqual(upload._money(Decimal("-0.5")), "-0.50")

    def test_check_json(self):
        check = starting_check(already_in_ynab=1, to_upload=2)
        self.assertEqual(
            upload._check_json(check),
            {
                "kind": "starting",
                "matches": False,
                "new_account": False,
                "statement": "500.00",
                "ynab_before": "1,234.50",
                "ynab_after": "2,033.50",
                "difference": "+734.50",
                "already_in_ynab": 1,
                "to_upload": 2,
                "message": upload.balance_check_message(check),
            },
        )

    def test_ending_check_json(self):
        check = {
            "kind": "ending",
            "statement": Decimal("1"),
            "ynab_before": None,
            "ynab_after": Decimal("1"),
            "difference": Decimal("0"),
            "matches": True,
            "new_account": None,
        }
        result = upload._check_json(check)
        self.assertEqual(
            (result["ynab_before"], result["already_in_ynab"], result["to_upload"]), (None, 0, None)
        )
        self.assertEqual(result["difference"], "+0.00")


class StartingBalanceTest(WebTest):
    def post(self, body):
        return self.client.post("/api/starting-balance", json=body)

    def test_needs_a_parsed_statement(self):
        self.connected()
        response = self.post(TARGET)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Parse a statement first.")

    def test_needs_plan_and_account(self):
        self.parsed()
        for body in ({}, {"plan_id": PLAN_ID}, {"account_id": ACCOUNT_ID}):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "Choose a plan and an account.")

    def test_not_connected(self):
        self.set_state(source="gcash", rows=ROWS, transactions=TRANSACTIONS)
        self.assertEqual(self.post(TARGET).status_code, 401)

    def test_check(self):
        self.parsed()
        check = starting_check()
        with mock.patch.object(upload, "starting_balance_check", return_value=check) as start:
            response = self.post(TARGET)
        self.assertEqual(
            response.get_json(),
            {**upload._check_json(check), "transfers": [], "unlinked_message": None},
        )
        start.assert_called_once_with(
            ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "test-token", source="gcash", transfers={}
        )


class UploadTest(WebTest):
    def setUp(self):
        super().setUp()
        self.new_account = self.patch(pipeline, "new_account_starting_balance", return_value=None)
        self.balance = self.patch(pipeline, "get_account_balance", return_value=Decimal("500"))
        self.upload = self.patch(
            pipeline,
            "upload_transactions",
            return_value=SimpleNamespace(
                transactions=[
                    ynab_txn(
                        id="txn-1",
                        var_date="2026-10-01",
                        payee_name="Received GCash from Test Sender",
                        category_name="Ready to Assign",
                        memo="GCash Ref: 1000000000001",
                        amount=1000000,
                    ),
                    ynab_txn(
                        id="txn-2",
                        var_date="2026-10-02",
                        payee_name=None,
                        category_name=None,
                        memo=None,
                        amount=50000,
                        flag_color=SimpleNamespace(value="red"),
                    ),
                ],
                duplicate_import_ids=["ETLPH:already"],
            ),
        )
        self.ending = compare_balance(
            "ending", Decimal("1299.5"), Decimal("500"), Decimal("1299.5")
        )
        self.end = self.patch(pipeline, "ending_balance_check", return_value=self.ending)

    def post(self, body=TARGET):
        return self.client.post("/api/upload", json=body)

    def progress(self):
        """What GET /api/progress answers now."""
        return self.client.get("/api/progress").get_json()

    def test_the_page_names_only_the_accounts_not_uploaded_yet(self):
        # A statement with several accounts (GoTyme GoalSave), uploaded one at a time
        self.parsed(
            filename="savings.pdf",
            account="11111",
            accounts={"11111": (ROWS, TRANSACTIONS), "22222": (ROWS, TRANSACTIONS)},
        )
        self.assertEqual(self.post().get_json()["other_accounts"], ["22222"])

        switched = self.client.post("/api/statement-account", json={"account": "22222"})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(self.post().get_json()["other_accounts"], [])

    def test_keeps_the_step_while_it_runs(self):
        self.parsed()
        seen = []
        uploaded = self.upload.return_value
        self.upload.side_effect = lambda *args, **kwargs: seen.append(self.progress()) or uploaded
        self.end.side_effect = lambda *args, **kwargs: seen.append(self.progress()) or self.ending
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(
            seen,
            [
                {"step": "upload", "detail": {"count": len(TRANSACTIONS)}},
                {"step": "end_balance", "detail": {}},
            ],
        )
        self.assertIsNone(self.progress())

    def test_forgets_the_step_when_the_upload_fails(self):
        self.parsed()
        self.upload.side_effect = exceptions.BadRequestException(status=400, reason="Bad Request")
        self.assertEqual(self.post().status_code, 400)
        self.assertIsNone(self.progress())

    def test_needs_a_parsed_statement(self):
        self.connected()
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Parse a statement first.")

    def test_needs_plan_and_account(self):
        self.parsed()
        response = self.post({"plan_id": PLAN_ID})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Choose a plan and an account.")
        self.upload.assert_not_called()

    def test_upload(self):
        self.parsed()
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "created": [
                    {
                        "id": "txn-1",
                        "date": "2026-10-01",
                        "payee": "Received GCash from Test Sender",
                        "category": "Ready to Assign",
                        "memo": "GCash Ref: 1000000000001",
                        "amount": 1000000,
                        "flag": "",
                    },
                    {
                        "id": "txn-2",
                        "date": "2026-10-02",
                        "payee": "",
                        "category": "Uncategorized",
                        "memo": "",
                        "amount": 50000,
                        "flag": "red",
                    },
                ],
                "skipped": 1,
                "transfers": 0,
                "unlinked_message": None,
                "to_categorize": 0,
                "ynab_url": f"https://app.ynab.com/{PLAN_ID}/accounts/{ACCOUNT_ID}",
                "other_accounts": [],
                "ending_check": upload._check_json(self.ending),
                "ending_check_error": None,
                "new_account_message": None,
            },
        )
        self.new_account.assert_called_once_with(
            ROWS, ACCOUNT_ID, PLAN_ID, "test-token", source="gcash"
        )
        self.balance.assert_called_once_with(PLAN_ID, ACCOUNT_ID, "test-token")
        self.upload.assert_called_once_with(TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "test-token")
        self.end.assert_called_once_with(
            ROWS, ACCOUNT_ID, PLAN_ID, "test-token", Decimal("500"), source="gcash"
        )

    def test_new_account(self):
        self.parsed()
        self.new_account.return_value = {"from": Decimal("0"), "to": Decimal("500")}
        message = self.post().get_json()["new_account_message"]
        self.assertEqual(
            message,
            "New YNAB account: Starting Balance set from 0.00 to 500.00"
            " (the statement starting balance).",
        )

    def test_nothing_created(self):
        self.parsed()
        self.upload.return_value = SimpleNamespace(transactions=None, duplicate_import_ids=None)
        body = self.post().get_json()
        self.assertEqual((body["created"], body["skipped"]), ([], 0))

    def test_errors_before_the_upload_stop_it(self):
        for step in (self.new_account, self.balance):
            with self.subTest(step=step):
                self.parsed()
                step.side_effect = exceptions.NotFoundException(status=404, reason="Not Found")
                response = self.post()
                step.side_effect = None
                self.assertEqual(response.status_code, 404)
                self.upload.assert_not_called()

    def test_upload_error(self):
        self.parsed()
        self.upload.side_effect = exceptions.BadRequestException(status=400, reason="Bad Request")
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "YNAB: Bad Request")
        self.end.assert_not_called()

    def test_ending_check_error_is_reported_not_raised(self):
        self.parsed()
        self.end.side_effect = exceptions.ApiException(status=503, reason="Unavailable")
        body = self.post().get_json()
        self.assertIsNone(body["ending_check"])
        self.assertEqual(body["ending_check_error"], "YNAB returned 503: Unavailable")
        self.assertEqual(len(body["created"]), 2)


# =============================================================================
# STARTING THE APP
# =============================================================================


class MainTest(WebTest):
    def run_main(self):
        stderr = io.StringIO()
        with mock.patch.object(web.app, "run") as run, contextlib.redirect_stderr(stderr):
            web.main()
        run.assert_called_once_with(host="127.0.0.1", port=config.PORT, debug=False)
        return stderr.getvalue()

    def test_binds_to_this_computer_only(self):
        self.patch_dict(os.environ, {"FLASK_SECRET_KEY": "test"})
        out = self.run_main()
        self.assertNotIn("FLASK_SECRET_KEY", out)
        self.assertIn("Enter your YNAB personal access token on the page to connect.", out)
        self.assertIn(f"Open http://localhost:{config.PORT}", out)
        self.assertNotIn("OAuth", out)

    def test_messages(self):
        os.environ.pop("FLASK_SECRET_KEY", None)
        self.oauth_env()
        self.load_token.return_value = "saved-secret-token"
        self.store_name.return_value = "Test Store"
        out = self.run_main()
        self.assertIn("Note: FLASK_SECRET_KEY not set", out)
        self.assertIn("Using the saved YNAB token from the Test Store.", out)
        self.assertNotIn("saved-secret-token", out)  # never print the token
        self.assertIn(f"YNAB OAuth also available (redirect URI: {config.REDIRECT_URI}).", out)

    def test_python_dash_m(self):
        with mock.patch.object(web, "main") as main:
            runpy.run_module("statement_etl.web", run_name="__main__")
        main.assert_called_once()


# =============================================================================
# PRIVACY AND TERMS
# =============================================================================

FOOTER_LINKS = [b'href="/privacy"', b'href="/terms"', b"github.com/Jomaries777/ph-statement-import"]


class PagesTest(WebTest):
    def test_pages(self):
        for path, text in (("/privacy", b"Last updated: "), ("/terms", b"provided as is")):
            with self.subTest(path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn(text, response.data)

    def test_footer_on_every_page(self):
        for path in ("/", "/privacy", "/terms"):
            with self.subTest(path), self.client.get(path) as response:
                self.assertIn(b"Your PDF is processed in memory and never stored.", response.data)
                for link in FOOTER_LINKS:
                    self.assertIn(link, response.data)

    def test_privacy_says_this_is_the_local_version(self):
        self.assertIn(b"running the local version", self.client.get("/privacy").data)


# =============================================================================
# SECURITY - headers in both modes; nothing is ever logged that shouldn't be
# =============================================================================

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self' https://app.ynab.com"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class SecurityHeadersTest(WebTest):
    def test_every_response_has_them(self):
        for path in ("/", "/api/status", "/static/app.js", "/no-such-page"):
            with self.subTest(path), self.client.get(path) as response:
                for name, value in SECURITY_HEADERS.items():
                    self.assertEqual(response.headers[name], value)


class LocalModeTest(WebTest):
    """What hosted mode adds stays off on your own computer."""

    def test_no_rate_limit(self):
        for _ in range(15):
            self.assertEqual(self.client.post("/api/parse").status_code, 400)

    def test_cookie_is_not_https_only(self):
        cookie = self.client.get("/api/status").headers["Set-Cookie"]
        self.assertNotIn("Secure", cookie)

    def test_idle_browsers_are_kept(self):
        self.set_state(rows=ROWS)
        with mock.patch.object(web_state.time, "time", return_value=time.time() + 24 * 3600):
            self.client.get("/api/status")
        self.assertEqual(self.state()["rows"], ROWS)


class LoggingTest(WebTest):
    """The log never gets the PDF, its password, a token or a statement row."""

    def test_parse_and_upload(self):
        self.set_state(access_token="log-test-token", token_kind="personal")
        secrets = ["log-test-password", "log-test-token", "%PDF-log-test"]
        secrets += [row["Description"] for row in ROWS] + [row["Reference No."] for row in ROWS]
        imported = SimpleNamespace(
            created=[ynab_txn(memo="GCash Ref: 1000000000001")],
            skipped=0,
            ending_check=None,
            ending_error=None,
            new_account=None,
            transfers=0,
        )
        form = {
            "password": "log-test-password",
            "pdf": (io.BytesIO(b"%PDF-log-test"), "statement.pdf"),
        }
        with self.assertLogs(level="DEBUG") as logs:
            # A PDF that can't be read: the one request that logs (the error type)
            with mock.patch.object(upload, "parse_statement", side_effect=ValueError(ROWS[0])):
                self.client.post("/api/parse", data=dict(form), content_type="multipart/form-data")
            form["pdf"] = (io.BytesIO(b"%PDF-log-test"), "statement.pdf")
            with mock.patch.object(upload, "parse_statement", return_value=(ROWS, TRANSACTIONS)):
                self.client.post("/api/parse", data=form, content_type="multipart/form-data")
            with mock.patch.object(upload, "import_statement", return_value=imported):
                self.assertEqual(self.client.post("/api/upload", json=TARGET).status_code, 200)
        log = "\n".join(logs.output)
        self.assertIn("ValueError", log)
        for secret in secrets:
            self.assertNotIn(secret, log)

    def test_app_log_lines_reach_stderr(self):
        """Cloud Run keeps stderr: auto-import's INFO lines must get there, once each."""
        runner_log = logging.getLogger("statement_etl.autoimport.runner")
        self.assertTrue(runner_log.isEnabledFor(logging.INFO))
        self.assertFalse(logging.getLogger("urllib3").isEnabledFor(logging.INFO))

        handlers = logging.getLogger("statement_etl").handlers
        self.assertEqual(len(handlers), 1)
        stderr = io.StringIO()
        with mock.patch.object(handlers[0], "stream", stderr):
            runner_log.info("Auto-import poll: %s", {"users": 0})
        self.assertEqual(
            stderr.getvalue(),
            "INFO statement_etl.autoimport.runner: Auto-import poll: {'users': 0}\n",
        )


# =============================================================================
# HOSTED MODE - PUBLIC_URL set: YNAB OAuth only, never the OS secure store
# =============================================================================


class Hosted:
    """Mixin for a WebTest: the app as it runs on the server, for other people."""

    def setUp(self):
        super().setUp()
        self.enterContext(hosted_mode())
        self.client = web.app.test_client()
        # One token saved on the server would connect every visitor
        for name in ("available", "store_name", "load_token", "save_token", "delete_token"):
            getattr(token_store, name).side_effect = AssertionError(
                f"hosted mode called token_store.{name}()"
            )

    def set_state(self, **state):
        # As session_state() stamps it on every request; without it, the entry
        # counts as idle and is dropped
        state.setdefault("last_seen", time.time())
        return super().set_state(**state)


class HostedTest(Hosted, WebTest):
    def test_mode(self):
        self.assertTrue(config.HOSTED)
        self.assertEqual(config.REDIRECT_URI, f"{PUBLIC_URL}/oauth/callback")

    def test_secrets_come_from_app_config(self):
        for key, value in SECRETS.items():
            self.assertEqual(os.environ[key], value)
        self.assertEqual(web.app.secret_key, SECRETS["FLASK_SECRET_KEY"])
        self.assertTrue(ynab_oauth.oauth_configured())

    def test_personal_access_token_is_refused(self):
        with mock.patch.object(ynab_auth, "get_user") as get_user:
            response = self.client.post("/api/token", json={"token": "a-token", "remember": True})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["error"],
            "Personal access tokens are only supported in the local version. "
            "Use Connect with YNAB login.",
        )
        get_user.assert_not_called()

    def test_status(self):
        status = self.client.get("/api/status").get_json()
        self.assertFalse(status["token_allowed"])
        self.assertFalse(status["remember_available"])
        self.assertIsNone(status["store_name"])
        self.assertTrue(status["oauth_configured"])
        self.assertFalse(status["connected"])  # no saved token to load

    def test_page_shows_only_the_ynab_login(self):
        page = self.client.get("/").data
        self.assertIn(b'id="connect-btn"', page)
        for element in (b'id="ynab-token"', b'id="token-btn"', b'id="remember-row"'):
            self.assertNotIn(element, page)

    def test_privacy_and_terms(self):
        for path in ("/privacy", "/terms"):
            with self.subTest(path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                for link in FOOTER_LINKS:
                    self.assertIn(link, response.data)
        privacy = self.client.get("/privacy").data
        self.assertIn(b"us-west1", privacy)
        self.assertNotIn(b"running the local version", privacy)

    def test_privacy_link_next_to_the_ynab_login(self):
        page = self.client.get("/").data.decode()
        button = page.index('id="connect-btn"')
        self.assertIn('<a href="/privacy">Privacy</a>', page[button : page.index("</div>", button)])

    def test_oauth_redirect_uri(self):
        location = self.client.get("/oauth/login").headers["Location"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        self.assertEqual(query["redirect_uri"], [f"{PUBLIC_URL}/oauth/callback"])
        self.assertEqual(query["client_id"], ["test-client-id"])

    def test_oauth_still_connects(self):
        self.set_state()
        with self.client.session_transaction() as session:
            session["oauth_state"] = "test-state"
        token = {"access_token": "oauth-token", "refresh_token": "refresh", "expires_in": 7200}
        with mock.patch.object(ynab_oauth, "exchange_token", return_value=token):
            response = self.client.get("/oauth/callback?code=test-code&state=test-state")
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(self.state()["token_kind"], "oauth")

    def test_logout_never_touches_the_secure_store(self):
        self.connected(kind="oauth", expires_at=time.time() + 3600)
        response = self.client.post("/oauth/logout", json={"forget": True})
        self.assertEqual(response.get_json(), {"connected": False})

    def test_main_skips_the_keychain_message(self):
        stderr = io.StringIO()
        with mock.patch.object(web.app, "run"), contextlib.redirect_stderr(stderr):
            web.main()
        out = stderr.getvalue()
        self.assertIn(f"Hosted mode: YNAB OAuth login only (redirect URI: {PUBLIC_URL}", out)
        self.assertNotIn("personal access token", out)
        self.assertNotIn("FLASK_SECRET_KEY", out)

    def test_session_cookie_is_https_only(self):
        cookie = self.client.get("/api/status").headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_security_headers(self):
        response = self.client.get("/")
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(response.headers[name], value)

    def test_idle_browsers_are_dropped(self):
        now = time.time()
        web_state.STORE.update(
            {
                "idle": {"rows": ROWS, "last_seen": now - web_state.IDLE_SECONDS - 1},
                "active": {"rows": ROWS, "last_seen": now - web_state.IDLE_SECONDS + 60},
            }
        )
        with mock.patch.object(web_state.time, "time", return_value=now):
            self.client.get("/api/status")
        self.assertNotIn("idle", web_state.STORE)
        self.assertIn("active", web_state.STORE)

    def test_each_request_counts_as_activity(self):
        self.client.get("/api/status")
        later = time.time() + web_state.IDLE_SECONDS - 60
        with mock.patch.object(web_state.time, "time", return_value=later):
            self.client.get("/api/status")
        self.assertEqual(self.state()["last_seen"], later)
        with mock.patch.object(web_state.time, "time", return_value=later + 120):
            self.client.get("/api/status")  # still there: idle 2 minutes only
        self.assertEqual(self.state()["last_seen"], later + 120)

    def test_parse_is_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/api/parse").status_code, 400)
        with mock.patch.object(upload, "parse_statement") as parse:
            response = self.client.post(
                "/api/parse",
                data={"pdf": (io.BytesIO(b"%PDF-test"), "statement.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.get_json(), {"error": "Too many requests. Wait a minute, then try again."}
        )
        parse.assert_not_called()  # blocked before any parsing work

    def test_rate_limit_is_per_client_ip(self):
        # ProxyFix: the client's IP is the one Cloud Run puts in X-Forwarded-For
        def parse(ip):
            return self.client.post("/api/parse", headers={"X-Forwarded-For": ip}).status_code

        for _ in range(10):
            self.assertEqual(parse("203.0.113.1"), 400)
        self.assertEqual(parse("203.0.113.1"), 429)
        self.assertEqual(parse("203.0.113.2"), 400)

    def test_oauth_is_rate_limited(self):
        for _ in range(20):
            self.assertEqual(self.client.get("/oauth/login").status_code, 302)
        self.assertEqual(self.client.get("/oauth/login").status_code, 429)
        self.assertEqual(self.client.post("/oauth/logout").status_code, 200)  # its own count


class HostedUploadTest(Hosted, UploadTest):
    """Every upload test again in hosted mode, and the statement is dropped after it."""

    def test_the_other_accounts_are_kept_after_the_upload(self):
        self.parsed(
            filename="savings.pdf",
            account="11111",
            accounts={"11111": (ROWS, TRANSACTIONS), "22222": (ROWS, TRANSACTIONS)},
        )
        self.assertEqual(self.post().get_json()["other_accounts"], ["22222"])
        state = self.state()
        self.assertEqual(list(state["accounts"]), ["22222"])
        self.assertEqual(state["source"], "gcash")
        for key in ("rows", "transactions", "parsed", "account"):
            self.assertNotIn(key, state)

    def test_statement_is_dropped_after_the_upload(self):
        self.parsed(parsed={"filename": "statement.pdf"})
        self.assertEqual(self.post().status_code, 200)
        state = self.state()
        for key in ("source", "rows", "transactions", "parsed"):
            self.assertNotIn(key, state)
        self.assertEqual(state["access_token"], "test-token")  # still connected
        with mock.patch.object(upload, "get_user"):
            self.assertIsNone(self.client.get("/api/status").get_json()["parsed"])

    def test_statement_is_kept_when_the_upload_fails(self):
        self.parsed()
        self.upload.side_effect = exceptions.BadRequestException(status=400, reason="Bad Request")
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.state()["rows"], ROWS)  # so the user can try again


class CategorizeTest(Hosted, WebTest):
    """Hosted mode with a Gemini key: the upload can be categorized right after."""

    GEMINI_API_KEY = "test-gemini-key"

    def setUp(self):
        super().setUp()
        self.import_statement = self.patch(
            upload,
            "import_statement",
            return_value=SimpleNamespace(
                created=[
                    ynab_txn(id="shop", payee_name="Test Shop", amount=-249000),
                    ynab_txn(id="rule", category_id="c-food", category_name="Groceries"),
                    ynab_txn(id="fix", flag_color=SimpleNamespace(value="red")),
                ],
                skipped=0,
                ending_check=None,
                ending_error=None,
                new_account=None,
                transfers=0,
            ),
        )
        self.categorize = self.patch(upload, "categorize_transactions")

    def upload(self, **body):
        return self.client.post("/api/upload", json={**TARGET, **body})

    def test_status_says_it_is_available(self):
        with mock.patch.object(upload, "get_user"):
            self.assertTrue(self.client.get("/api/status").get_json()["categorize_available"])

    def test_upload_keeps_only_what_it_created_with_no_category(self):
        self.parsed()
        self.assertEqual(self.upload(categorize=True).get_json()["to_categorize"], 1)
        pending = self.state()["to_categorize"]
        self.assertEqual(pending["plan_id"], PLAN_ID)
        self.assertEqual([t["id"] for t in pending["transactions"]], ["shop"])

    def test_not_ticked_keeps_nothing(self):
        self.parsed()
        self.assertEqual(self.upload().get_json()["to_categorize"], 0)
        self.assertNotIn("to_categorize", self.state())

    def test_categorizes_only_the_last_upload(self):
        self.parsed()
        self.upload(categorize=True)
        groceries = {"id": "c-food", "name": "Groceries", "group": "Everyday"}
        self.categorize.return_value = (
            [
                SimpleNamespace(transaction={"id": "shop"}, category=groceries),
                SimpleNamespace(transaction={"id": "other"}, category=None),  # unsure
            ],
            1,
        )

        response = self.client.post("/api/categorize")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"categorized": 1, "total": 2, "categories": {"shop": "Everyday / Groceries"}},
        )
        plan_id, transactions, api_key, token = self.categorize.call_args.args
        self.assertEqual(
            (plan_id, [t["id"] for t in transactions], api_key, token),
            (PLAN_ID, ["shop"], "test-gemini-key", "test-token"),
        )
        self.assertIsNotNone(self.categorize.call_args.kwargs["deadline"])
        # Once only: they're forgotten afterwards
        self.assertEqual(self.client.post("/api/categorize").status_code, 400)

    def test_nothing_uploaded(self):
        self.connected()
        response = self.client.post("/api/categorize")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "Nothing to categorize: upload a statement first."
        )
        self.categorize.assert_not_called()

    def test_gemini_failing_says_the_import_is_done(self):
        self.parsed()
        self.upload(categorize=True)
        self.categorize.side_effect = GeminiError("Gemini ran out of time.")
        with self.assertLogs(web.app.logger, level="WARNING"):
            response = self.client.post("/api/categorize")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"], "Imported, but not categorized: Gemini ran out of time."
        )
        self.assertNotIn("to_categorize", self.state())


class LocalCategorizeTest(WebTest):
    def test_not_in_local_mode(self):
        self.parsed(to_categorize={"plan_id": PLAN_ID, "transactions": [{"id": "shop"}]})
        self.assertEqual(self.client.post("/api/categorize").status_code, 404)


class HostedStartupTest(unittest.TestCase):
    """Hosted mode stops at startup without its secrets, and never prints them."""

    def start(self, **env):
        with self.assertRaises(SystemExit) as caught, hosted_mode({**HOSTED_ENV, **env}):
            pass
        return str(caught.exception)

    def test_missing_secret_key(self):
        message = self.start(
            FLASK_SECRET_KEY="",
            APP_CONFIG=json.dumps({"YNAB_CLIENT_SECRET": "do-not-print-this"}),
        )
        self.assertIn("needs FLASK_SECRET_KEY", message)
        self.assertNotIn("do-not-print-this", message)
        self.assertNotIn("test-client-id", message)

    def test_every_missing_key_is_named(self):
        message = self.start(FLASK_SECRET_KEY="", YNAB_CLIENT_ID="", APP_CONFIG="{}")
        self.assertIn("FLASK_SECRET_KEY, YNAB_CLIENT_ID, YNAB_CLIENT_SECRET", message)

    def test_app_config_is_not_json(self):
        message = self.start(APP_CONFIG='{"FLASK_SECRET_KEY": do-not-print-this')
        self.assertEqual(message, "APP_CONFIG is not valid JSON.")

    def test_app_config_values_must_be_strings(self):
        message = self.start(APP_CONFIG='{"FLASK_SECRET_KEY": 12345678}')
        self.assertEqual(message, "APP_CONFIG: FLASK_SECRET_KEY must be a string.")

    def test_local_mode_again_afterwards(self):
        with hosted_mode():
            self.assertTrue(config.HOSTED)
        self.assertFalse(config.HOSTED)
        self.assertEqual(config.REDIRECT_URI, f"http://localhost:{config.PORT}/oauth/callback")


if __name__ == "__main__":
    unittest.main()
