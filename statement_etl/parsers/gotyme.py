"""
ph-statement-import - GoTyme Bank Everyday Deposit Account parser.

Parse a password-protected GoTyme Everyday Deposit Account statement PDF.

Output fields per transaction (same columns as the PDF table, amounts without commas):
    Date             - e.g. "05 Oct 2026"
    Details          - full transaction details, wrapped lines joined with a space
    Credits          - amount text without commas, e.g. "1000.00" or "0.00"
    Debits           - amount text without commas, e.g. "249.00" or "0.00"
    Running Balance  - amount text without commas, e.g. "4751.00"

to_transactions() turns these rows into neutral Transactions (transaction.py).

How it works:
    1. Open      - open the PDF with its password                (open_pdf)
    2. Summary   - read Period, Opening, Total Credit,
                   Total Debit and Closing from page 1           (_read_summary)
    3. Header    - find the table header on each page to learn
                   the column edges                              (_find_columns)
    4. Rows      - anchor one row per date line and attach
                   wrapped Details lines above and below it      (_anchor_rows,
                                                                  _to_output_row)
    5. Check     - verify total credits, total debits, closing
                   balance, and every row's running balance      (_check_totals)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.gotyme statement.pdf  # uses $GOTYME_PDF_PASSWORD or prompts
    python -m statement_etl.parsers.gotyme statement.pdf -p PASSWORD
    python -m statement_etl.parsers.gotyme statement.pdf -o out.json
"""

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.parsers.common import StatementError, group_lines, open_pdf, to_decimal
from statement_etl.transaction import Transaction

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - GoTyme-specific; change these for another e-wallet / bank
# =============================================================================

# Every column of the transaction table, in the order the PDF prints them.
# Each parsed row is a dict with exactly these keys (see _to_output_row).
COLUMNS = (
    "Date",
    "Details",
    "Credits",
    "Debits",
    "Running Balance",
)

# Words in the table header. "Running Balance" prints across two lines around
# the rest of the header ("Running" ~4 pt above, "Balance" on or ~4 pt below).
DATE_LABEL = "Date"
DETAILS_LABEL = "Details"
CREDITS_LABEL = "Credits"
DEBITS_LABEL = "Debits"
BALANCE_LABEL = "Balance"
HEADER_BAND = 6.0  # max vertical distance (pt) from the header line to "Running" / "Balance"

# Wrapped Details lines sit about 4 pt above and 4 pt below the date/amount line
WRAP_ABOVE = 6.0

# How the date is written at the start of each row: "05 Oct 2026"
DATE_RE = re.compile(r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}$")
DATE_FORMAT = "%d %b %Y"

# Money amounts: "1,234.56" or "0.00"
AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")

# Summary box on page 1
PERIOD_RE = re.compile(
    r"\bPeriod:\s*(\d{2}\s+[A-Za-z]{3}\s+\d{4}\s*-\s*\d{2}\s+[A-Za-z]{3}\s+\d{4})",
    re.IGNORECASE,
)
OPENING_RE = re.compile(r"\bOpening balance\s+PHP\s+(-?[\d,]+\.\d{2})\b", re.IGNORECASE)
TOTAL_CREDIT_RE = re.compile(r"\bTotal Credit\s+PHP\s+(-?[\d,]+\.\d{2})\b", re.IGNORECASE)
TOTAL_DEBIT_RE = re.compile(r"\bTotal Debit\s+PHP\s+(-?[\d,]+\.\d{2})\b", re.IGNORECASE)
CLOSING_RE = re.compile(r"\bClosing balance\s+PHP\s+(-?[\d,]+\.\d{2})\b", re.IGNORECASE)

# Footer lines below the table that are not wrapped Details text
FOOTER_RE = re.compile(
    r"^(All figures are in PHP\b|Document ID:|Request Date:|\bPage\s+\d+\s+of\s+\d+\b)",
    re.IGNORECASE,
)

# Payee patterns in Details:
#   "Card payment at SHOPEE PH, Card •••• 1234" -> "SHOPEE PH"
#   "Transfer to/from GoalSave"                 -> "GoalSave"
#   "Transfer to/from NAME BANK, Account No. …" -> "Transfer to/from NAME BANK"
GOTYME_CARD_PAYMENT_RE = re.compile(r"^Card payment at\s+(.+),\s*Card\b.*$", re.IGNORECASE)
GOTYME_GOALSAVE_RE = re.compile(r"^Transfer\s+(?:to|from)\s+GoalSave$", re.IGNORECASE)
GOTYME_TRANSFER_ACCOUNT_RE = re.compile(
    r"^(Transfer\s+(?:to|from)\s+.+?),\s*Account No\..*$", re.IGNORECASE
)


