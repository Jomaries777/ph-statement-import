"""
ph-statement-import - the transfer rules page (Blueprint "transfer_rules").

    GET  /transfer-rules     - the page: your wallets and your rules
    POST /api/transfer-rules - save them

A rule (transfers.py) says which statement rows are money moved to another of
your wallets. Where rules and wallets are kept depends on the mode:

    Hosted (needs Google sign-in, so only with auto-import; app.py):
        rules   - the Google user's Firestore record, field transfer_rules
                  (autoimport/store.py)
        wallets - the user's auto-import mappings: "gcash", "gcash-2", ...,
                  each with its YNAB account. Edited on /auto-import, not here.
    Local:
        rules and wallets - local_settings.py, a JSON file on this computer.
                  A wallet is a YNAB account you import one source into, picked
                  on this page; its id is the account's id.

Both modes hand the page the same wallet dicts:
    {"id", "name", "source", "account_id", "account_name"}
"""

from flask import Blueprint, jsonify, render_template, request

from statement_etl import local_settings
from statement_etl.autoimport import runner, store
from statement_etl.sources import SOURCES
from statement_etl.transfers import clean_rules
from statement_etl.web.auto_import import is_email_allowed, signed_in_user
from statement_etl.web.config import HOSTED
from statement_etl.web.limits import AUTO_IMPORT_LIMIT, limiter
from statement_etl.web.state import error_response

blueprint = Blueprint("transfer_rules", __name__)

MAX_LOCAL_WALLETS = 20


# =============================================================================
# WALLETS AND RULES, PER MODE
# =============================================================================


def hosted_wallets(user: dict) -> list[dict]:
    """The signed-in user's auto-import wallets that have a YNAB account."""
    wallets = []
    for wallet_id, mapping in (user.get("mappings") or {}).items():
        source = SOURCES.get(runner.wallet_source(wallet_id, mapping))
        if not source or not mapping.get("account_id"):
            continue
        account = runner.wallet_account(user, source, wallet_id)
        wallets.append(
            {
                "id": wallet_id,
                "name": source.wallet_name(account),
                "source": source.key,
                "account_id": mapping["account_id"],
                "account_name": mapping.get("account_name", ""),
            }
        )
    return wallets


def local_wallets(settings: dict) -> list[dict]:
    """The wallets saved in the local settings file."""
    wallets = []
    for saved in settings.get("wallets") or []:
        source = SOURCES.get(saved.get("source"))
        if not source:
            continue
        wallets.append(
            {
                "id": saved["id"],
                "name": source.wallet_name(),
                "source": source.key,
                "account_id": saved["id"],
                "account_name": saved.get("account_name", ""),
            }
        )
    return wallets


def _hosted_user() -> tuple[dict | None, tuple | None]:
    """(the signed-in, invited Google user, None), or (None, an error response)."""
    user = signed_in_user()
    if not user:
        return None, error_response("Sign in with Google on the Auto-import page first.", 401)
    if not is_email_allowed(user.get("email", "")):
        return None, error_response("This Google account is not on the invite list.", 403)
    return user, None


def _clean_local_wallets(raw: object) -> tuple[list[dict] | None, str | None]:
    """Check the wallets the page sends in local mode: (wallets to save, None) or
    (None, a message). Each is {"id": YNAB account id, "source", "account_name"}."""
    if not isinstance(raw, list):
        return None, "Send the banks as a list."
    if len(raw) > MAX_LOCAL_WALLETS:
        return None, f"Save at most {MAX_LOCAL_WALLETS} banks."
    wallets = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return None, f"Bank {number} is not valid."
        account_id = str(item.get("id") or "").strip()
        source = str(item.get("source") or "")
        if source not in SOURCES:
            return None, f"Bank {number}: choose which bank."
        if not account_id or len(account_id) > 64:
            return None, f"Bank {number}: choose a YNAB account."
        if any(wallet["id"] == account_id for wallet in wallets):
            return None, "Each YNAB account can be only one bank."
        account_name = str(item.get("account_name") or "")[:200]
        wallets.append({"id": account_id, "source": source, "account_name": account_name})
    return wallets, None


# =============================================================================
# ROUTES
# =============================================================================


@blueprint.get("/transfer-rules")
def page():
    """GET /transfer-rules -> 200 HTML (templates/transfer_rules.html)."""
    if HOSTED:
        user, error = _hosted_user()
        return render_template(
            "transfer_rules.html",
            hosted=True,
            signed_in=error is None,
            wallets=hosted_wallets(user) if user else [],
            rules=(user or {}).get("transfer_rules") or [],
            sources=list(SOURCES.values()),
        )

    try:
        settings = local_settings.load_settings()
        settings_error = ""
    except local_settings.LocalSettingsError as e:
        settings, settings_error = {}, str(e)
    return render_template(
        "transfer_rules.html",
        hosted=False,
        signed_in=False,
        settings_path=local_settings.settings_path(),
        settings_error=settings_error,
        plan_id=settings.get("plan_id", ""),
        plan_name=settings.get("plan_name", ""),
        wallets=local_wallets(settings),
        rules=settings.get("transfer_rules") or [],
        sources=list(SOURCES.values()),
    )


@blueprint.post("/api/transfer-rules")
@limiter.limit(AUTO_IMPORT_LIMIT)
def save():
    """Save the rules (and, in local mode, the wallets and their plan).

    POST /api/transfer-rules
         {"rules": [{"description", "from_wallet", "to_wallet"}],
          local mode only: "plan_id", "plan_name", "wallets": [{"id", "source", "account_name"}]}
      -> 200 {"saved": true, "rules": 1}
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("Send the rules as JSON.", 400)

    if HOSTED:
        user, error = _hosted_user()
        if error:
            return error
        wallet_ids = {wallet["id"] for wallet in hosted_wallets(user)}
        rules, message = clean_rules(body.get("rules"), wallet_ids)
        if message:
            return error_response(message, 400)
        store.save_user(user["sub"], transfer_rules=[rule.to_dict() for rule in rules])
        return jsonify({"saved": True, "rules": len(rules)})

    wallets, message = _clean_local_wallets(body.get("wallets"))
    if message:
        return error_response(message, 400)
    rules, message = clean_rules(body.get("rules"), {wallet["id"] for wallet in wallets})
    if message:
        return error_response(message, 400)
    try:
        local_settings.save_settings(
            plan_id=str(body.get("plan_id") or ""),
            plan_name=str(body.get("plan_name") or ""),
            wallets=wallets,
            transfer_rules=[rule.to_dict() for rule in rules],
        )
    except (OSError, local_settings.LocalSettingsError) as e:
        return error_response(f"Could not save the settings file: {e}", 500)
    return jsonify({"saved": True, "rules": len(rules)})
