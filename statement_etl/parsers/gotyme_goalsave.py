"""
ph-statement-import - GoTyme Bank GoalSave (savings) parser.

Parse a password-protected GoTyme savings statement PDF. One PDF holds every
GoalSave account (one to five), one section each. A section is the Everyday
statement's layout again (parsers/gotyme.py), with a "GoalSave No." line:

    Summary   Opening balance PHP 50,000.00 ... Closing balance PHP 42,200.00
    GoalSave No.: *******22222
    Period: 01 Oct 2026 - 31 Oct 2026
    Date         Details                    Credits   Debits  Running Balance
    01 Oct 2026  Earned interest             200.00     0.00        50,200.00

Output fields per transaction (amounts without commas):
    GoalSave No.     - the account's last digits as printed, e.g. "22222"
    Date             - e.g. "01 Oct 2026"
    Details          - e.g. "Transfer to Deposit account"
    Credits          - amount text without commas, e.g. "200.00" or "0.00"
    Debits           - amount text without commas, e.g. "40.00" or "0.00"
    Running Balance  - amount text without commas, e.g. "50160.00"

Each account goes into its own YNAB account: callers split the rows by
"GoalSave No." (pipeline.split_accounts, Source.account_column) and import
one account's rows at a time. to_transactions() turns rows into neutral
Transactions (transaction.py).

How it works:
    1. Open      - open the PDF with its password                (open_pdf)
    2. Sections  - a page with a GoalSave No. starts that
                   account's section and has its summary box;
                   a page without one continues the last section
                   (an account that runs over a page)            (_goalsave_number,
                                                                  gotyme.read_summary)
    3. Rows      - the same table as GoTyme Everyday             (gotyme.find_columns,
                                                                  gotyme.anchor_rows,
                                                                  gotyme.to_output_row)
    4. Check     - each section's total credits, total debits,
                   closing balance and running balances          (gotyme.check_totals)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.gotyme_goalsave savings.pdf  # $GOTYME_PDF_PASSWORD, or a prompt
    python -m statement_etl.parsers.gotyme_goalsave savings.pdf -p PASSWORD
    python -m statement_etl.parsers.gotyme_goalsave savings.pdf -o out.json
"""

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.parsers import gotyme
from statement_etl.parsers.common import StatementError, open_pdf, to_decimal
from statement_etl.transaction import Transaction, transaction_ref

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - GoalSave-specific; the table itself is GoTyme Everyday's
# =============================================================================

# The column that tells the accounts apart (Source.account_column)
ACCOUNT_COLUMN = "GoalSave No."

# Every column of a parsed row: the account, then the table's own columns
COLUMNS = (ACCOUNT_COLUMN, *gotyme.COLUMNS)

# "GoalSave No.: *******22222" -> "22222"
GOALSAVE_NO_RE = re.compile(r"\bGoalSave No\.:\s*\**(\d+)\b", re.IGNORECASE)


# =============================================================================
# PARSING - go through every page, one section per GoalSave account
# =============================================================================


def _goalsave_number(lines: list[dict]) -> str | None:
    """The GoalSave No. digits printed on a page, or None if it has none."""
    for line in lines:
        match = GOALSAVE_NO_RE.search(line["text"])
        if match:
            return match.group(1)
    return None


def parse_transactions(
    pdf_path, password: str = "", on_page: Callable[[int, int], None] | None = None
) -> list[dict[str, str]]:
    """Parse a GoTyme savings statement PDF into a list of row dicts.

    Each dict has keys equal to COLUMNS, with amounts as plain text without
    commas ("200.00"), every account's rows in the order the PDF prints them.
    on_page(page, pages), if given, is called as each page is read. Raises
    StatementError if the password is wrong, there's no GoalSave account, or
    any account's statement checks fail.
    """
    sections = []  # [{"account": "22222", "summary": {...}, "rows": [...]}], in PDF order

    with open_pdf(pdf_path, password) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if on_page:
                on_page(number, len(pdf.pages))  # progress, for the page or the run log
            lines = gotyme.page_lines(page)

            # 1. A GoalSave No. starts that account's section, with its summary box
            account = _goalsave_number(lines)
            if account and not (sections and sections[-1]["account"] == account):
                sections.append(
                    {"account": account, "summary": gotyme.read_summary(lines), "rows": []}
                )

            # 2. The table rows, for the section this page belongs to
            columns = gotyme.find_columns(lines)
            if columns is None:
                continue
            if not sections:
                raise StatementError(
                    f"Page {number} has transactions but no GoalSave No. above them."
                )
            anchored = gotyme.anchor_rows(lines, columns)
            sections[-1]["rows"].extend(gotyme.to_output_row(r) for r in anchored)

    if not sections:
        raise StatementError("No GoalSave account found in this PDF. Is it the savings statement?")

    # 3. Each account's totals and running balances add up on their own
    rows = []
    for section in sections:
        try:
            gotyme.check_totals(section["rows"], section["summary"])
        except StatementError as e:
            raise StatementError(f"{ACCOUNT_COLUMN} {section['account']}: {e}") from e
        rows.extend({ACCOUNT_COLUMN: section["account"], **row} for row in section["rows"])
    return rows