# =============================================================================
# PAGE LAYOUT - read the summary box, find column edges, and anchor rows
# =============================================================================


def _page_lines(page_or_lines) -> list[dict]:
    """Accept either a pdfplumber page (or FakePage) or pre-grouped lines."""
    if isinstance(page_or_lines, list):
        return page_or_lines
    return group_lines(page_or_lines.extract_words(keep_blank_chars=False, use_text_flow=False))


def _read_summary(page) -> dict:
    """Read the summary box from a page (Period, Opening, Total Credit, Total Debit, Closing).

    Returns a dict with any of {"period", "opening", "credit", "debit", "closing"}
    found on the page; money values are Decimals.
    """
    summary = {}
    for line in _page_lines(page):
        text = line["text"]
        period = PERIOD_RE.search(text)
        if period:
            summary["period"] = period.group(1)
        for key, pattern in (
            ("opening", OPENING_RE),
            ("credit", TOTAL_CREDIT_RE),
            ("debit", TOTAL_DEBIT_RE),
            ("closing", CLOSING_RE),
        ):
            match = pattern.search(text)
            if match:
                summary[key] = to_decimal(match.group(1))
    return summary


def _find_columns(page) -> dict | None:
    """Locate the table header on a page and return its column edges.

    "Running Balance" spans two lines ("Running" ~4 pt above the header line,
    "Balance" on the header line or ~4 pt below it). Returns a dict with
    header_top and column x positions, or None if this page has no table.
    """
    lines = _page_lines(page)
    for line in lines:
        by_text = {w["text"]: w for w in line["words"]}
        if not {DATE_LABEL, DETAILS_LABEL, CREDITS_LABEL, DEBITS_LABEL}.issubset(by_text):
            continue

        # Find "Balance" on this header line or within HEADER_BAND pt of it
        nearby_words = [
            w
            for near in lines
            if abs(near["top"] - line["top"]) <= HEADER_BAND
            for w in near["words"]
        ]
        balance = next((w for w in nearby_words if w["text"] == BALANCE_LABEL), None)
        if balance is None:
            continue

        return {
            "header_top": max(line["top"], balance["top"]),
            "date_x0": by_text[DATE_LABEL]["x0"],
            "details_x0": by_text[DETAILS_LABEL]["x0"],
            "credits_x0": by_text[CREDITS_LABEL]["x0"],
            "credits_x1": by_text[CREDITS_LABEL]["x1"],
            "debits_x1": by_text[DEBITS_LABEL]["x1"],
            "balance_x1": balance["x1"],
        }
    return None


def _parse_date_line(line: dict) -> dict | None:
    """Read a transaction anchor line (starts with DD Mon YYYY, ends with 3 amounts).

    Returns a raw row dict, or None if the line is not a date-and-amounts line.
    """
    words = line["words"]
    if len(words) < 6:
        return None

    date_text = " ".join(w["text"] for w in words[:3])
    if not DATE_RE.match(date_text):
        return None
    try:
        datetime.strptime(date_text, DATE_FORMAT)
    except ValueError:
        return None

    amounts = words[-3:]
    if not all(AMOUNT_RE.match(w["text"]) for w in amounts):
        return None

    return {
        "top": line["top"],
        "date": date_text,
        "details_words": list(words[3:-3]),
        "credits": to_decimal(amounts[0]["text"]),
        "debits": to_decimal(amounts[1]["text"]),
        "balance": to_decimal(amounts[2]["text"]),
    }


