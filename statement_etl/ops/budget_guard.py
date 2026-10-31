"""
ph-statement-import - Cloud Run billing kill switch for Google Cloud budget alerts.

Runs as a private Cloud Run service (`budget-guard`) from the same container
image as the web app, under its own service account. Google Cloud Billing
Budgets publishes spend notifications several times a day to a Pub/Sub topic,
and a push subscription posts each message to `POST /`:

    1. Decode the base64 JSON budget notification from the Pub/Sub envelope.
    2. If `costAmount <= budgetAmount`, return 204 and do nothing.
    3. Otherwise unlink billing from the project through the Cloud Billing API
       (`PUT projects/<GCP_PROJECT_ID>/billingInfo` with
       `{"billingAccountName": ""}`) and log one line.
    4. When `BUDGET_GUARD_DRY_RUN=1`, log what it would do instead of calling
       the Cloud Billing API.

Uses `urllib` and the Cloud Run metadata server for its access token, so the
container image needs no extra dependencies.

Environment:
    GCP_PROJECT_ID        - Google Cloud project ID whose billing is unlinked
                            when `costAmount > budgetAmount`
    BUDGET_GUARD_DRY_RUN  - set to `1` to log `"would disable billing"` without
                            calling the Cloud Billing API

Usage (Cloud Run / Gunicorn):
    gunicorn -w 1 -b 0.0.0.0:8080 statement_etl.ops.budget_guard:app
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from flask import Flask, Response, jsonify, request

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
BILLING_API_BASE = "https://cloudbilling.googleapis.com/v1"

app = Flask(__name__)


# =============================================================================
# PUB/SUB BUDGET MESSAGE
# =============================================================================


def _amount(payload: dict, key: str) -> Decimal:
    """Read a finite numeric budget field as `Decimal` (never `float`)."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ValueError(f"{key} must be a number.")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError(f"{key} must be finite.")
    return amount


def parse_budget_message(envelope: object) -> tuple[Decimal, Decimal]:
    """Decode `(cost_amount, budget_amount)` from a Pub/Sub push JSON body.

    parse_budget_message(
        {"message": {"data": "eyJjb3N0QW1vdW50IjogMiwgImJ1ZGdldEFtb3VudCI6IDF9"}}
    ) -> (Decimal("2"), Decimal("1"))
    """
    if not isinstance(envelope, dict):
        raise ValueError("Expected a JSON object.")
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise ValueError("Missing Pub/Sub message.")
    encoded = message.get("data")
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError("Missing Pub/Sub message data.")

    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
        payload = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (ValueError, ArithmeticError):
        raise ValueError("Invalid Pub/Sub message data.") from None

    if not isinstance(payload, dict):
        raise ValueError("Budget notification must be a JSON object.")
    return _amount(payload, "costAmount"), _amount(payload, "budgetAmount")


# =============================================================================
# CLOUD BILLING API - unlink billing via the Cloud Run metadata server token
# =============================================================================


def metadata_access_token() -> str:
    """Fetch a short-lived OAuth2 access token from the Cloud Run metadata server.

    metadata_access_token() -> "ya29.c...."
    """
    # Cloud Run requires Metadata-Flavor: Google to block unintended metadata reads
    req = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Metadata server response did not include an access_token.")
    return token


def disable_billing(project_id: str) -> dict:
    """Unlink billing from `project_id` via `PUT projects/<id>/billingInfo`.

    disable_billing("example-project")
    -> {"name": "projects/example-project/billingInfo", "billingAccountName": "", ...}
    """
    token = metadata_access_token()
    project = urllib.parse.quote(project_id, safe="")
    url = f"{BILLING_API_BASE}/projects/{project}/billingInfo"
    body = json.dumps({"billingAccountName": ""}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# =============================================================================
# ROUTE - Pub/Sub push endpoint
# =============================================================================


@app.post("/")
def handle_budget_alert() -> tuple[Response | str, int]:
    """Handle one Cloud Billing Budget notification pushed by Pub/Sub."""
    # 1. Decode the Pub/Sub push message
    try:
        cost, budget = parse_budget_message(request.get_json(silent=True))
    except ValueError as e:
        # The reason goes to the log; the caller gets a fixed message, never exception text
        app.logger.warning("Ignored a message that isn't a budget notification: %s", e)
        return jsonify({"error": "Not a budget notification."}), 400

    # 2. Budgets publish several times a day even at $0; under or at budget does nothing
    if cost <= budget:
        return "", 204

    project_id = os.environ.get("GCP_PROJECT_ID", "").strip()
    if not project_id:
        app.logger.error("Budget exceeded (%s > %s), but GCP_PROJECT_ID is not set.", cost, budget)
        return jsonify({"error": "GCP_PROJECT_ID is not set."}), 500

    # 3. Dry run: verify the Pub/Sub -> Cloud Run path without unlinking billing
    if os.environ.get("BUDGET_GUARD_DRY_RUN") == "1":
        app.logger.warning(
            "Budget exceeded (%s > %s): would disable billing for project %s (dry run).",
            cost,
            budget,
            project_id,
        )
        return "", 204

    # 4. Over budget: unlink billing from the project and log one line
    try:
        disable_billing(project_id)
    except urllib.error.HTTPError as e:
        app.logger.error(
            "Budget exceeded (%s > %s): failed to disable billing for project %s (HTTP %s).",
            cost,
            budget,
            project_id,
            e.code,
        )
        return jsonify({"error": f"Cloud Billing API returned HTTP {e.code}."}), 502
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        app.logger.error(
            "Budget exceeded (%s > %s): failed to disable billing for project %s (%s).",
            cost,
            budget,
            project_id,
            type(e).__name__,
        )
        return jsonify({"error": "Failed to disable billing."}), 502

    app.logger.warning(
        "Budget exceeded (%s > %s): disabled billing for project %s.",
        cost,
        budget,
        project_id,
    )
    return "", 204
