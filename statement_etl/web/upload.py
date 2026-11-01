"""
ph-statement-import - the page and the statement upload steps (Blueprint "upload").

Routes, in the order the page uses them:
    GET  /                              - the page (templates/index.html)
    GET  /api/status                    - restore the page after a reload or the OAuth redirect
    POST /api/parse                     - ① parse the statement PDF (in memory only)
    POST /api/statement-account         - ① a statement that holds several accounts
                                          (GoTyme GoalSave): show and upload this one
    GET  /api/plans                     - ③ the Plan dropdown
    GET  /api/plans/<plan_id>/accounts  - ③ the Account dropdown
    POST /api/starting-balance          - ④ check before the upload
    POST /api/upload                    - ④ upload, then the ending balance check
    POST /api/categorize                - ④ hosted, opt-in: Gemini categorizes what
                                          the upload created (categorize.py)
    GET  /api/progress                  - the step ① or ④ is on, asked while it runs

While /api/parse or /api/upload runs, pipeline's on_step callback keeps the
step it's on in this browser's state["progress"] (in memory, like the
statement itself), and the page asks GET /api/progress to show it. gunicorn
and the local server both answer requests in threads, so the question is
answered while the parse or upload is still going.

A statement that holds several accounts (Source.account_column) is uploaded
one account at a time, each into its own YNAB account: /api/parse keeps
every account's rows in state["accounts"] and shows the first;
/api/statement-account switches to another. state["uploaded_accounts"]
lists the ones already uploaded, so the page names only the others.

Step ② (connecting to YNAB) is in ynab_auth.py. Parsing, the starting balance
check and the upload reuse pipeline.parse_statement(), starting_balance_check()
and import_statement(), the same functions as the command line.
"""

import io
import os
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from statement_etl.categorize import MIN_CONFIDENCE, categorize_transactions, category_label
from statement_etl.gemini import GeminiError
from statement_etl.parsers.common import StatementError
from statement_etl.pipeline import (
    import_statement,
    parse_statement,
    split_accounts,
    starting_balance_check,
)
from statement_etl.sources import DEFAULT_SOURCE, SOURCES, get_source
from statement_etl.transfers import find_transfers, unlinked_message
from statement_etl.web import token_store, transfer_rules
from statement_etl.web.config import CATEGORIZE_AVAILABLE, HOSTED, TRANSFER_RULES
from statement_etl.web.limits import CATEGORIZE_LIMIT, PARSE_LIMIT, limiter
from statement_etl.web.state import error_response, session_state
from statement_etl.web.ynab_auth import api_error_message, get_token, ynab_call
from statement_etl.web.ynab_oauth import oauth_configured
from statement_etl.ynab_api.accounts import get_accounts, get_plans, get_user
from statement_etl.ynab_api.balance import (
    balance_check_message,
    new_account_message,
)
from statement_etl.ynab_api.categories import uncategorized_created

blueprint = Blueprint("upload", __name__)

# /api/categorize must answer within Cloud Run's 60 seconds; Gemini gets this
# long, and reading the categories and saving the picks the rest
CATEGORIZE_SECONDS = 40


@blueprint.get("/")
def index():
    return render_template(
        "index.html",
        sources=SOURCES.values(),
        default_source=DEFAULT_SOURCE,
        # hosted mode: no personal access token field, only "Connect with YNAB login"
        token_allowed=not HOSTED,
        min_confidence=MIN_CONFIDENCE,
    )


