"""
Tests for web/app.py: every route, the token handling and the YNAB error
mapping, through Flask's test client.

Run from the project folder:  python -m unittest discover tests

YNAB, OAuth and the OS secure store are mocked. All data here is made up.
"""

import contextlib
import io
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

from statement_etl.parsers.common import StatementError
from statement_etl.transform import transform
from statement_etl.web import app as web
from statement_etl.ynab_api.balance import compare_balance
from tests.fakes import ACCOUNT_ID, PLAN_ID, gcash_row, ynab_txn

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
TRANSACTIONS = transform(ROWS, "gcash")
TARGET = {"plan_id": PLAN_ID, "account_id": ACCOUNT_ID}


def unauthorized():
    return exceptions.UnauthorizedException(status=401, reason="Unauthorized")


class WebTest(unittest.TestCase):
    """A test client with no saved token, no OAuth settings and an empty STORE."""

    def setUp(self):
        self.patch_dict(web.STORE, {}, clear=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("YNAB_CLIENT_")}
        self.patch_dict(os.environ, env, clear=True)
        self.available = self.patch(web.token_store, "available", return_value=False)
        self.store_name = self.patch(web.token_store, "store_name", return_value=None)
        self.load_token = self.patch(web.token_store, "load_token", return_value=None)
        self.save_token = self.patch(web.token_store, "save_token", return_value=True)
        self.delete_token = self.patch(web.token_store, "delete_token")
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
        web.STORE["test-sid"] = state
        return state

    def state(self):
        with self.client.session_transaction() as session:
            return web.STORE[session["sid"]]

    def connected(self, kind="personal", **state):
        return self.set_state(access_token="test-token", token_kind=kind, **state)

    def parsed(self, **state):
        return self.connected(source="gcash", rows=ROWS, transactions=TRANSACTIONS, **state)

    def oauth_env(self):
        self.patch_dict(
            os.environ, {"YNAB_CLIENT_ID": "client-id", "YNAB_CLIENT_SECRET": "client-secret"}
        )


# =============================================================================
# PAGE AND STATUS
# =============================================================================


class IndexTest(WebTest):
    def test_page_lists_the_sources(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"GCash", response.data)
        self.assertIn(b'value="gcash"', response.data)


