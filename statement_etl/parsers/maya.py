"""
ph-statement-import - Maya Wallet parser.

Parse a Maya Wallet statement PDF (e.g.
"MayaWallet_SoA_<id>_01-Sep-2026_30-Sep-2026.pdf", emailed monthly).

Output fields per transaction (amounts without "PHP" or commas):
    Date & Time      - e.g. "6 Sep 2026 09:15:30 AM"
    Transaction type - e.g. "Purchased", "Received from bank", "Paid bill"
    Details          - e.g. "SAMPLE SHOP", "Credit via InstaPay", or ""
    Reference ID     - Maya's reference, unique per transaction, e.g. "0A1B2C3D4E5F"
    Amount           - signed: "-500.00" money out, "1000.00" money in
    Running balance  - e.g. "1500.00"

The statement prints "Transaction type & details" as one column with two
lines: the type on the row's first line, the details under it. They become
two columns here, because the details are the payee and the type isn't.

to_transactions() turns these rows into neutral Transactions (transaction.py).

How it works:
    1. Open      - open the password-protected PDF                (open_pdf)
    2. Summary   - read the account summary: starting balance,
                   total incoming, total outgoing, ending balance (read_summary)
    3. Rows      - each transaction is two lines: "6 Sep 2026
                   Purchased 0A1B2C3D4E5F -PHP 500.00 PHP 1,500.00",
                   then "09:15:30 AM SAMPLE SHOP"                     (read_rows)
    4. Check     - verify total incoming, total outgoing, the
                   ending balance, and every row's running
                   balance                                        (check_totals)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.maya statement.pdf
    python -m statement_etl.parsers.maya statement.pdf -p PASSWORD -o out.json
"""

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.parsers.common import StatementError, group_lines, open_pdf, to_decimal
from statement_etl.transaction import Transaction

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - Maya-specific; change these for another e-wallet
# =============================================================================

# Every column of the transaction table, in the order the PDF prints them.
# Each parsed row is a dict with exactly these keys (see read_rows).
COLUMNS = (
    "Date & Time",
    "Transaction type",
    "Details",
    "Reference ID",
    "Amount",
    "Running balance",
)

# A row's first line starts with its date: "6 Sep 2026"
DATE_RE = re.compile(r"^\d{1,2} [A-Z][a-z]{2} \d{4}$")
DATE_FORMAT = "%d %b %Y"

# Its second line starts with the time: "09:15:30 AM"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
AM_PM = ("AM", "PM")

# Money amounts, each printed after a currency word: "-PHP 500.00", "PHP 1,000.00"
CURRENCY_RE = re.compile(r"^-?PHP$")
AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")

# The account summary: a line of labels, then a line of their four amounts
SUMMARY_LABELS_RE = re.compile(r"Starting balance.*Total incoming.*Total outgoing.*Ending balance")
SUMMARY_KEYS = ("starting", "incoming", "outgoing", "ending")

# The table ends here; nothing after it is a transaction
TABLE_END_RE = re.compile(r"^(Ending Balance|\*\* Nothing follows \*\*)")

# A details line wrapped onto a third line is at most this far (pt) below the
# line above it; anything further is another part of the page
WRAP_GAP = 25


# =============================================================================
# PAGE LAYOUT - amounts and the account summary
# =============================================================================


def _amounts(words: list[dict]) -> list[str]:
    """The amounts in a run of words, each with its currency word's sign:
    ["-PHP", "500.00", "PHP", "1,500.00"] -> ["-500.00", "1500.00"]."""
    amounts = []
    for currency, amount in zip(words, words[1:], strict=False):
        if CURRENCY_RE.match(currency["text"]) and AMOUNT_RE.match(amount["text"]):
            sign = "-" if currency["text"].startswith("-") else ""
            amounts.append(f"{sign}{to_decimal(amount['text']):.2f}")
    return amounts


def read_summary(lines: list[dict]) -> dict:
    """Read the account summary on a page.

    Returns {"starting", "incoming", "outgoing", "ending"} as Decimals, or {}
    if this page has no summary.
    """
    for label_line, amount_line in zip(lines, lines[1:], strict=False):
        if SUMMARY_LABELS_RE.search(label_line["text"]):
            amounts = _amounts(amount_line["words"])
            if len(amounts) == len(SUMMARY_KEYS):
                return {key: Decimal(a) for key, a in zip(SUMMARY_KEYS, amounts, strict=True)}
    return {}


# =============================================================================
# ROW PARSING - a date line, then a time + details line
# =============================================================================


