"""
ph-statement-import - BPI savings account parser.

Parse a BPI savings account statement PDF (e.g. "SA20261027.pdf", which BPI
online banking downloads without a password). BPI doesn't email it, so this
source has no Gmail auto-import: upload it on the manual upload page.

Output fields per transaction (same columns as the PDF table, amounts without commas):
    Date           - e.g. "Jul 27, 2026". The statement prints "Jul 27"; the
                     year comes from its PERIOD COVERED line
    Description    - e.g. "InstaPay Transfer", "3448 EOL/MOBILE BNKG PAYMENT"
    Ref            - BPI's short branch / channel code, e.g. "0010", or "".
                     Not unique per transaction
    Details        - e.g. "TRANSFER TO OTHER BANK", "TO: BPI CREDIT CARD.....XXXXX12345"
    Debit Amount   - amount text without commas, e.g. "20000.00", or "" if blank
    Credit Amount  - amount text without commas, or "" if blank
    Balance        - amount text without commas, e.g. "10877.92"

to_transactions() turns these rows into neutral Transactions (transaction.py).

How it works:
    1. Open      - open the PDF (BPI's has no password)           (open_pdf)
    2. Lines     - group the words on each page into text lines,
                   with a smaller gap than other sources: BPI
                   prints words close together ("InstaPay
                   Transfer" would read "InstaPayTransfer")       (page_lines)
    3. Summary   - read PERIOD COVERED, BEGINNING BALANCE,
                   BALANCE THIS STATEMENT, TOTAL DEBIT and
                   TOTAL CREDIT                                   (read_summary)
    4. Header    - find the table header on each page to learn
                   the column edges                               (find_columns)
    5. Rows      - one row per line that starts with a date;
                   a line without one is wrapped text, joined
                   to the row above it                            (read_rows)
    6. Check     - verify total debit, total credit, the ending
                   balance, and every row's running balance       (check_totals)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.bpi statement.pdf
    python -m statement_etl.parsers.bpi statement.pdf -o out.json
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.parsers.common import StatementError, group_lines, open_pdf, to_decimal
from statement_etl.transaction import Transaction, transaction_ref

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - BPI-specific; change these for another bank
# =============================================================================

# Every column of the transaction table, in the order the PDF prints them.
# Each parsed row is a dict with exactly these keys (see to_output_row).
COLUMNS = (
    "Date",
    "Description",
    "Ref",
    "Details",
    "Debit Amount",
    "Credit Amount",
    "Balance",
)

# Largest gap (pt) between letters of one word. BPI's words are ~3 pt apart,
# and its spaced-out capitals ~2 pt, so pdfplumber's default (3) joins words.
WORD_GAP = 2.5

# Words of the table header: "DATE DESCRIPTION REF DETAILS DEBIT AMOUNT CREDIT AMOUNT BALANCE"
HEADER_LABELS = ("DATE", "DESCRIPTION", "REF", "DETAILS", "DEBIT", "CREDIT", "BALANCE")

# A Ref code is printed under the "REF" label; Details start a few pt to its right
REF_SLACK = 4.0

# How the date is written at the start of each row: "Jul 27" (no year)
MONTH_RE = re.compile(r"^[A-Za-z]{3}$")
DAY_RE = re.compile(r"^\d{1,2}$")
DATE_FORMAT = "%b %d, %Y"  # the Date column, with the year added: "Jul 27, 2026"

# Money amounts: "1,234.56"
AMOUNT = r"-?[\d,]+\.\d{2}"
AMOUNT_RE = re.compile(rf"^{AMOUNT}$")

# Summary lines, around the table
PERIOD_RE = re.compile(
    r"PERIOD\s*COVERED\s*([A-Z]{3})\s*(\d{1,2}),\s*(\d{4})\s*-\s*([A-Z]{3})\s*(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)
BEGINNING_RE = re.compile(rf"^BEGINNING\s*BALANCE\s+({AMOUNT})$", re.IGNORECASE)
ENDING_RE = re.compile(rf"^BALANCE\s*THIS\s*STATEMENT\s+({AMOUNT})$", re.IGNORECASE)
TOTAL_DEBIT_RE = re.compile(rf"^TOTAL\s*DEBIT\s+({AMOUNT})$", re.IGNORECASE)
TOTAL_CREDIT_RE = re.compile(rf"^TOTAL\s*CREDIT\s+({AMOUNT})$", re.IGNORECASE)

# The table ends at the ending balance; what follows are totals and notes
TABLE_END_RE = re.compile(r"^(BALANCE\s*THIS\s*STATEMENT|TOTAL\s*DEBIT)\b", re.IGNORECASE)

# Payee patterns:
#   Details "TO: BPI CREDIT CARD.....XXXXX12345"  -> "BPI CREDIT CARD"
#   Description "3448 EOL/MOBILE BNKG PAYMENT"    -> "EOL/MOBILE BNKG PAYMENT"
BPI_PAID_TO_RE = re.compile(r"^TO:\s*(.+?)\s*\.{3,}", re.IGNORECASE)
BPI_CHANNEL_CODE_RE = re.compile(r"^\d+\s+")


# =============================================================================
# PAGE LAYOUT - read the summary lines, find column edges
# =============================================================================


def page_lines(page_or_lines) -> list[dict]:
    """A page's text lines (common.group_lines), read with BPI's WORD_GAP.
    Pre-grouped lines are returned as they are (for tests)."""
    if isinstance(page_or_lines, list):
        return page_or_lines
    return group_lines(page_or_lines.extract_words(x_tolerance=WORD_GAP))


def _period_date(month: str, day: str, year: str) -> date:
    return datetime.strptime(f"{month.title()} {day} {year}", "%b %d %Y").date()


def read_summary(lines: list[dict]) -> dict:
    """Read the summary lines on a page.

    Returns a dict with any of {"start", "end"} (dates of PERIOD COVERED) and
    {"beginning", "ending", "debit", "credit"} (Decimals) found on the page.
    """
    summary = {}
    for line in lines:
        text = line["text"]
        period = PERIOD_RE.search(text)
        if period:
            summary["start"] = _period_date(*period.group(1, 2, 3))
            summary["end"] = _period_date(*period.group(4, 5, 6))
        for key, pattern in (
            ("beginning", BEGINNING_RE),
            ("ending", ENDING_RE),
            ("debit", TOTAL_DEBIT_RE),
            ("credit", TOTAL_CREDIT_RE),
        ):
            match = pattern.search(text)
            if match:
                summary[key] = to_decimal(match.group(1))
    return summary


def find_columns(lines: list[dict]) -> dict | None:
    """Locate the table header on a page and return its column edges, or None
    if this page has no table."""
    for line in lines:
        by_text = {w["text"].upper(): w for w in line["words"]}
        if not set(HEADER_LABELS).issubset(by_text):
            continue
        return {
            "header_top": line["top"],
            "ref_x0": by_text["REF"]["x0"],
            "ref_x1": by_text["REF"]["x1"],
            "credit_x0": by_text["CREDIT"]["x0"],
        }
    return None


# =============================================================================
# ROW PARSING - one row per date line, wrapped text joined to it
# =============================================================================


def _column_of(word: dict, columns: dict) -> str:
    """Which text column a word between the date and the amounts is in.

    Description is left-aligned at the row's start, the Ref code sits under
    the "REF" label, and Details start just right of it.
    """
    if word["x1"] < columns["ref_x0"]:
        return "Description"
    if word["x1"] <= columns["ref_x1"] + REF_SLACK:
        return "Ref"
    return "Details"


def _parse_date_line(line: dict, columns: dict) -> dict | None:
    """Read a transaction line: "Jul 27", text, then two or three amounts
    (Debit and/or Credit, then Balance). None if the line isn't one."""
    words = line["words"]
    if len(words) < 4 or not (MONTH_RE.match(words[0]["text"]) and DAY_RE.match(words[1]["text"])):
        return None

    amounts = []  # the amounts at the end of the line
    for w in reversed(words[2:]):
        if not AMOUNT_RE.match(w["text"]):
            break
        amounts.insert(0, w)
    if not 2 <= len(amounts) <= 3:
        return None

    row = {
        "month_day": f"{words[0]['text']} {words[1]['text']}",
        "words": {"Description": [], "Ref": [], "Details": []},
        "Debit Amount": "",
        "Credit Amount": "",
        "Balance": f"{to_decimal(amounts[-1]['text']):.2f}",
    }
    # Debit amounts end left of the "CREDIT AMOUNT" label; credit amounts under it
    for w in amounts[:-1]:
        column = "Debit Amount" if w["x1"] <= columns["credit_x0"] else "Credit Amount"
        row[column] = f"{to_decimal(w['text']):.2f}"
    for w in words[2 : len(words) - len(amounts)]:
        row["words"][_column_of(w, columns)].append(w)
    return row