def _anchor_rows(lines: list[dict], columns: dict) -> list[dict]:
    """Build one row per date line and attach wrapped Details words in its band.

    A long Details value wraps onto two lines: the first sits ~4 pt above the
    date and amounts line, and the second sits ~4 pt below it. Details words
    between this date's band (top - WRAP_ABOVE) and the next date's band belong
    to this row.
    """
    header_top = columns["header_top"]
    details_left = columns["details_x0"] - 2
    credits_left = columns["credits_x0"]

    # 1. Collect table lines below the header, stopping at the page footer
    body_lines = []
    for line in lines:
        if line["top"] <= header_top:
            continue
        if FOOTER_RE.search(line["text"]):
            break
        body_lines.append(line)

    # 2. Anchor one row per date line and keep leftover lines for wrapped Details
    rows, other_lines = [], []
    for line in body_lines:
        row = _parse_date_line(line)
        if row is not None:
            rows.append(row)
        else:
            other_lines.append(line)

    if not rows:
        return []

    # 3. Assign each wrapped Details line to the row whose vertical band it falls in
    for i, row in enumerate(rows):
        band_start = row["top"] - WRAP_ABOVE
        band_end = rows[i + 1]["top"] - WRAP_ABOVE if i + 1 < len(rows) else float("inf")
        for line in other_lines:
            if not (band_start <= line["top"] < band_end):
                continue
            # Wrapped Details lines start at the Details column and sit left of Credits
            if not line["words"] or line["words"][0]["x0"] < details_left:
                continue
            details_words = [
                w
                for w in line["words"]
                if w["x0"] >= details_left and line["words"][0]["x0"] < credits_left
            ]
            row["details_words"].extend(details_words)

    return rows


def _to_output_row(row: dict) -> dict[str, str]:
    """Build the final output row with keys equal to COLUMNS and plain amounts."""
    details_words = sorted(row["details_words"], key=lambda w: (round(w["top"]), w["x0"]))
    return {
        "Date": row["date"],
        "Details": " ".join(w["text"] for w in details_words),
        "Credits": f"{row['credits']:.2f}",
        "Debits": f"{row['debits']:.2f}",
        "Running Balance": f"{row['balance']:.2f}",
    }


# =============================================================================
# PARSING - go through every page, collect rows, and verify all totals
# =============================================================================


def parse_transactions(pdf_path, password: str = "") -> list[dict[str, str]]:
    """Parse a GoTyme Everyday Deposit statement PDF into a list of row dicts.

    Each dict has keys equal to COLUMNS, with amounts as plain text without
    commas ("249.00"). Raises StatementError if the password is wrong or any of
    the four statement checks fails.
    """
    rows = []
    statement_summary = {}

    with open_pdf(pdf_path, password) as pdf:
        for page in pdf.pages:
            lines = _page_lines(page)

            # 1. Read the summary box (printed on page 1)
            if not statement_summary:
                statement_summary.update(_read_summary(lines))

            # 2. Find the table header on this page; skip pages with no table
            columns = _find_columns(lines)
            if columns is None:
                continue

            # 3 + 4. Anchor rows by date line, attach wrapped Details, format output
            anchored = _anchor_rows(lines, columns)
            rows.extend(_to_output_row(r) for r in anchored)

    # 5. Verify total credits, total debits, closing balance, and running balance
    _check_totals(rows, statement_summary)
    return rows


# =============================================================================
# CHECKING - verify statement totals and every row's running balance
# =============================================================================


def _check_totals(rows: list[dict[str, str]], summary: dict) -> None:
    """Verify the four GoTyme statement checks; raise StatementError on mismatch.

    1. sum(Credits) == Total Credit
    2. sum(Debits) == Total Debit
    3. Opening + sum(Credits) - sum(Debits) == Closing
    4. Each row's Running Balance == previous balance + Credits - Debits
    """
    credits = sum((to_decimal(r["Credits"]) for r in rows), Decimal("0.00"))
    debits = sum((to_decimal(r["Debits"]) for r in rows), Decimal("0.00"))

    # 1. Total Credit check
    if "credit" in summary and credits != summary["credit"]:
        raise StatementError(
            f"parsed total credit {credits:.2f} != statement total {summary['credit']:.2f}"
        )

    # 2. Total Debit check
    if "debit" in summary and debits != summary["debit"]:
        raise StatementError(
            f"parsed total debit {debits:.2f} != statement total {summary['debit']:.2f}"
        )

    # 3. Opening + Credits - Debits == Closing check
    if "opening" in summary and "closing" in summary:
        expected_closing = summary["opening"] + credits - debits
        if expected_closing != summary["closing"]:
            raise StatementError(
                f"closing balance check failed: opening {summary['opening']:.2f} "
                f"+ credits {credits:.2f} - debits {debits:.2f} "
                f"= {expected_closing:.2f} != statement closing {summary['closing']:.2f}"
            )

    # 4. Row-by-row Running Balance check
    previous = summary.get("opening")
    for row in rows:
        row_credit = to_decimal(row["Credits"])
        row_debit = to_decimal(row["Debits"])
        row_balance = to_decimal(row["Running Balance"])
        if previous is not None:
            expected_balance = previous + row_credit - row_debit
            if row_balance != expected_balance:
                raise StatementError(
                    f"running balance check failed on {row['Date']}: "
                    f"expected {expected_balance:.2f}, got {row_balance:.2f}"
                )
        previous = row_balance