@blueprint.get("/api/status")
def api_status():
    """Is YNAB connected, and is a parsed statement waiting? Restores the page
    after a reload or the OAuth redirect."""
    state = session_state()
    connected, connect_error = False, None
    get_token(state)  # loads the saved token, if there is one
    if state.get("access_token"):
        _, err = ynab_call(get_user)  # confirms the token still works
        connected = err is None
        if err:
            connect_error = err[0].get_json()["error"]
    return jsonify(
        {
            "connected": connected,
            "connect_error": connect_error,
            "token_kind": state.get("token_kind") if connected else None,
            "oauth_configured": oauth_configured(),
            # personal access tokens: local mode only (hosted mode is OAuth only)
            "token_allowed": not HOSTED,
            # show "Remember this token on this computer" only with a secure store,
            # and never in hosted mode (the server's store would be shared)
            "remember_available": not HOSTED and token_store.available(),
            "store_name": None if HOSTED else token_store.store_name(),
            # show "Categorize new transactions with Gemini" (hosted, with a Gemini key)
            "categorize_available": CATEGORIZE_AVAILABLE,
            "parsed": state.get("parsed"),
        }
    )


def _progress_reporter(state):
    """An on_step callback (pipeline.py) keeping the step in state["progress"]."""

    def report(step, detail):
        state["progress"] = {"step": step, "detail": detail}

    return report


@blueprint.get("/api/progress")
def api_progress():
    """The step /api/parse or /api/upload is on, while it runs.

    GET /api/progress -> {"step": "read", "detail": {"page": 3, "pages": 5}}
    -> null when neither is running
    """
    return jsonify(session_state().get("progress"))


@blueprint.post("/api/parse")
@limiter.limit(PARSE_LIMIT)
def api_parse():
    """Parse the uploaded PDF (kept in memory only) and keep the transactions for upload."""
    upload = request.files.get("pdf")
    password = request.form.get("password", "")
    source = request.form.get("source") or DEFAULT_SOURCE
    if source not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        return error_response(f"Unknown source {source!r}. Known sources: {known}", 400)
    if not upload or not upload.filename:
        return error_response("Choose a PDF file.", 400)

    state = session_state()
    try:
        rows, transactions = parse_statement(
            io.BytesIO(upload.read()), password, source, on_step=_progress_reporter(state)
        )
    except StatementError as e:
        # Written for the user (wrong password, totals off), so shown as it is
        return error_response(str(e), 400)
    except Exception as e:  # noqa: BLE001 - e.g. not a PDF at all
        # Any other error can carry internal details, so the page gets a fixed message
        # and the log gets only the error type (never the PDF or its contents).
        current_app.logger.warning("Could not read the uploaded PDF: %s", type(e).__name__)
        return error_response("Could not read the PDF. Is it the statement PDF?", 400)
    finally:
        state.pop("progress", None)

    state["source"] = source
    state["filename"] = upload.filename
    # Each account's rows, for a statement that holds several; {"": all} otherwise
    state["accounts"] = split_accounts(rows, transactions, source)
    state["uploaded_accounts"] = []  # a new statement: none of its accounts uploaded yet
    return jsonify(_show_account(state, next(iter(state["accounts"]))))


@blueprint.post("/api/statement-account")
def api_statement_account():
    """Show and upload another account of the parsed statement, for a
    statement that holds several (GoTyme GoalSave).

    POST /api/statement-account {"account": "22222"} -> what /api/parse returns,
    for that account's rows
    """
    state = session_state()
    account = (request.get_json(silent=True) or {}).get("account")
    if not state.get("accounts"):
        return error_response("Parse a statement first.", 400)
    if account not in state["accounts"]:
        return error_response(f"This statement has no account {account!r}.", 400)
    return jsonify(_show_account(state, account))