def read_rows(lines: list[dict], columns: dict) -> list[dict]:
    """The table's rows on one page, between the header and the table's end.

    A line that starts with a date is a row. Any other line (not a summary
    line) is wrapped text: its words join the row above, in their columns.
    """
    rows = []
    for line in lines:
        if line["top"] <= columns["header_top"]:
            continue
        if TABLE_END_RE.search(line["text"]):
            break
        row = _parse_date_line(line, columns)
        if row is not None:
            rows.append(row)
        elif rows and not BEGINNING_RE.search(line["text"]):
            for w in line["words"]:
                rows[-1]["words"][_column_of(w, columns)].append(w)
    return rows


def _row_date(month_day: str, start: date) -> date:
    """The date of "Jul 27" in a statement period starting `start`: the start's
    year, or the next one for a period that crosses New Year ("Jan 05" in
    a period from Dec 14)."""
    day = datetime.strptime(f"{month_day} {start.year}", "%b %d %Y").date()
    return day if day >= start else day.replace(year=start.year + 1)


def to_output_row(row: dict, start: date) -> dict[str, str]:
    """Build the final output row with keys equal to COLUMNS."""

    def text(column):
        return " ".join(w["text"] for w in row["words"][column])

    return {
        "Date": _row_date(row["month_day"], start).strftime(DATE_FORMAT),
        "Description": text("Description"),
        "Ref": text("Ref"),
        "Details": text("Details"),
        "Debit Amount": row["Debit Amount"],
        "Credit Amount": row["Credit Amount"],
        "Balance": row["Balance"],
    }