# =============================================================================
# BALANCES & SUMMARY - for one account's rows (pipeline.split_accounts)
# =============================================================================

# Same columns as GoTyme Everyday, so the same functions: each takes one
# account's rows, e.g. statement_balances(rows) -> {"starting": ..., "ending": ...}
statement_balances = gotyme.statement_balances
summary = gotyme.summary


# =============================================================================
# CONVERTING - rows -> neutral Transactions (registered in sources.py)
#
# Input row example:
#   {
#     "GoalSave No.":    "22222",
#     "Date":            "01 Oct 2026",
#     "Details":         "Earned interest",
#     "Credits":         "200.00",
#     "Debits":          "0.00",
#     "Running Balance": "50200.00"
#   }
#
# Like GoTyme Everyday there are no reference numbers, so the Transaction key
# is "{GoalSave No.}|{Date}|{Credits}|{Debits}|{Running Balance}|{n}", where n
# counts earlier rows with the same five values (almost always 0). The memo is
# the GoTyme Ref: transaction_ref("gotyme_goalsave", key), the hash part of
# the import_id, so searching it in YNAB finds the row.
# =============================================================================


def to_transactions(rows: list[dict[str, str]]) -> list[Transaction]:
    """Convert GoalSave rows into neutral Transactions, one per row.

    The payee is the Details text as printed, e.g. "Earned interest".

    Example:
        to_transactions([{"GoalSave No.": "22222", "Date": "01 Oct 2026",
                          "Details": "Earned interest", "Credits": "200.00",
                          "Debits": "0.00", "Running Balance": "50200.00"}])
    """
    counts: dict[str, int] = {}
    transactions = []
    for row in rows:
        base_key = (
            f"{row[ACCOUNT_COLUMN]}|{row['Date']}|{row['Credits']}|{row['Debits']}"
            f"|{row['Running Balance']}"
        )
        occurrence = counts.get(base_key, 0)
        counts[base_key] = occurrence + 1

        key = f"{base_key}|{occurrence}"
        transactions.append(
            Transaction(
                source="gotyme_goalsave",
                key=key,
                date=datetime.strptime(row["Date"], gotyme.DATE_FORMAT).date(),
                payee=row["Details"],
                amount=to_decimal(row["Credits"]) - to_decimal(row["Debits"]),
                memo=f"GoTyme Ref: {transaction_ref('gotyme_goalsave', key)}",
                flagged=False,
            )
        )
    return transactions


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output
# =============================================================================


def main() -> None:
    # 1. Getting the PDF: read the file path and password from the CLI, env or prompt
    ap = argparse.ArgumentParser(description="Parse a GoTyme GoalSave (savings) statement PDF.")
    ap.add_argument("pdf", help="Path to the GoTyme savings statement PDF")
    ap.add_argument(
        "-p",
        "--password",
        help="PDF password (defaults to $GOTYME_PDF_PASSWORD, or prompts if omitted)",
    )
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()

    if args.password is not None:
        password = args.password
    elif os.environ.get("GOTYME_PDF_PASSWORD"):
        password = os.environ["GOTYME_PDF_PASSWORD"]
    else:
        password = getpass.getpass("PDF password: ")

    # 2. Parsing: open the PDF with the password and read every account's table
    transactions = parse_transactions(args.pdf, password)

    # 3. Output: print the JSON, or write it to a file with -o
    out = json.dumps(transactions, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out + "\n")
        print(f"Wrote {len(transactions)} transactions to {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