def _show_account(state, account):
    """Make `account` of the parsed statement the one the page shows and
    uploads (state["rows"], state["transactions"]); returns its parsed JSON."""
    src = get_source(state["source"])
    rows, transactions = state["accounts"][account]
    # Statement rows vs. balance-correction rows added by the parser
    real_rows = [r for r in rows if not src.is_correction_row(r)]
    balances = src.statement_balances(rows)
    parsed = {
        "source": src.key,
        "source_name": src.name,
        "filename": state["filename"],
        # the statement's own columns; the page styles them from the lists below
        "columns": list(src.columns),
        "rows": [[row[c] for c in src.columns] for row in rows],
        "amount_columns": list(src.amount_columns),
        "money_in_columns": list(src.money_in_columns),
        "money_out_columns": list(src.money_out_columns),
        "text_column": src.text_column,
        # indexes in "rows" of the balance-correction rows (highlighted on the page)
        "correction_rows": [i for i, r in enumerate(rows) if src.is_correction_row(r)],
        # one line under the table, e.g. the statement's totals
        "summary": src.summary(rows),
        "statement_count": len(real_rows),
        "count": len(transactions),  # what will be uploaded, corrections included
        # running-balance check; empty / None when the statement adds up
        "corrections": [
            {**c, "amount": f"{c['amount']:+,.2f}"} for c in src.balance_corrections(rows)
        ],
        "balance_warning": src.balance_warning(rows),
        # compared with the YNAB account balance before / after the upload
        "starting_balance": f"{balances['starting']:,.2f}",
        "ending_balance": f"{balances['ending']:,.2f}",
        # a statement that holds several accounts: the page's picker
        # (Source.account_column), e.g. [{"account": "22222", "count": 5}]
        "account_label": src.account_column,
        "account": account,
        "accounts": [
            {"account": key, "count": len(account_rows)}
            for key, (account_rows, _) in state["accounts"].items()
        ]
        if src.account_column
        else [],
        # rows that are always transfers (Source.own_transfers): the page asks
        # which YNAB account they go to, prefilled from /auto-import if mapped
        "own_transfers": _own_transfers_json(src),
    }
    state["account"] = account
    state["rows"] = rows
    state["transactions"] = transactions
    state["parsed"] = parsed
    return parsed


def _own_transfers_json(src) -> dict | None:
    """The "Transfers to/from Deposit account go to" select for a GoalSave
    upload: {"description": "Deposit account", "other_name": "GoTyme",
    "account_id": the Everyday account mapped on /auto-import, or ""}."""
    own = src.own_transfers
    if not own:
        return None
    mapped = transfer_rules.saved_own_accounts().get(own.other_source) if TRANSFER_RULES else None
    return {
        "description": own.description,
        "other_name": SOURCES[own.other_source].name,
        "account_id": mapped[0] if mapped else "",
    }


@blueprint.get("/api/plans")
def api_plans():
    plans, err = ynab_call(get_plans)
    return err or jsonify(plans)


@blueprint.get("/api/plans/<plan_id>/accounts")
def api_accounts(plan_id):
    accounts, err = ynab_call(lambda token: get_accounts(plan_id, token))
    return err or jsonify(accounts)


def _check_json(check):
    """A balance check (from ynab_api.balance / ynab_api.client) as JSON for the page."""
    return {
        "kind": check["kind"],
        "matches": check["matches"],
        "new_account": check["new_account"] is not None,
        "statement": f"{check['statement']:,.2f}",
        "ynab_before": _money(check["ynab_before"]),
        "ynab_after": _money(check["ynab_after"]),
        "difference": f"{check['difference']:+,.2f}",
        # starting check: transactions YNAB already has (skipped on upload)
        "already_in_ynab": check.get("already_in_ynab") or 0,
        "to_upload": check.get("to_upload"),
        "message": balance_check_message(check),
    }


def _money(amount):
    """Decimal -> "1,234.56"; None stays None."""
    return None if amount is None else f"{amount:,.2f}"


def _transfers(state, account_id, body) -> tuple[dict[int, str | tuple], dict[str, str]]:
    """The parsed rows that are transfers, for an upload into `account_id`
    (transfers.find_transfers()): ({row index: the other YNAB account id},
    {account id: its name}).

    Rows a saved transfer rule matches: none without rules, and in hosted
    mode without auto-import's Google sign-in. Then the moves that need no
    rule (Source.own_transfers): GoalSave's "Deposit account" rows go to the
    account the page's "Transfers to/from" select names (body's
    "own_transfer_account_id"); Everyday's "GoalSave" rows link to a
    transfer from any GoalSave account mapped on /auto-import, or, with none
    mapped, from any account.
    """
    source = get_source(state["source"])
    rules = transfer_rules.saved_rules() if TRANSFER_RULES else []
    own_accounts = transfer_rules.saved_own_accounts() if TRANSFER_RULES else {}
    picked = body.get("own_transfer_account_id")
    if source.own_transfers and isinstance(picked, str) and picked:
        own_accounts[source.own_transfers.other_source] = [picked]
    transfers = find_transfers(state["rows"], source, account_id, rules, own_accounts)
    names = {}
    for rule in rules:
        names[rule.account_1_id] = rule.account_1_name
        names[rule.account_2_id] = rule.account_2_name
    return transfers, names