# =============================================================================
# PARSING - go through every page, collect rows, and verify all totals
# =============================================================================


def parse_transactions(
    pdf_path, password: str = "", on_page: Callable[[int, int], None] | None = None
) -> list[dict[str, str]]:
    """Parse a BPI savings statement PDF into a list of row dicts.

    Each dict has keys equal to COLUMNS. on_page(page, pages), if given, is
    called as each page is read: (1, 2), (2, 2). Raises StatementError if the
    statement has no PERIOD COVERED line, or any statement check fails.
    """
    raw_rows = []
    statement_summary = {}

    with open_pdf(pdf_path, password) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if on_page:
                on_page(number, len(pdf.pages))  # progress, for the page or the run log
            lines = page_lines(page)

            # 1. Summary lines (period, balances, totals) may be on any page
            statement_summary.update(read_summary(lines))

            # 2. Find the table header on this page; skip pages with no table
            columns = find_columns(lines)
            if columns is not None:
                raw_rows.extend(read_rows(lines, columns))

    if raw_rows and "start" not in statement_summary:
        raise StatementError("Could not find the statement's PERIOD COVERED, for the dates' year.")
    rows = [to_output_row(r, statement_summary["start"]) for r in raw_rows]

    # 3. Verify total debit, total credit, ending balance, and running balance
    check_totals(rows, statement_summary)
    return rows


# =============================================================================
# CHECKING - verify statement totals and every row's running balance
# =============================================================================


def _total(rows, column) -> Decimal:
    return sum((to_decimal(r[column]) for r in rows), Decimal("0.00"))


def check_totals(rows: list[dict[str, str]], summary: dict) -> None:
    """Verify the four BPI statement checks; raise StatementError on a mismatch.

    1. sum(Debit Amount) == TOTAL DEBIT
    2. sum(Credit Amount) == TOTAL CREDIT
    3. BEGINNING BALANCE - debits + credits == BALANCE THIS STATEMENT
    4. Each row's Balance == previous balance - Debit Amount + Credit Amount
    """
    debits, credits = _total(rows, "Debit Amount"), _total(rows, "Credit Amount")

    # 1 + 2. Totals
    for name, parsed in (("debit", debits), ("credit", credits)):
        if name in summary and parsed != summary[name]:
            raise StatementError(
                f"parsed total {name} {parsed:,.2f} != statement total {summary[name]:,.2f}"
            )

    # 3. Beginning - debits + credits == ending
    if "beginning" in summary and "ending" in summary:
        expected = summary["beginning"] - debits + credits
        if expected != summary["ending"]:
            raise StatementError(
                f"ending balance check failed: beginning {summary['beginning']:,.2f} "
                f"- debits {debits:,.2f} + credits {credits:,.2f} "
                f"= {expected:,.2f} != statement ending {summary['ending']:,.2f}"
            )

    # 4. Row by row
    previous = summary.get("beginning")
    for row in rows:
        balance = to_decimal(row["Balance"])
        if previous is not None:
            expected = previous - to_decimal(row["Debit Amount"]) + to_decimal(row["Credit Amount"])
            if balance != expected:
                raise StatementError(
                    f"running balance check failed on {row['Date']}: "
                    f"expected {expected:,.2f}, got {balance:,.2f}"
                )
        previous = balance


