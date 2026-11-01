"""
ph-statement-import - e-wallet / bank statement PDF -> YNAB.

Steps: parse PDF transactions -> convert to YNAB format -> import into YNAB.
Handles every e-wallet / bank in sources.SOURCES; pick one with --source
(default: GCash).

How it works:
    1. Parse      - open the PDF with its password and read the
                    transaction table                            (parse_statement ->
                                                                  Source.parse)
    2. Check      - the parser stops on a totals mismatch, and
                    adds balance-correction rows (STARTING
                    BALANCE CORRECTION / "Refund from" /
                    split "Payment to" / MISSING
                    TRANSACTIONS) where the running balance
                    doesn't add up; a warning only               (Source.balance_warning)
    3. Convert    - turn all rows, corrections included, into
                    Transactions, then YNAB transactions
                    (corrections flagged red)                    (parse_statement ->
                                                                  convert.rows_to_transactions,
                                                                  ynab_api.format.to_ynab)
    4. Start      - before the upload, the YNAB account balance
                    should equal the statement's starting
                    balance; a warning only, the upload goes on  (starting_balance_check ->
                                                                  ynab_api.balance.check_starting_balance)
                    A newly created YNAB account (only its
                    "Starting Balance" transaction) gets that
                    transaction set to the statement's starting
                    balance instead                              (new_account_starting_balance ->
                                                                  ynab_api.balance.set_new_account_starting_balance)
    5. Import     - send all transactions to YNAB in one call    (upload_transactions ->
                                                                  ynab_api.client.import_transactions)
    6. End        - after the upload, the YNAB account balance
                    should equal the statement's ending
                    balance; a warning only, adjust in YNAB      (ending_balance_check ->
                                                                  ynab_api.client.check_ending_balance)
    7. Output     - print each step (STEP 1 to STEP 4)           (run_pipeline, _print_table)

    import_statement() runs the new-account part of 4, then 5 and 6, so every
    caller imports the same way.

Who uses what:
    Command line - main() -> run_pipeline(), which prints and exits on errors.
    Web app      - web/upload.py calls parse_statement(), starting_balance_check()
                   and import_statement(); they raise errors instead of exiting,
                   so the page can show them. It never calls run_pipeline().

Adding another e-wallet or bank needs no change here: register it in
sources.py (see docs/adding-an-e-wallet.md).

Requirements:
    pip install pdfplumber ynab

Environment:
    <SOURCE>_PDF_PASSWORD - password for the statement PDF; the name is the
                            source's password_env, e.g. GCASH_PDF_PASSWORD
    BEARER_TOKEN          - YNAB personal access token
    YNAB_ACCOUNT_ID       - YNAB account id for the e-wallet (or pass --account-id)
    YNAB_PLAN_ID          - YNAB plan (budget) id (or pass --plan-id)

Usage:
    python -m statement_etl.pipeline statement.pdf --dry-run                 # parse only
    python -m statement_etl.pipeline statement.pdf                           # parse and import
    python -m statement_etl.pipeline statement.pdf --source gcash --dry-run  # pick the source

Output:
    1. The parsed rows, with the same columns as the PDF, and whether parsing succeeded
    2. The starting balance check against YNAB (not in a dry run)
    3. The transactions created in YNAB (Date / Payee / Category / Memo / Amount),
       left unapproved so they can be reviewed and approved in YNAB
    4. The ending balance check against YNAB
"""

import argparse
import os
import sys
from dataclasses import dataclass
from decimal import Decimal

import ynab
from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.convert import rows_to_transactions
from statement_etl.parsers.common import StatementError
from statement_etl.sources import DEFAULT_SOURCE, SOURCES, get_source
from statement_etl.ynab_api.balance import (
    balance_check_message,
    check_starting_balance,
    get_account_balance,
    new_account_message,
    set_new_account_starting_balance,
)
from statement_etl.ynab_api.client import (
    check_ending_balance,
    find_already_imported,
    import_transactions,
)
from statement_etl.ynab_api.format import to_ynab

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# PROGRESS - the steps a caller can show while a statement is imported
#
# parse_statement() and import_statement() take on_step(step, detail) and call
# it as each step starts. The web page (web/upload.py) and the auto-import run
# log (autoimport/runner.py) show them; the command line prints its own steps.
# =============================================================================