def _transfer_label(source, to, names) -> str:
    """What a transfer row's "→ …" mark on the page says: the rule's account
    name, or for a move that needs no rule, the other source's name
    ("GoTyme" on a GoalSave row, "GoTyme GoalSave" on an Everyday one)."""
    if isinstance(to, str) and to in names:
        return names[to]
    if source.own_transfers:
        return SOURCES[source.own_transfers.other_source].name
    return next(
        s.name
        for s in SOURCES.values()
        if s.own_transfers and s.own_transfers.other_source == source.key
    )


@blueprint.post("/api/starting-balance")
def api_starting_balance():
    """Before the upload: YNAB account balance vs. the statement's starting balance.

    A mismatch is returned as a warning; the page still allows the upload.
    """
    state = session_state()
    rows, transactions = state.get("rows"), state.get("transactions")
    body = request.get_json(silent=True) or {}
    plan_id, account_id = body.get("plan_id"), body.get("account_id")
    if not rows:
        return error_response("Parse a statement first.", 400)
    if not plan_id or not account_id:
        return error_response("Choose a plan and an account.", 400)

    transfers, account_names = _transfers(state, account_id, body)
    source = get_source(state["source"])
    check, err = ynab_call(
        lambda token: starting_balance_check(
            rows,
            transactions,
            account_id,
            plan_id,
            token,
            source=state["source"],
            transfers=transfers,
        )
    )
    if err:
        return err
    unlinked = check.get("unlinked_transfers") or []
    return jsonify(
        {
            **_check_json(check),
            # rows a transfer rule matched, so the page can mark them; not
            # the ones that found no transfer to link to (they go as ordinary rows)
            "transfers": [
                {"row": index, "to": _transfer_label(source, to, account_names)}
                for index, to in sorted(transfers.items())
                if index not in unlinked
            ],
            "unlinked_message": unlinked_message(source, len(unlinked)) if unlinked else None,
        }
    )


