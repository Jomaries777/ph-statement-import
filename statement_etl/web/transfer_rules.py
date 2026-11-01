"""
ph-statement-import - the transfer rules page (Blueprint "transfer_rules").

    GET  /transfer-rules     - the page: your rules, each a pair of YNAB accounts
    POST /api/transfer-rules - save them

A rule (transfers.py) pairs two of your YNAB accounts with the text each one's
statement shows for money moved between them. The page lists the accounts of
a YNAB plan, so it needs this browser connected to YNAB, the same way as the
upload page (web/ynab_auth.py); in hosted mode it first tries auto-import's
saved YNAB connection (POST /api/auto-import/ynab-session).

Where rules are kept depends on the mode:

    Hosted (needs Google sign-in, so only with auto-import; app.py): the Google
        user's Firestore record, fields transfer_pairs and transfer_plan_id
        (autoimport/store.py).
    Local: local_settings.py, a JSON file on this computer, same two fields.

The manual upload reads them with saved_rules(), and the YNAB accounts mapped
on /auto-import with saved_own_accounts() (for the transfers that need no
rule, Source.own_transfers); auto-import (autoimport/runner.py) reads the
user's record itself.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from statement_etl import local_settings
from statement_etl.autoimport import runner, store
from statement_etl.transfers import TransferRule, clean_rules, rules_from_dicts
from statement_etl.web.auto_import import is_email_allowed, signed_in_user
from statement_etl.web.config import HOSTED
from statement_etl.web.limits import AUTO_IMPORT_LIMIT, limiter
from statement_etl.web.state import error_response
from statement_etl.web.ynab_auth import ynab_call
from statement_etl.ynab_api.accounts import get_accounts

blueprint = Blueprint("transfer_rules", __name__)
log = logging.getLogger(__name__)


def _hosted_user() -> tuple[dict | None, tuple | None]:
    """(the signed-in, invited Google user, None), or (None, an error response)."""
    user = signed_in_user()
    if not user:
        return None, error_response("Sign in with Google on the Auto-import page first.", 401)
    if not is_email_allowed(user.get("email", "")):
        return None, error_response("This Google account is not on the invite list.", 403)
    return user, None


def saved_rules() -> list[TransferRule]:
    """This browser's transfer rules, for an upload (web/upload.py).

    Hosted: the signed-in, invited Google user's; none when not signed in.
    Local: the settings file's; none when it can't be read (the upload goes
    on without transfers, and /transfer-rules shows what's wrong with it).
    """
    if HOSTED:
        user, error = _hosted_user()
        return [] if error else rules_from_dicts(user.get("transfer_pairs"))
    try:
        settings = local_settings.load_settings()
    except local_settings.LocalSettingsError as e:
        log.warning("Upload without transfer rules: %s", e)
        return []
    return rules_from_dicts(settings.get("transfer_pairs"))


def saved_own_accounts() -> dict[str, list[str]]:
    """The YNAB accounts this browser's user mapped on /auto-import, per
    source (runner.own_accounts()), for an upload's transfers that need no
    rule. Hosted and signed in only; {} otherwise, as nothing is mapped locally.
    """
    if not HOSTED:
        return {}
    user, error = _hosted_user()
    return {} if error else runner.own_accounts(user)


# =============================================================================
# ROUTES
# =============================================================================


@blueprint.get("/transfer-rules")
def page():
    """GET /transfer-rules -> 200 HTML (templates/transfer_rules.html)."""
    if HOSTED:
        user, error = _hosted_user()
        user = user or {}
        return render_template(
            "transfer_rules.html",
            hosted=True,
            signed_in=error is None,
            # auto-import's saved YNAB connection (POST /api/auto-import/ynab-session
            # needs Gmail connected too)
            has_saved_ynab=bool(user.get("ynab_refresh_token") and user.get("gmail_refresh_token")),
            plan_id=user.get("transfer_plan_id") or user.get("plan_id") or "",
            rules=user.get("transfer_pairs") or [],
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
        has_saved_ynab=False,
        settings_path=local_settings.settings_path(),
        settings_error=settings_error,
        plan_id=settings.get("transfer_plan_id", ""),
        rules=settings.get("transfer_pairs") or [],
    )


@blueprint.post("/api/transfer-rules")
@limiter.limit(AUTO_IMPORT_LIMIT)
def save():
    """Save the rules, checked against the plan's open YNAB accounts.

    POST /api/transfer-rules
         {"plan_id": "...",
          "rules": [{"account_1_id", "description_1", "account_2_id", "description_2"}]}
      -> 200 {"saved": true, "rules": 1}
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("Send the rules as JSON.", 400)
    user = None
    if HOSTED:
        user, error = _hosted_user()
        if error:
            return error

    plan_id = str(body.get("plan_id") or "")
    accounts = {}
    if body.get("rules"):  # an empty list clears the rules, with no need for YNAB
        if not plan_id:
            return error_response("Choose a YNAB plan.", 400)
        found, err = ynab_call(lambda token: get_accounts(plan_id, token))
        if err:
            return err
        accounts = {account["id"]: account["name"] for account in found}
    rules, message = clean_rules(body.get("rules"), accounts)
    if message:
        return error_response(message, 400)

    fields = {"transfer_plan_id": plan_id, "transfer_pairs": [rule.to_dict() for rule in rules]}
    if user:
        store.save_user(user["sub"], **fields)
    else:
        try:
            local_settings.save_settings(**fields)
        except (OSError, local_settings.LocalSettingsError) as e:
            return error_response(f"Could not save the settings file: {e}", 500)
    return jsonify({"saved": True, "rules": len(rules)})