class StatusTest(WebTest):
    def test_not_connected(self):
        self.assertEqual(
            self.client.get("/api/status").get_json(),
            {
                "connected": False,
                "connect_error": None,
                "token_kind": None,
                "oauth_configured": False,
                "remember_available": False,
                "store_name": None,
                "parsed": None,
            },
        )

    def test_saved_token_connects_by_itself(self):
        self.load_token.return_value = "saved-token"
        self.available.return_value = True
        self.store_name.return_value = "Test Store"
        with mock.patch.object(web, "get_user") as get_user:
            status = self.client.get("/api/status").get_json()
        get_user.assert_called_once_with("saved-token")
        self.assertTrue(status["connected"])
        self.assertEqual(status["token_kind"], "saved")
        self.assertTrue(status["remember_available"])
        self.assertEqual(status["store_name"], "Test Store")

    def test_revoked_saved_token_is_removed(self):
        self.load_token.return_value = "saved-token"
        with mock.patch.object(web, "get_user", side_effect=unauthorized()):
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
        with mock.patch.object(web, "get_user", side_effect=unauthorized()):
            response = self.post({"token": "bad-token"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("YNAB rejected this token", response.get_json()["error"])

    def test_ynab_down(self):
        error = exceptions.ApiException(
            status=500, reason="Server Error", body='{"error": {"detail": "try later"}}'
        )
        with mock.patch.object(web, "get_user", side_effect=error):
            response = self.post({"token": "test-token"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "YNAB returned 500: try later")

    def test_connects_for_this_session(self):
        with mock.patch.object(web, "get_user") as get_user:
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
        with mock.patch.object(web, "get_user"):
            response = self.post({"token": "test-token", "remember": True})
        self.save_token.assert_called_once_with("test-token")
        self.assertEqual(response.get_json()["token_kind"], "saved")
        self.assertTrue(response.get_json()["saved"])

    def test_not_remembered_unless_asked(self):
        self.available.return_value = True
        with mock.patch.object(web, "get_user"):
            self.post({"token": "test-token"})
        self.save_token.assert_not_called()

    def test_save_fails(self):
        self.available.return_value = True
        self.store_name.return_value = "Test Store"
        self.save_token.return_value = False
        with mock.patch.object(web, "get_user"):
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
        with mock.patch.object(web, "get_user"):
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
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", web.YNAB_AUTHORIZE_URL)
        query = urllib.parse.parse_qs(url.query)
        with self.client.session_transaction() as session:
            state = session["oauth_state"]
        self.assertEqual(
            query,
            {
                "client_id": ["client-id"],
                "redirect_uri": [web.REDIRECT_URI],
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
                with mock.patch.object(web, "_exchange_token") as exchange:
                    response = self.callback(query, session_state)
                self.assertEqual(response.location, "/?oauth_error=state_mismatch")
                exchange.assert_not_called()

    def test_callback_exchange_fails(self):
        with mock.patch.object(web, "_exchange_token", side_effect=urllib.error.URLError("down")):
            response = self.callback({"state": "state-1", "code": "c"})
        self.assertEqual(response.location, "/?oauth_error=token_exchange_failed")

    def test_callback_stores_tokens(self):
        token = {"access_token": "oauth-token", "refresh_token": "rt", "expires_in": 7200}
        with mock.patch.object(web, "_exchange_token", return_value=token) as exchange:
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
            web.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"access_token": "oauth-token"}'),
        ) as urlopen:
            token = web._exchange_token(grant_type="refresh_token", refresh_token="rt")
        self.assertEqual(token, {"access_token": "oauth-token"})
        url = urlopen.call_args.args[0]
        body = urllib.parse.parse_qs(urlopen.call_args.kwargs["data"].decode())
        self.assertEqual(url, web.YNAB_TOKEN_URL)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)
        self.assertEqual(
            body,
            {
                "client_id": ["client-id"],
                "client_secret": ["client-secret"],
                "redirect_uri": [web.REDIRECT_URI],
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
            with self.subTest(kind), mock.patch.object(web, "_exchange_token") as exchange:
                self.assertEqual(
                    web.get_token({"access_token": "t", "token_kind": kind, "expires_at": 0}), "t"
                )
                exchange.assert_not_called()

    def test_loads_the_saved_token(self):
        self.load_token.return_value = "saved-token"
        state = {}
        self.assertEqual(web.get_token(state), "saved-token")
        self.assertEqual(state, {"access_token": "saved-token", "token_kind": "saved"})
        self.assertIsNone(web.get_token({"skip_saved": True}))

    def test_not_connected(self):
        self.assertIsNone(web.get_token({}))

    def test_oauth_token_still_valid(self):
        state = {
            "access_token": "oauth-token",
            "token_kind": "oauth",
            "expires_at": time.time() + 3600,
        }
        with mock.patch.object(web, "_exchange_token") as exchange:
            self.assertEqual(web.get_token(state), "oauth-token")
        exchange.assert_not_called()

    def test_oauth_token_about_to_expire_is_refreshed(self):
        state = {
            "access_token": "old",
            "token_kind": "oauth",
            "refresh_token": "rt",
            "expires_at": time.time() + 30,
        }
        with mock.patch.object(
            web, "_exchange_token", return_value={"access_token": "new", "expires_in": 60}
        ) as exchange:
            self.assertEqual(web.get_token(state), "new")
        exchange.assert_called_once_with(grant_type="refresh_token", refresh_token="rt")
        self.assertEqual(state["refresh_token"], "rt")  # kept when YNAB doesn't send a new one

    def test_failed_refresh_disconnects(self):
        state = {
            "access_token": "old",
            "token_kind": "oauth",
            "refresh_token": "rt",
            "expires_at": 0,
        }
        with mock.patch.object(web, "_exchange_token", side_effect=urllib.error.URLError("down")):
            self.assertIsNone(web.get_token(state))
        self.assertEqual(state, {})

    def test_no_refresh_token(self):
        state = {"access_token": "old", "token_kind": "oauth", "expires_at": 0}
        self.assertIsNone(web.get_token(state))


# =============================================================================
# YNAB CALLS AND ERRORS
# =============================================================================


class YnabCallTest(WebTest):
    def plans(self, side_effect):
        with mock.patch.object(web, "get_plans", side_effect=side_effect) as get_plans:
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
        with mock.patch.object(web, "_exchange_token", return_value={"access_token": "new-token"}):
            response, get_plans = self.plans([unauthorized(), ["plan"]])
        self.assertEqual(response.get_json(), ["plan"])
        self.assertEqual(
            get_plans.call_args_list, [mock.call("test-token"), mock.call("new-token")]
        )

    def test_oauth_rejected_after_refresh(self):
        self.connected("oauth", refresh_token="rt", expires_at=time.time() + 3600)
        with mock.patch.object(web, "_exchange_token", return_value={"access_token": "new-token"}):
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
        self.assertEqual(web._error_detail(with_data), "from data")
        self.assertEqual(web._error_detail(with_body), "from body")
        self.assertEqual(web._error_detail(bad_body), "the reason")
        self.assertEqual(web._error_detail(nothing), "Unknown error")

    def test_accounts(self):
        self.connected()
        accounts = [
            {"id": ACCOUNT_ID, "name": "Test Wallet", "type": "checking", "balance": "₱0.00"}
        ]
        with mock.patch.object(web, "get_accounts", return_value=accounts) as get_accounts:
            response = self.client.get(f"/api/plans/{PLAN_ID}/accounts")
        self.assertEqual(response.get_json(), accounts)
        get_accounts.assert_called_once_with(PLAN_ID, "test-token")


# =============================================================================
# PARSE
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
            response.get_json()["error"], "Unknown source 'nope'. Known sources: gcash"
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
                mock.patch.object(web, "parse_statement", side_effect=error),
            ):
                response = self.post(self.pdf())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], message)

    def test_unexpected_parse_error_logs_only_its_type(self):
        error = RuntimeError("/internal/path/x.py")
        with (
            mock.patch.object(web, "parse_statement", side_effect=error),
            self.assertLogs(web.app.logger, level="WARNING") as logs,
        ):
            self.post(self.pdf())
        self.assertEqual(
            logs.output,
            [f"WARNING:{web.app.logger.name}:Could not read the uploaded PDF: RuntimeError"],
        )

    def test_parsed_statement(self):
        with mock.patch.object(web, "parse_statement", return_value=(ROWS, TRANSACTIONS)) as parse:
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


# =============================================================================
# BALANCE CHECK AND UPLOAD
# =============================================================================


def starting_check(**kwargs):
    return compare_balance(
        "starting", Decimal("500"), Decimal("1234.5"), Decimal("2033.5"), **kwargs
    )


class CheckJsonTest(unittest.TestCase):
    def test_money(self):
        self.assertIsNone(web._money(None))
        self.assertEqual(web._money(Decimal("1234.5")), "1,234.50")
        self.assertEqual(web._money(Decimal("-0.5")), "-0.50")

    def test_check_json(self):
        check = starting_check(already_in_ynab=1, to_upload=2)
        self.assertEqual(
            web._check_json(check),
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
                "message": web.balance_check_message(check),
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
        result = web._check_json(check)
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
        with mock.patch.object(web, "starting_balance_check", return_value=check) as start:
            response = self.post(TARGET)
        self.assertEqual(response.get_json(), web._check_json(check))
        start.assert_called_once_with(
            ROWS, TRANSACTIONS, ACCOUNT_ID, PLAN_ID, "test-token", source="gcash"
        )


class UploadTest(WebTest):
    def setUp(self):
        super().setUp()
        self.new_account = self.patch(web, "new_account_starting_balance", return_value=None)
        self.balance = self.patch(web, "get_account_balance", return_value=Decimal("500"))
        self.upload = self.patch(
            web,
            "upload_transactions",
            return_value=SimpleNamespace(
                transactions=[
                    ynab_txn(
                        var_date="2026-10-01",
                        payee_name="Received GCash from Test Sender",
                        category_name="Ready to Assign",
                        memo="GCash Ref: 1000000000001",
                        amount=1000000,
                    ),
                    ynab_txn(
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
        self.end = self.patch(web, "ending_balance_check", return_value=self.ending)

    def post(self, body=TARGET):
        return self.client.post("/api/upload", json=body)

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
                        "date": "2026-10-01",
                        "payee": "Received GCash from Test Sender",
                        "category": "Ready to Assign",
                        "memo": "GCash Ref: 1000000000001",
                        "amount": 1000000,
                        "flag": "",
                    },
                    {
                        "date": "2026-10-02",
                        "payee": "",
                        "category": "Uncategorized",
                        "memo": "",
                        "amount": 50000,
                        "flag": "red",
                    },
                ],
                "skipped": 1,
                "ynab_url": f"https://app.ynab.com/{PLAN_ID}/accounts/{ACCOUNT_ID}",
                "ending_check": web._check_json(self.ending),
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
        run.assert_called_once_with(host="127.0.0.1", port=web.PORT, debug=False)
        return stderr.getvalue()

    def test_binds_to_this_computer_only(self):
        self.patch_dict(os.environ, {"FLASK_SECRET_KEY": "test"})
        out = self.run_main()
        self.assertNotIn("FLASK_SECRET_KEY", out)
        self.assertIn("Enter your YNAB personal access token on the page to connect.", out)
        self.assertIn(f"Open http://localhost:{web.PORT}", out)
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
        self.assertIn(f"YNAB OAuth also available (redirect URI: {web.REDIRECT_URI}).", out)

    def test_python_dash_m(self):
        with mock.patch.object(web, "main") as main:
            runpy.run_module("statement_etl.web", run_name="__main__")
        main.assert_called_once()


if __name__ == "__main__":
    unittest.main()