def _parse_date_line(line: dict) -> dict | None:
    """Read a row's first line: "6 Sep 2026", the type, the Reference ID, then
    the amount and running balance. None if the line isn't one."""
    words = line["words"]
    if len(words) < 8 or not DATE_RE.match(" ".join(w["text"] for w in words[:3])):
        return None
    # The line ends with "[-]PHP amount PHP balance"
    amounts = _amounts(words[-4:])
    if len(amounts) != 2:
        return None
    reference, type_words = words[-5], words[3:-5]
    if not type_words:
        return None
    return {
        "date": " ".join(w["text"] for w in words[:3]),
        "time": "",
        "Transaction type": " ".join(w["text"] for w in type_words),
        "details": [],
        "Reference ID": reference["text"],
        "Amount": amounts[0],
        "Running balance": amounts[1],
        "ref_x0": reference["x0"],  # details lines end left of the Reference ID
    }


def _read_detail_line(row: dict, line: dict) -> None:
    """Add a line under a row: its time ("09:15:30 AM") and details, or more
    details wrapped onto another line."""
    words = line["words"]
    starts_with_time = len(words) >= 2 and TIME_RE.match(words[0]["text"])
    if not row["time"] and starts_with_time and words[1]["text"] in AM_PM:
        row["time"] = f"{words[0]['text']} {words[1]['text']}"
        words = words[2:]
    row["details"] += [w["text"] for w in words]


def read_rows(lines: list[dict]) -> list[dict[str, str]]:
    """The transactions on one page, each with keys equal to COLUMNS.

    A line that starts with a date is a row. The lines right under it, in the
    "Transaction type & details" column or left of it, are its time and
    details. The table ends at "Ending Balance" or "** Nothing follows **".
    """
    rows = []
    previous_top = None
    for line in lines:
        if TABLE_END_RE.search(line["text"]):
            break
        row = _parse_date_line(line)
        if row is not None:
            rows.append(row)
        elif (
            rows
            and line["top"] - previous_top <= WRAP_GAP
            and line["words"][-1]["x1"] < rows[-1]["ref_x0"]
        ):
            _read_detail_line(rows[-1], line)
        else:
            continue  # not part of the table: a page header, the column labels
        previous_top = line["top"]

    return [
        {
            "Date & Time": f"{r['date']} {r['time']}".strip(),
            "Transaction type": r["Transaction type"],
            "Details": " ".join(r["details"]),
            "Reference ID": r["Reference ID"],
            "Amount": r["Amount"],
            "Running balance": r["Running balance"],
        }
        for r in rows
    ]


# =============================================================================
# PARSING - go through every page, collect rows, and verify all totals
# =============================================================================


def parse_transactions(
    pdf_path, password: str, on_page: Callable[[int, int], None] | None = None
) -> list[dict[str, str]]:
    """Parse a Maya Wallet statement PDF into a list of row dicts.

    Each dict has keys equal to COLUMNS. on_page(page, pages), if given, is
    called as each page is read: (1, 2), (2, 2). Raises StatementError if any
    statement check fails, or PasswordError if the password is wrong.
    """
    rows = []
    statement_summary = {}

    with open_pdf(pdf_path, password) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if on_page:
                on_page(number, len(pdf.pages))  # progress, for the page or the run log
            lines = group_lines(page.extract_words())

            # 1. The account summary is on the first page
            statement_summary.update(read_summary(lines))

            # 2. Rows, on every page
            rows.extend(read_rows(lines))

    # 3. Verify total incoming, total outgoing, ending balance, and running balance
    check_totals(rows, statement_summary)
    return rows


# =============================================================================
# CHECKING - verify statement totals and every row's running balance
# =============================================================================


def _totals(rows) -> tuple[Decimal, Decimal]:
    """(incoming, outgoing) of the rows, both positive."""
    amounts = [Decimal(r["Amount"]) for r in rows]
    incoming = sum((a for a in amounts if a > 0), Decimal("0.00"))
    outgoing = sum((-a for a in amounts if a < 0), Decimal("0.00"))
    return incoming, outgoing