STEP_READ = "read"  # read the PDF's rows; detail {"page": 3, "pages": 5}
STEP_UPLOAD = "upload"  # send the transactions to YNAB; detail {"count": 286}
STEP_END_BALANCE = "end_balance"  # YNAB's balance after the upload vs. the statement's


def _no_progress(step, detail):
    """on_step for callers that don't show progress."""


# =============================================================================
# STEPS - parse and upload (REUSABLE)
#
# Plain functions that raise errors instead of exiting. Used by both the
# command line (run_pipeline below) and the web app (web/upload.py).
# =============================================================================


def parse_statement(pdf, password, source=DEFAULT_SOURCE, on_step=None):
    """ph-statement-import step: parse a statement PDF from `source` and
    convert the rows into YNAB transactions.

    Used by run_pipeline() and by web/upload.py. Raises instead of exiting, so the
    web app can show the error on the page.

    Args:
        pdf:      a file path, or a file-like object (the web upload)
        password: password used to unlock the PDF
        source:   key in sources.SOURCES, e.g. "gcash"
        on_step:  optional progress callback (see PROGRESS above): STEP_READ,
                  once per page

    Returns:
        (rows, transactions): rows with the statement's own columns
        (Source.columns, for display) and
        the YNAB transaction dicts (for upload). If the running balance doesn't
        add up, both also hold balance-correction rows (see balance_warning());
        they are uploaded too, flagged red.

    Raises:
        StatementError: wrong password, totals mismatch, or no transactions
        found (a ValueError, with a message for the user)
        ValueError: unknown source
    """
    src = get_source(source)

    # Parse: read the PDF table (rows use the PDF's own column names),
    # including any balance-correction rows the parser added
    report = on_step or _no_progress
    rows = src.parse(
        pdf, password, on_page=lambda page, pages: report(STEP_READ, {"page": page, "pages": pages})
    )

    # Must have at least one real statement row, not only corrections
    if not any(not src.is_correction_row(r) for r in rows):
        raise StatementError("no transactions found in the PDF")

    # Convert every row; correction rows are uploaded too (flagged red)
    return rows, [to_ynab(t) for t in rows_to_transactions(rows, source)]


def upload_transactions(transactions, account_id, plan_id, access_token=None):
    """ph-statement-import step: send YNAB transactions to YNAB in one API call.

    Used by run_pipeline() and by web/upload.py. access_token is the web app's token
    (BEARER_TOKEN or OAuth); None uses $BEARER_TOKEN from .env.

    Returns the SaveTransactionsResponseData (transactions, duplicate_import_ids).
    Raises ynab.ApiException (or a subclass) if YNAB rejects the request.
    """
    return import_transactions(transactions, account_id, plan_id, access_token)


def starting_balance_check(
    rows, transactions, account_id, plan_id, access_token=None, source=DEFAULT_SOURCE
):
    """ph-statement-import step, before the upload: compare the YNAB account
    balance with the statement's starting balance. Read only.

    rows and transactions are from parse_statement(). Transactions YNAB
    already has (ynab_api.client.find_already_imported) are skipped on upload, so
    the expected "YNAB balance AFTER upload" adds only the others, and the
    check says "X transactions already in YNAB. Only Y will be uploaded."
    Returns the check dict from
    ynab_api.balance.check_starting_balance(); describe it with
    ynab_api.balance.balance_check_message(). A mismatch is a warning only.
    Raises ynab.ApiException if YNAB rejects the request.
    """
    starting = get_source(source).statement_balances(rows)["starting"]
    already = find_already_imported(transactions, account_id, plan_id, access_token)
    to_upload = [t for t in transactions if t["import_id"] not in already]
    upload_total = Decimal(sum(t["amount"] for t in to_upload)) / 1000
    return check_starting_balance(
        starting,
        upload_total,
        account_id,
        plan_id,
        access_token,
        already_in_ynab=len(already),
        to_upload=len(to_upload),
    )