@blueprint.post("/api/upload")
def api_upload():
    """Send the parsed transactions to the chosen plan / account.

    Before: a newly created account gets its Starting Balance set to the
    statement's. After: the YNAB balance is compared with the statement's
    ending balance (a warning only). In hosted mode the statement is then
    dropped from the server: it's in YNAB now.

    With "categorize": true (the page's opt-in, hosted mode), what it created
    with no category is kept for POST /api/categorize; "to_categorize" says how many.
    """
    state = session_state()
    transactions = state.get("transactions")
    body = request.get_json(silent=True) or {}
    plan_id, account_id = body.get("plan_id"), body.get("account_id")
    if not transactions:
        return error_response("Parse a statement first.", 400)
    if not plan_id or not account_id:
        return error_response("Choose a plan and an account.", 400)

    rows = state["rows"]
    source = get_source(state["source"])  # read before the hosted clean-up below
    transfers, _ = _transfers(state, account_id, body)

    # New account's Starting Balance, upload, ending balance check. After a
    # token refresh ynab_call() runs it again, which is safe: YNAB skips
    # import_ids it already has, and the Starting Balance already matches.
    try:
        imported, err = ynab_call(
            lambda token: import_statement(
                rows,
                transactions,
                account_id,
                plan_id,
                token,
                source=state["source"],
                on_step=_progress_reporter(state),
                transfers=transfers,
            )
        )
    finally:
        state.pop("progress", None)
    if err:
        return err
    unlinked = getattr(imported, "unlinked_transfers", [])
    # The statement's other accounts, still to upload (GoTyme GoalSave)
    uploaded = state.setdefault("uploaded_accounts", [])
    if state.get("account") not in uploaded:
        uploaded.append(state.get("account"))
    other_accounts = [a for a in state.get("accounts", {}) if a not in uploaded]
    if HOSTED:
        # This account is in YNAB now: the server keeps only the others
        state.get("accounts", {}).pop(state.get("account"), None)
        for key in ("rows", "transactions", "parsed", "account"):
            state.pop(key, None)
        if not other_accounts:
            for key in ("source", "filename", "accounts", "uploaded_accounts"):
                state.pop(key, None)

    # Only this upload's transactions, never anything else in the plan
    state.pop("to_categorize", None)
    to_categorize = []
    if CATEGORIZE_AVAILABLE and body.get("categorize") is True:
        to_categorize = uncategorized_created(imported.created)
        if to_categorize:
            state["to_categorize"] = {"plan_id": plan_id, "transactions": to_categorize}

    created = [
        {
            "id": t.id,  # so the page can show the categories /api/categorize picks
            "date": str(t.var_date),
            "payee": t.payee_name or "",
            "category": t.category_name or "Uncategorized",
            "memo": t.memo or "",
            "amount": t.amount,  # milliunits; the page formats it
            # "red" on balance-correction rows, so the page can highlight them
            "flag": t.flag_color.value if t.flag_color else "",
        }
        for t in imported.created
    ]

    # The upload is already done, so a failed ending check is reported, not an error
    ending_check = _check_json(imported.ending_check) if imported.ending_check else None
    ending_error = api_error_message(imported.ending_error)[0] if imported.ending_error else None

    return jsonify(
        {
            "created": created,
            "skipped": imported.skipped,
            "transfers": imported.transfers,
            "unlinked_message": unlinked_message(source, len(unlinked)) if unlinked else None,
            "to_categorize": len(to_categorize),
            "ynab_url": f"https://app.ynab.com/{plan_id}/accounts/{account_id}",
            "other_accounts": other_accounts,
            "ending_check": ending_check,
            "ending_check_error": ending_error,
            # set when a new account's Starting Balance was set (or already matched)
            "new_account_message": (
                new_account_message(imported.new_account) if imported.new_account else None
            ),
        }
    )


@blueprint.post("/api/categorize")
@limiter.limit(CATEGORIZE_LIMIT)
def api_categorize():
    """④ after an upload with "Categorize" ticked: Gemini picks the categories.

    POST /api/categorize
    -> {"categorized": 240, "total": 286, "categories": {"<txn id>": "Everyday / Groceries"}}

    Hosted mode with GEMINI_API_KEY only (404 otherwise). Only the transactions
    the last upload created with no category (state["to_categorize"]), and
    only picks Gemini is at least categorize.MIN_CONFIDENCE sure of; the
    others stay uncategorized. They're forgotten afterwards, also on an error:
    the import itself is done either way.
    """
    if not CATEGORIZE_AVAILABLE:
        return error_response("Not found.", 404)
    pending = session_state().pop("to_categorize", None)
    if not pending:
        return error_response("Nothing to categorize: upload a statement first.", 400)

    deadline = time.monotonic() + CATEGORIZE_SECONDS
    try:
        result, err = ynab_call(
            lambda token: categorize_transactions(
                pending["plan_id"],
                pending["transactions"],
                os.environ["GEMINI_API_KEY"],
                token,
                deadline=deadline,
            )
        )
    except GeminiError as e:  # its message never has the key in it
        current_app.logger.warning("Categorize failed: %s", e)
        return error_response(f"Imported, but not categorized: {e}", 502)
    if err:
        return err
    suggestions, updated = result
    return jsonify(
        {
            "categorized": updated,
            "total": len(suggestions),
            "categories": {
                s.transaction["id"]: category_label(s.category) for s in suggestions if s.category
            },
        }
    )