def check_totals(rows: list[dict[str, str]], summary: dict) -> None:
    """Verify the four Maya statement checks; raise StatementError on a mismatch.

    1. sum(positive Amount) == Total incoming
    2. sum(negative Amount) == Total outgoing
    3. Starting balance + incoming - outgoing == Ending balance
    4. Each row's Running balance == previous balance + Amount
    """
    incoming, outgoing = _totals(rows)

    # 1 + 2. Totals
    for name, parsed in (("incoming", incoming), ("outgoing", outgoing)):
        if name in summary and parsed != summary[name]:
            raise StatementError(
                f"parsed total {name} {parsed:,.2f} != statement total {summary[name]:,.2f}"
            )

    # 3. Starting + incoming - outgoing == ending
    if "starting" in summary and "ending" in summary:
        expected = summary["starting"] + incoming - outgoing
        if expected != summary["ending"]:
            raise StatementError(
                f"ending balance check failed: starting {summary['starting']:,.2f} "
                f"+ incoming {incoming:,.2f} - outgoing {outgoing:,.2f} "
                f"= {expected:,.2f} != statement ending {summary['ending']:,.2f}"
            )

    # 4. Row by row
    previous = summary.get("starting")
    for row in rows:
        balance = Decimal(row["Running balance"])
        if previous is not None and balance != previous + Decimal(row["Amount"]):
            raise StatementError(
                f"running balance check failed on {row['Date & Time']}: "
                f"expected {previous + Decimal(row['Amount']):,.2f}, got {balance:,.2f}"
            )
        previous = balance


def statement_balances(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    """The statement's starting and ending balance, for the YNAB balance checks.

      starting = first row's Running balance - its Amount
      ending   = last row's Running balance
    The running-balance check makes these equal the printed Starting balance
    and Ending balance.

    Returns {"starting": Decimal, "ending": Decimal}, or {} if there are no rows.
    """
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    starting = Decimal(first["Running balance"]) - Decimal(first["Amount"])
    return {"starting": starting, "ending": Decimal(last["Running balance"])}


def summary(rows: list[dict[str, str]]) -> str:
    """One line shown under the parsed rows: the rows' totals.

    check_totals() already stopped parsing if these differ from the
    statement's printed totals, so they always match it here.
    """
    incoming, outgoing = _totals(rows)
    return f"Total incoming {incoming:,.2f} · Total outgoing {outgoing:,.2f} · matches statement ✓"


# =============================================================================
# CONVERTING - rows -> neutral Transactions (registered in sources.py)
#
# Input row example:
#   {
#     "Date & Time":      "6 Sep 2026 09:15:30 AM",
#     "Transaction type": "Purchased",
#     "Details":          "SAMPLE SHOP",
#     "Reference ID":     "0A1B2C3D4E5F",
#     "Amount":           "-500.00",
#     "Running balance":  "1500.00"
#   }
#
# The Transaction key is the Reference ID, unique per transaction. YNAB's
# import_id is make_import_id("maya", key) (ynab_api/format.py).
# =============================================================================


def _date_part(date_time: str) -> str:
    """ "6 Sep 2026 09:15:30 AM" -> "6 Sep 2026" """
    return " ".join(date_time.split()[:3])


def to_transactions(rows: list[dict[str, str]]) -> list[Transaction]:
    """Convert Maya Wallet rows into neutral Transactions, one per row.

    The payee is the row's details ("Credit via InstaPay"), or its type when a
    row has none; the memo is its Reference ID.
    """
    return [
        Transaction(
            source="maya",
            key=row["Reference ID"],
            date=datetime.strptime(_date_part(row["Date & Time"]), DATE_FORMAT).date(),
            payee=row["Details"] or row["Transaction type"],
            amount=Decimal(row["Amount"]),
            memo=f"Maya Ref: {row['Reference ID']}",
            flagged=False,
        )
        for row in rows
    ]


# =============================================================================
# PASSWORD - rule for the statement PDF (auto-import builds it from inputs)
# =============================================================================


def build_password(inputs: Mapping[str, str]) -> str:
    """Maya Wallet PDF password: first name's initial + last name (one capital) + last 5 of mobile.

    Takes the `Source.build_password` input dict:
        {"first_name": "Juan", "last_name": "De La Cruz", "mobile_last5": "12345"}
        -> "JDelacruz12345"
    """
    first_name = inputs["first_name"].strip()
    last_name = inputs["last_name"].replace(" ", "")
    return first_name[:1].upper() + last_name.capitalize() + inputs["mobile_last5"]


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output
# =============================================================================


def main() -> None:
    # 1. Getting the PDF password: -p, $MAYA_PDF_PASSWORD, or a prompt
    ap = argparse.ArgumentParser(description="Parse a Maya Wallet statement PDF.")
    ap.add_argument("pdf", help="Path to the Maya Wallet statement PDF")
    ap.add_argument("-p", "--password", help="PDF password (defaults to $MAYA_PDF_PASSWORD)")
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()
    password = (
        args.password or os.environ.get("MAYA_PDF_PASSWORD") or getpass.getpass("PDF password: ")
    )

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