def statement_balances(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    """The statement's starting and ending balance, for the YNAB balance checks.

    Computed the same way as GCash:
      starting = first row's Running Balance - its Credits + its Debits
      ending   = last row's Running Balance
    The running-balance check guarantees these equal the printed Opening and
    Closing balances.

    Returns {"starting": Decimal, "ending": Decimal}, or {} if there are no rows.
    """
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    starting = (
        to_decimal(first["Running Balance"])
        - to_decimal(first["Credits"])
        + to_decimal(first["Debits"])
    )
    return {"starting": starting, "ending": to_decimal(last["Running Balance"])}


def summary(rows: list[dict[str, str]]) -> str:
    """One line shown under the parsed rows: the statement rows' totals.

    Example: "Total credit 1,000.00 · Total debit 249.00 · matches statement ✓"
    """
    credit = sum((to_decimal(r["Credits"]) for r in rows), Decimal("0.00"))
    debit = sum((to_decimal(r["Debits"]) for r in rows), Decimal("0.00"))
    return f"Total credit {credit:,.2f} · Total debit {debit:,.2f} · matches statement ✓"


# =============================================================================
# CONVERTING - rows -> neutral Transactions (registered in sources.py)
#
# Input row example:
#   {
#     "Date":            "05 Oct 2026",
#     "Details":         "Card payment at SHOPEE PH, Card •••• 1234",
#     "Credits":         "0.00",
#     "Debits":          "249.00",
#     "Running Balance": "4751.00"
#   }
#
# GoTyme has no reference numbers, so the Transaction key is
# "{Date}|{Credits}|{Debits}|{Running Balance}|{n}" where n counts earlier rows
# in the same statement with the same four values (almost always 0).
# YNAB's import_id is make_import_id("gotyme", key) (ynab_api/format.py).
# =============================================================================


def gotyme_payee(details: str) -> str:
    """Get a clean payee name from a GoTyme Details string.

    - "Card payment at MERCHANT, Card •••• NNNN" -> "MERCHANT"
    - "Transfer to/from GoalSave" -> "GoalSave"
    - "Transfer to/from NAME BANK, Account No. •••NNNN" -> text before ", Account No."
    - Anything else -> Details as is
    """
    card = GOTYME_CARD_PAYMENT_RE.match(details)
    if card:
        return card.group(1).strip()

    if GOTYME_GOALSAVE_RE.match(details):
        return "GoalSave"

    transfer = GOTYME_TRANSFER_ACCOUNT_RE.match(details)
    if transfer:
        return transfer.group(1).strip()

    return details


def to_transactions(rows: list[dict[str, str]]) -> list[Transaction]:
    """Convert GoTyme rows into neutral Transactions, one per row.

    Example:
        to_transactions([{"Date": "05 Oct 2026", "Details": "Qr Payment",
                          "Credits": "0.00", "Debits": "249.00",
                          "Running Balance": "4751.00"}])
    """
    counts: dict[str, int] = {}
    transactions = []
    for row in rows:
        base_key = f"{row['Date']}|{row['Credits']}|{row['Debits']}|{row['Running Balance']}"
        occurrence = counts.get(base_key, 0)
        counts[base_key] = occurrence + 1

        amount = to_decimal(row["Credits"]) - to_decimal(row["Debits"])
        transactions.append(
            Transaction(
                source="gotyme",
                key=f"{base_key}|{occurrence}",
                date=datetime.strptime(row["Date"], DATE_FORMAT).date(),
                payee=gotyme_payee(row["Details"]),
                amount=amount,
                memo="GoTyme: " + row["Details"],
                flagged=False,
            )
        )
    return transactions


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output
# =============================================================================


def main() -> None:
    # 1. Getting the PDF: read the file path and password from the CLI, env or prompt
    ap = argparse.ArgumentParser(description="Parse a GoTyme Everyday Deposit statement PDF.")
    ap.add_argument("pdf", help="Path to the GoTyme statement PDF")
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

    # 2. Parsing: open the PDF with the password and read the transaction table
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