def new_account_starting_balance(
    rows, account_id, plan_id, access_token=None, source=DEFAULT_SOURCE
):
    """ph-statement-import step, just before the upload: if the YNAB account
    is newly created, set its Starting Balance to the statement's starting balance.

    Returns {"from", "to"} for a new account (describe it with
    ynab_api.balance.new_account_message()), None otherwise.
    Raises ynab.ApiException if YNAB rejects the request.
    """
    starting = get_source(source).statement_balances(rows)["starting"]
    return set_new_account_starting_balance(starting, account_id, plan_id, access_token)


def ending_balance_check(
    rows, account_id, plan_id, access_token=None, ynab_before=None, source=DEFAULT_SOURCE
):
    """ph-statement-import step, after the upload: compare the YNAB account
    balance with the statement's ending balance.

    ynab_before is the YNAB balance just before the upload, for the warning.
    Same as starting_balance_check(), using ynab_api.client.check_ending_balance().
    Any adjustment in YNAB is up to the user.
    """
    ending = get_source(source).statement_balances(rows)["ending"]
    return check_ending_balance(ending, account_id, plan_id, access_token, ynab_before)


@dataclass
class ImportResult:
    """What import_statement() did, for the caller to show or record."""

    new_account: dict | None  # {"from", "to"} if a new account's Starting Balance was set
    ynab_before: Decimal  # YNAB account balance just before the upload, in pesos
    created: list  # YNAB TransactionDetail objects that were created
    skipped: int  # transactions YNAB already had (same import_id), so not created again
    ending_check: dict | None  # from ending_balance_check(); None if it failed
    ending_error: ynab.ApiException | None  # why the ending check failed, if it did


def import_statement(
    rows, transactions, account_id, plan_id, access_token=None, source=DEFAULT_SOURCE, on_step=None
):
    """ph-statement-import step: import a parsed statement into one YNAB account.

    The one place that uploads, used by the command line and the web app.
    rows and transactions are from parse_statement(); access_token works as in
    upload_transactions(). on_step, if given, is told STEP_UPLOAD before step 1
    and STEP_END_BALANCE before step 4 (see PROGRESS above).

        result = import_statement(rows, transactions, account_id, plan_id)
        len(result.created), result.skipped  ->  (23, 2)

    Raises ynab.ApiException if YNAB rejects steps 1-3, so each caller reports
    it its own way. Step 4 runs after the upload is done, so its failure is
    returned in ending_error instead.

    Running it again with the same transactions is safe (the web app does
    after refreshing an expired token): YNAB skips import_ids it already has,
    and step 1 changes nothing once the Starting Balance matches.
    """
    report = on_step or _no_progress
    report(STEP_UPLOAD, {"count": len(transactions)})

    # 1. A newly created YNAB account gets its Starting Balance set to the statement's
    new_account = new_account_starting_balance(
        rows, account_id, plan_id, access_token, source=source
    )

    # 2. YNAB balance just before the upload, for the ending-balance message
    ynab_before = get_account_balance(plan_id, account_id, access_token)

    # 3. All transactions in one API call; YNAB skips import_ids it already has
    uploaded = upload_transactions(transactions, account_id, plan_id, access_token)

    # 4. YNAB balance after the upload vs. the statement's ending balance
    report(STEP_END_BALANCE, {})
    ending_check, ending_error = None, None
    try:
        ending_check = ending_balance_check(
            rows, account_id, plan_id, access_token, ynab_before, source=source
        )
    except ynab.ApiException as e:
        ending_error = e

    return ImportResult(
        new_account=new_account,
        ynab_before=ynab_before,
        created=uploaded.transactions or [],
        skipped=len(uploaded.duplicate_import_ids or []),
        ending_check=ending_check,
        ending_error=ending_error,
    )


# =============================================================================
# COMMAND-LINE RUN - parse -> check -> import, printing each step (REUSABLE)
#
# Output, in order:
#   "Uploading <Source.name> Statement..."  (e.g. "Uploading GCash Statement...")
#   STEP 1: PARSE            - table of the PDF rows, then "Parsing successful: N ...",
#                              the source's summary line, and a WARNING if the
#                              ending balance doesn't add up
#   STEP 2: STARTING BALANCE - YNAB balance vs. statement starting balance
#                              (WARNING on a mismatch; the import still goes on).
#                              A new YNAB account gets its Starting Balance set.
#   STEP 3: IMPORT           - table of what YNAB created, skipped count, review note
#   STEP 4: ENDING BALANCE   - YNAB balance vs. statement ending balance
#                              (WARNING on a mismatch; adjust in YNAB as you see fit)
# Any error prints "ERROR: ..." and exits with status 1 (see _fail()).
# =============================================================================