def statement_balances(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    """The statement's starting and ending balance, for the YNAB balance checks.

    Computed the same way as GCash and GoTyme:
      starting = first row's Balance + its Debit Amount - its Credit Amount
      ending   = last row's Balance
    The running-balance check makes these equal the printed BEGINNING BALANCE
    and BALANCE THIS STATEMENT.

    Returns {"starting": Decimal, "ending": Decimal}, or {} if there are no rows.
    """
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    starting = (
        to_decimal(first["Balance"])
        + to_decimal(first["Debit Amount"])
        - to_decimal(first["Credit Amount"])
    )
    return {"starting": starting, "ending": to_decimal(last["Balance"])}


def summary(rows: list[dict[str, str]]) -> str:
    """One line shown under the parsed rows: the statement rows' totals.

    check_totals() already stopped parsing if these differ from the
    statement's printed totals, so they always match it here.
    """
    debit, credit = _total(rows, "Debit Amount"), _total(rows, "Credit Amount")
    return f"Total debit {debit:,.2f} · Total credit {credit:,.2f} · matches statement ✓"


# =============================================================================
# CONVERTING - rows -> neutral Transactions (registered in sources.py)
#
# Input row example:
#   {
#     "Date":          "Aug 03, 2026",
#     "Description":   "Bills Payment",
#     "Ref":           "0010",
#     "Details":       "TO: SAMPLE CREDIT CARD.....XXXXX12345",
#     "Debit Amount":  "10000.00",
#     "Credit Amount": "",
#     "Balance":       "7817.92"
#   }
#
# BPI has no per-transaction reference numbers (Ref repeats), so the
# Transaction key is "{Date}|{Debit Amount}|{Credit Amount}|{Balance}|{n}",
# where n counts earlier rows in the same statement with the same four values
# (almost always 0). YNAB's import_id is make_import_id("bpi", key)
# (ynab_api/format.py).
#
# The memo is the BPI Ref: transaction_ref("bpi", key), the hash part of
# that import_id, so searching it in YNAB finds the row.
# =============================================================================


def bpi_payee(row: dict[str, str]) -> str:
    """Get a clean payee name from a BPI row.

    - Details "TO: NAME.....XXXXX12345" -> "NAME"
    - otherwise the Description, without a leading channel code:
      "3448 EOL/MOBILE BNKG PAYMENT" -> "EOL/MOBILE BNKG PAYMENT"
    """
    paid_to = BPI_PAID_TO_RE.match(row["Details"])
    if paid_to:
        return paid_to.group(1)
    return BPI_CHANNEL_CODE_RE.sub("", row["Description"]) or row["Details"]


def to_transactions(rows: list[dict[str, str]]) -> list[Transaction]:
    """Convert BPI rows into neutral Transactions, one per row."""
    counts: dict[str, int] = {}
    transactions = []
    for row in rows:
        base_key = f"{row['Date']}|{row['Debit Amount']}|{row['Credit Amount']}|{row['Balance']}"
        occurrence = counts.get(base_key, 0)
        counts[base_key] = occurrence + 1

        key = f"{base_key}|{occurrence}"
        transactions.append(
            Transaction(
                source="bpi",
                key=key,
                date=datetime.strptime(row["Date"], DATE_FORMAT).date(),
                payee=bpi_payee(row),
                amount=to_decimal(row["Credit Amount"]) - to_decimal(row["Debit Amount"]),
                memo=f"BPI Ref: {transaction_ref('bpi', key)}",
                flagged=False,
            )
        )
    return transactions


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output
# =============================================================================


def main() -> None:
    # 1. Getting the PDF: BPI's has no password, unless $BPI_PDF_PASSWORD or -p says so
    ap = argparse.ArgumentParser(description="Parse a BPI savings account statement PDF.")
    ap.add_argument("pdf", help="Path to the BPI statement PDF")
    ap.add_argument("-p", "--password", help="PDF password, if any (defaults to $BPI_PDF_PASSWORD)")
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()
    password = args.password or os.environ.get("BPI_PDF_PASSWORD", "")

    # 2. Parsing: open the PDF and read the transaction table
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
