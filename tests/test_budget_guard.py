"""
Tests for the Cloud Run budget kill switch (ops/budget_guard.py), through
Flask's test client.

Run from the project folder:  python -m unittest discover tests

The Cloud Run metadata server and the Cloud Billing API are mocked; any
unmocked network call fails the test. All values here are made up.
"""

import base64
import io
import json
import os
import unittest
import urllib.error
from decimal import Decimal
from email.message import Message
from unittest import mock

import tests  # noqa: F401 - loads the safety net (no YNAB, no keychain)
from statement_etl.ops import budget_guard

PROJECT_ID = "example-project-test"
FAKE_TOKEN = "fake-metadata-token-do-not-log"


def pubsub_envelope(data: dict | str | bytes) -> dict:
    """Wrap `data` in a Pub/Sub push JSON envelope, base64-encoded as Pub/Sub sends it."""
    raw = (
        json.dumps(data).encode()
        if isinstance(data, dict)
        else (data.encode() if isinstance(data, str) else data)
    )
    return {
        "message": {"data": base64.b64encode(raw).decode("ascii"), "messageId": "1000000000001"},
        "subscription": f"projects/{PROJECT_ID}/subscriptions/budget-guard",
    }


class BudgetGuardTest(unittest.TestCase):
    """Test client for `budget_guard.app` with all network calls blocked by default."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "BUDGET_GUARD_DRY_RUN"}
        env["GCP_PROJECT_ID"] = PROJECT_ID
        env_patcher = mock.patch.dict(os.environ, env, clear=True)
        self.addCleanup(env_patcher.stop)
        env_patcher.start()

        url_patcher = mock.patch.object(
            budget_guard.urllib.request,
            "urlopen",
            side_effect=AssertionError("A test tried to reach the network."),
        )
        self.addCleanup(url_patcher.stop)
        self.urlopen = url_patcher.start()
        self.client = budget_guard.app.test_client()


class RoutesAndParsingTest(BudgetGuardTest):
    def test_only_post_root_is_exposed(self):
        routes = {
            (rule.rule, method)
            for rule in budget_guard.app.url_map.iter_rules()
            if rule.endpoint != "static"
            for method in rule.methods - {"HEAD", "OPTIONS"}
        }
        self.assertEqual(routes, {("/", "POST")})
        self.assertEqual(self.client.get("/").status_code, 405)

    def test_parses_decimal_amounts_without_float(self):
        envelope = pubsub_envelope(
            {
                "budgetDisplayName": "ph-statement-import",
                "alertThresholdExceeded": 0.5,
                "costAmount": 0.55,
                "budgetAmount": 1.00,
                "currencyCode": "USD",
            }
        )
        cost, budget = budget_guard.parse_budget_message(envelope)
        self.assertEqual((cost, budget), (Decimal("0.55"), Decimal("1.00")))
        self.assertIsInstance(cost, Decimal)
        self.assertIsInstance(budget, Decimal)


class UnderBudgetTest(BudgetGuardTest):
    def test_under_and_at_budget_return_204_without_calling_billing(self):
        for cost, budget in ((0, 1), (0.50, 1.00), (0.90, 1.00), (1.00, 1.00), (50, 50)):
            with self.subTest(cost=cost, budget=budget):
                payload = {"costAmount": cost, "budgetAmount": budget}
                response = self.client.post("/", json=pubsub_envelope(payload))
                self.assertEqual((response.status_code, response.data), (204, b""))
        self.urlopen.assert_not_called()


class OverBudgetTest(BudgetGuardTest):
    def test_over_budget_unlinks_billing_and_logs_one_line(self):
        self.urlopen.side_effect = [
            io.BytesIO(json.dumps({"access_token": FAKE_TOKEN}).encode()),
            io.BytesIO(json.dumps({"billingAccountName": "", "billingEnabled": False}).encode()),
        ]
        with self.assertLogs(budget_guard.app.logger, level="WARNING") as logs:
            response = self.client.post(
                "/", json=pubsub_envelope({"costAmount": 1.25, "budgetAmount": 1.00})
            )

        self.assertEqual((response.status_code, response.data), (204, b""))
        self.assertEqual(self.urlopen.call_count, 2)

        token_req = self.urlopen.call_args_list[0].args[0]
        self.assertEqual(token_req.full_url, budget_guard.METADATA_TOKEN_URL)
        self.assertEqual(token_req.get_method(), "GET")
        self.assertEqual(token_req.get_header("Metadata-flavor"), "Google")

        billing_req = self.urlopen.call_args_list[1].args[0]
        self.assertEqual(
            billing_req.full_url,
            f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT_ID}/billingInfo",
        )
        self.assertEqual(billing_req.get_method(), "PUT")
        self.assertEqual(billing_req.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")
        self.assertEqual(billing_req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(billing_req.data.decode()), {"billingAccountName": ""})

        self.assertEqual(len(logs.output), 1)
        self.assertIn("disabled billing", logs.output[0])
        self.assertIn(PROJECT_ID, logs.output[0])
        self.assertNotIn(FAKE_TOKEN, logs.output[0])

    def test_dry_run_logs_would_disable_billing_and_never_calls_api(self):
        os.environ["BUDGET_GUARD_DRY_RUN"] = "1"
        with self.assertLogs(budget_guard.app.logger, level="WARNING") as logs:
            response = self.client.post(
                "/", json=pubsub_envelope({"costAmount": 2, "budgetAmount": 1})
            )

        self.assertEqual((response.status_code, response.data), (204, b""))
        self.urlopen.assert_not_called()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("would disable billing", logs.output[0])
        self.assertIn(PROJECT_ID, logs.output[0])

    def test_missing_project_id_when_over_budget_returns_500(self):
        os.environ.pop("GCP_PROJECT_ID", None)
        with self.assertLogs(budget_guard.app.logger, level="ERROR"):
            response = self.client.post(
                "/", json=pubsub_envelope({"costAmount": 2, "budgetAmount": 1})
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "GCP_PROJECT_ID is not set."})
        self.urlopen.assert_not_called()

    def test_metadata_response_without_token_raises(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = io.BytesIO(b"{}")
        with self.assertRaises(RuntimeError):
            budget_guard.metadata_access_token()

    def test_billing_api_http_error_returns_502_without_logging_token(self):
        self.urlopen.side_effect = [
            io.BytesIO(json.dumps({"access_token": FAKE_TOKEN}).encode()),
            urllib.error.HTTPError(
                url=f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT_ID}/billingInfo",
                code=403,
                msg="Forbidden",
                hdrs=Message(),
                fp=io.BytesIO(b'{"error": "permission denied"}'),
            ),
        ]
        with self.assertLogs(budget_guard.app.logger, level="ERROR") as logs:
            response = self.client.post(
                "/", json=pubsub_envelope({"costAmount": 2, "budgetAmount": 1})
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"error": "Cloud Billing API returned HTTP 403."})
        self.assertIn("HTTP 403", logs.output[0])
        self.assertNotIn(FAKE_TOKEN, logs.output[0])

    def test_metadata_network_error_returns_502(self):
        self.urlopen.side_effect = urllib.error.URLError("metadata unreachable")
        with self.assertLogs(budget_guard.app.logger, level="ERROR") as logs:
            response = self.client.post(
                "/", json=pubsub_envelope({"costAmount": 2, "budgetAmount": 1})
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("URLError", logs.output[0])


class BadMessageTest(BudgetGuardTest):
    def test_malformed_envelopes_return_400(self):
        bad_bodies = [
            None,
            [],
            "not-an-object",
            {},
            {"message": "not-a-dict"},
            {"message": {}},
            {"message": {"data": ""}},
            {"message": {"data": "   "}},
            {"message": {"data": 123}},
            {"message": {"data": "!!!not-base64!!!"}},
            {"message": {"data": base64.b64encode(b"\xff\xfe").decode("ascii")}},
            pubsub_envelope("not-json"),
            pubsub_envelope("[1, 2, 3]"),
            pubsub_envelope({}),
            pubsub_envelope({"costAmount": 2}),
            pubsub_envelope({"budgetAmount": 1}),
            pubsub_envelope({"costAmount": "2", "budgetAmount": 1}),
            pubsub_envelope({"costAmount": 2, "budgetAmount": "1"}),
            pubsub_envelope({"costAmount": True, "budgetAmount": 1}),
            pubsub_envelope({"costAmount": 2, "budgetAmount": False}),
            pubsub_envelope({"costAmount": None, "budgetAmount": 1}),
            pubsub_envelope('{"costAmount": NaN, "budgetAmount": 1}'),
            pubsub_envelope('{"costAmount": Infinity, "budgetAmount": 1}'),
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                if body is None:
                    response = self.client.post("/", data=b"{not-json", content_type="text/plain")
                else:
                    response = self.client.post("/", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