def run_pipeline(pdf_path, password, account_id, plan_id, dry_run=False, source=DEFAULT_SOURCE):
    """Run ph-statement-import from the command line: parse a
    statement PDF from `source`, then import the transactions into YNAB.

    Exits with status 1 if parsing or importing fails.

    Args:
        pdf_path:   path to the encrypted statement PDF
        password:   password used to unlock the PDF
        account_id: YNAB account the transactions are added to (e.g. GCash1)
        plan_id:    YNAB plan (budget) that contains that account
        dry_run:    if True, only parse and print the rows; nothing is sent to YNAB
        source:     key in sources.SOURCES, e.g. "gcash"

    Returns:
        The YNAB transactions (list of dicts with date, payee_name,
        import_id, amount, memo).
    """
    src = get_source(source)

    # flush so this shows before any parsing error (errors go to stderr)
    print(f"Uploading {src.name} Statement...\n", flush=True)

    # 1 + 3. Parsing and converting: open the PDF with the password, read the
    #        transaction table, convert the rows into YNAB transactions
    try:
        rows, transactions = parse_statement(pdf_path, password, source)
    except Exception as e:  # noqa: BLE001
        _fail(f"Parsing failed: {e}")

    # STEP 1 output: the rows as read from the PDF, plus any balance-correction rows
    print("STEP 1: PARSE")
    _print_table(
        src.columns,
        [tuple(row[c] for c in src.columns) for row in rows],
        right_aligned=src.amount_columns,
    )
    statement_count = sum(1 for r in rows if not src.is_correction_row(r))
    print(f"Parsing successful: {statement_count} transactions from {pdf_path}")
    summary = src.summary(rows)
    if summary:
        print(summary)
    balances = src.statement_balances(rows)
    print(
        f"Statement balance: starting {balances['starting']:,.2f}, ending {balances['ending']:,.2f}"
    )

    # 2. Checking: running balance didn't add up -> warn, keep going
    #    (the correction rows are imported, flagged red)
    warning = src.balance_warning(rows)
    if warning:
        print(f"WARNING: {warning}")

    # Dry run stops here so you can check the parsed data before importing
    if dry_run:
        print("\nDry run: nothing was sent to YNAB")
        return transactions

    # 4. STEP 2: starting balance - YNAB before the upload vs. the statement;
    #    a mismatch is only a warning, the import goes ahead
    print("\nSTEP 2: STARTING BALANCE")
    try:
        check = starting_balance_check(rows, transactions, account_id, plan_id, source=source)
    except ynab.ApiException as e:
        _fail(
            f"Could not read the YNAB balance: YNAB returned {e.status} {e.reason}\n{e.body or ''}"
        )
    # A new YNAB account's message comes after import_statement() sets its
    # Starting Balance; any other account's can be shown now
    if not check["new_account"]:
        print(
            ("" if check["matches"] and not check["already_in_ynab"] else "WARNING: ")
            + balance_check_message(check)
        )

    # 5 + 6. Importing into YNAB: a new account's Starting Balance, one upload
    #        call for all transactions, then the ending balance check
    try:
        imported = import_statement(rows, transactions, account_id, plan_id, source=source)
    except ynab.ApiException as e:
        _fail(f"Import failed: YNAB returned {e.status} {e.reason}\n{e.body or ''}")

    if check["new_account"]:
        if imported.new_account:
            print(new_account_message(imported.new_account))
        else:
            # Got other transactions since the check: compare as a normal account
            check = {**check, "new_account": None, "matches": check["difference"] == 0}
            print(
                ("" if check["matches"] and not check["already_in_ynab"] else "WARNING: ")
                + balance_check_message(check)
            )

    # STEP 3 output: what YNAB actually created (already-imported rows are not included)
    created = imported.created
    print("\nSTEP 3: IMPORT")
    _print_table(
        ("Date", "Payee", "Category", "Memo", "Amount"),
        [
            (
                str(t.var_date),
                t.payee_name,
                t.category_name or "Uncategorized",
                t.memo,
                # milliunits -> pesos: -1124250 -> -1,124.25
                f"{t.amount / 1000:,.2f}",
            )
            for t in created
        ],
        right_aligned=("Amount",),
    )
    print(f"Import successful: {len(created)} transactions created in YNAB (unapproved)")

    # Rows whose import_id (hashed from Reference No.) YNAB already had are skipped, not duplicated
    if imported.skipped:
        print(f"Skipped {imported.skipped} already imported")
    if created:
        print("Review and approve them in YNAB.")

    # STEP 4 output: ending balance - YNAB after the upload vs. the statement.
    # The import is already done, so a failed check is a warning, not an error.
    print("\nSTEP 4: ENDING BALANCE")
    if imported.ending_error:
        e = imported.ending_error
        print(
            "WARNING: could not read the YNAB balance after the upload:"
            f" YNAB returned {e.status} {e.reason}"
        )
    else:
        check = imported.ending_check
        print(("" if check["matches"] else "WARNING: ") + balance_check_message(check))
    return transactions


# =============================================================================
# OUTPUT HELPERS - error exit and text tables for the command line (REUSABLE)
# =============================================================================


def _fail(message):
    """Print an error and stop the script with exit status 1."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _print_table(headers, rows, right_aligned=(), max_width=40):
    """Print rows as an aligned text table.

    Args:
        headers:       column names
        rows:          tuples of values, one per row, in the same order as headers
        right_aligned: names of columns to right-align (amounts)
        max_width:     longest text shown in a cell before it is cut off with "…"
    """
    if not rows:
        return

    # Shorten long text (e.g. the BPI "Received GCash ..." payee) so columns stay aligned
    def clip(text):
        text = str(text or "")
        return text if len(text) <= max_width else text[: max_width - 1] + "…"

    rows = [tuple(clip(c) for c in r) for r in rows]

    # Each column is as wide as its longest value (or header)
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    # Format one line: text columns left-aligned, amount columns right-aligned
    def fmt(cols):
        return "  ".join(
            c.rjust(w) if h in right_aligned else c.ljust(w)
            for c, w, h in zip(cols, widths, headers)  # noqa: B905  # same length as headers
        )

    print()
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))
    print()


# =============================================================================
# MAIN - read settings from the command line / .env, check them, run (REUSABLE)
# =============================================================================


def main():
    """Command-line entry point: read settings, check they're present, run
    ph-statement-import."""
    # Command-line options; account and plan ids fall back to environment variables
    ap = argparse.ArgumentParser(description="Import an e-wallet / bank statement PDF into YNAB.")
    ap.add_argument("pdf", help="Path to the encrypted statement PDF")
    ap.add_argument(
        "--source",
        choices=sorted(SOURCES),
        default=DEFAULT_SOURCE,
        help=f"Which e-wallet / bank the statement is from (default: {DEFAULT_SOURCE})",
    )
    ap.add_argument(
        "--account-id",
        default=os.environ.get("YNAB_ACCOUNT_ID"),
        help="YNAB account id (default: $YNAB_ACCOUNT_ID)",
    )
    ap.add_argument(
        "--plan-id",
        default=os.environ.get("YNAB_PLAN_ID"),
        help="YNAB plan id (default: $YNAB_PLAN_ID)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print only; don't send to YNAB",
    )
    args = ap.parse_args()

    # YNAB settings are only needed when actually importing
    if not args.dry_run:
        if not args.account_id:
            ap.error("--account-id or YNAB_ACCOUNT_ID is required")
        if not args.plan_id:
            ap.error("--plan-id or YNAB_PLAN_ID is required")
        if "BEARER_TOKEN" not in os.environ:
            ap.error("BEARER_TOKEN environment variable is not set")

    # PDF password always comes from the environment, never the command line;
    # each source has its own variable, e.g. GCASH_PDF_PASSWORD
    password_env = SOURCES[args.source].password_env
    password = os.environ.get(password_env)
    if not password:
        ap.error(f"{password_env} environment variable is not set")

    run_pipeline(args.pdf, password, args.account_id, args.plan_id, args.dry_run, args.source)


if __name__ == "__main__":
    main()
