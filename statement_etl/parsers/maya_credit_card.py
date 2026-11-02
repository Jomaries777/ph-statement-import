"""
ph-statement-import - Maya Credit Card parser.

Parse a Maya credit card billing statement PDF (e.g.
"MayaCreditCard_SoA_<id>_2026OCT.pdf", emailed monthly).

Output fields per transaction (amounts without "PHP" or commas):
    Transaction date - e.g. "20 Sep 2026". The statement prints "20 Sep"; the
                       year comes from its period line
    Posting date     - e.g. "21 Sep 2026"
    Description      - e.g. "SAMPLE STORE QUEZON CITY PH", as printed
    Amount           - signed like the statement: a charge "2099.00",
                       a payment or credit "-2099.00" (printed "- 2,099.00")
    Balance          - what is owed after this row, e.g. "500.95". Not printed
                       on the statement: the previous balance plus every
                       Amount so far. The last row's is the statement's
                       balance: below 0 when the card is overpaid

to_transactions() turns these rows into neutral Transactions (transaction.py).

Most of page 1's labels ("Previous balance", "Total amount due", the table's
column names) are images, not text. Only the values can be read, by where
they are:
    - the right-hand column under "Billing date: 20 October 2026" lists the
      statement's balance in parts: the previous balance first, then the
      payments ("- 1,000.00 PHP"), purchases, fees and so on. The balance
      itself isn't printed
    - the amount due is on the line that starts with the due date:
      "9 Nov 2026 ... 1,234.56 PHP". It's the balance, or 0.00 when the card
      is overpaid
    - the rows come after the period line: "21 Sep 2026 to 20 Oct 2026"
So the checks are: the rows add up to the parts after the previous balance,
and the amount due matches the balance they give.

How it works:
    1. Open      - open the password-protected PDF                (open_pdf)
    2. Summary   - read the billing date, the balance's parts,
                   the amount due and the period                  (read_summary)
    3. Rows      - one row per line that starts with two dates:
                   "20 Sep 21 Sep SAMPLE STORE ... 2,099.00"      (read_rows)
    4. Check     - the rows add up to the parts; the amount due
                   matches the balance                            (check_totals)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.maya_credit_card statement.pdf
    python -m statement_etl.parsers.maya_credit_card statement.pdf -p PASSWORD -o out.json
"""

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.parsers.common import StatementError, group_lines, open_pdf, to_decimal
from statement_etl.transaction import Transaction, transaction_ref

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - Maya-card-specific; change these for another card
# =============================================================================

# Every column of the transaction table, in the order the PDF prints them,
# plus the computed Balance. Each parsed row is a dict with exactly these keys.
COLUMNS = ("Transaction date", "Posting date", "Description", "Amount", "Balance")

DATE_FORMAT = "%d %b %Y"  # "20 Sep 2026"

# Money amounts: "1,234.56", "-500.00"
AMOUNT = r"-?[\d,]+\.\d{2}"
AMOUNT_RE = re.compile(rf"^{AMOUNT}$")

# Page 1's values
BILLING_DATE_RE = re.compile(r"Billing date:\s*(\d{1,2} [A-Za-z]+ \d{4})")
SHORT_DATE = r"\d{1,2} [A-Z][a-z]{2} \d{4}"
PERIOD_RE = re.compile(rf"^({SHORT_DATE}) to ({SHORT_DATE})$")
DUE_LINE_RE = re.compile(rf"^{SHORT_DATE} ({AMOUNT}) PHP$")
# The right-hand column of amounts ("-500.00 PHP") starts right of this x (pt);
# the credit limits printed on the left don't
RIGHT_COLUMN_X = 400

# A row starts with two dates without a year: "20 Sep 21 Sep"
DAY_RE = re.compile(r"^\d{1,2}$")
MONTH_RE = re.compile(r"^[A-Z][a-z]{2}$")

# Maya prints a payment's minus sign as a word of its own, "- 2,099.00",
# just left of the amount: at most this far (pt) from it
SIGN_WORD = "-"
SIGN_GAP = 6
# A payment or credit printed as "2,099.00 CR" instead
CREDIT_MARK = "CR"


# =============================================================================
# PAGE LAYOUT - read page 1's values
# =============================================================================


def _date(text: str, fmt: str = DATE_FORMAT) -> date:
    return datetime.strptime(text, fmt).date()


def _read_amount(words: list[dict]) -> tuple[Decimal, int] | None:
    """The signed amount at the end of `words`, and how many words it takes:
    "2,099.00" -> (2099.00, 1), "- 2,099.00" -> (-2099.00, 2),
    "2,099.00 CR" -> (-2099.00, 2). None if they don't end with an amount."""
    texts = [w["text"] for w in words]
    if texts and texts[-1] == CREDIT_MARK:
        if len(texts) >= 2 and AMOUNT_RE.match(texts[-2]):
            return -abs(to_decimal(texts[-2])), 2
        return None
    if not texts or not AMOUNT_RE.match(texts[-1]):
        return None
    amount = to_decimal(texts[-1])
    if len(words) >= 2 and texts[-2] == SIGN_WORD and words[-1]["x0"] - words[-2]["x1"] <= SIGN_GAP:
        return -amount, 2
    return amount, 1


def read_summary(lines: list[dict]) -> dict:
    """Read page 1's values by where they are (their labels are images).

    Returns a dict with any of "billing_date", "start" and "end" (dates of the
    period line), "parts" (the right-hand column's amounts between the billing
    date and the due date, previous balance first) and "due" (Decimal) found
    on the page.
    """
    summary = {}
    parts = []
    for line in lines:
        text = line["text"]
        billing = BILLING_DATE_RE.search(text)
        period = PERIOD_RE.match(text)
        due = DUE_LINE_RE.match(text)
        if billing:
            summary["billing_date"] = _date(billing.group(1), "%d %B %Y")
        elif period:
            summary["start"], summary["end"] = _date(period.group(1)), _date(period.group(2))
        elif due:
            summary["due"] = to_decimal(due.group(1))
        elif "billing_date" in summary and "due" not in summary:
            # A right-hand "1,500.00 PHP" or "- 2,000.00 PHP": one of the parts
            words = line["words"]
            amount = _read_amount(words[:-1]) if words[-1]["text"] == "PHP" else None
            if amount is not None and words[-2]["x0"] > RIGHT_COLUMN_X:
                parts.append(amount[0])
    if parts:
        summary["parts"] = parts
    return summary


# =============================================================================
# ROW PARSING - one row per line that starts with two dates
# =============================================================================


def _row_date(day_month: str, end: date) -> str:
    """ "20 Sep" in a statement period ending `end` -> "20 Sep 2026": the end's
    year, or the year before for a month after the end's ("28 Dec" on a
    statement ending 20 Jan 2027 is 2026)."""
    day = _date(f"{day_month} {end.year}")
    if day.month > end.month:
        day = day.replace(year=end.year - 1)
    return f"{day.day} {day:%b %Y}"


def _parse_row_line(line: dict) -> dict | None:
    """Read "20 Sep 21 Sep DESCRIPTION 2,099.00", or a payment's
    "... - 2,099.00". None if the line isn't a row."""
    amount = _read_amount(line["words"])
    if amount is None:
        return None
    words = [w["text"] for w in line["words"][: -amount[1]]]
    if len(words) < 5:
        return None
    if not (DAY_RE.match(words[0]) and MONTH_RE.match(words[1])):
        return None
    if not (DAY_RE.match(words[2]) and MONTH_RE.match(words[3])):
        return None
    return {
        "transaction": f"{words[0]} {words[1]}",
        "posting": f"{words[2]} {words[3]}",
        "Description": " ".join(words[4:]),
        "Amount": amount[0],
    }


def read_rows(lines: list[dict]) -> list[dict]:
    """The rows on one page, with their dates still without a year."""
    return [row for row in map(_parse_row_line, lines) if row is not None]


# =============================================================================
# PARSING - go through every page, collect rows, and verify the amount due
# =============================================================================


def parse_transactions(
    pdf_path, password: str, on_page: Callable[[int, int], None] | None = None
) -> list[dict[str, str]]:
    """Parse a Maya credit card statement PDF into a list of row dicts.

    Each dict has keys equal to COLUMNS. on_page(page, pages), if given, is
    called as each page is read: (1, 5), (2, 5), ... Raises StatementError if
    the balance's parts, amount due or period can't be found, or they don't
    add up; PasswordError if the password is wrong.
    """
    raw_rows = []
    statement_summary = {}

    with open_pdf(pdf_path, password) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if on_page:
                on_page(number, len(pdf.pages))  # progress, for the page or the run log
            lines = group_lines(page.extract_words())

            # 1. Page 1's values
            statement_summary.update(read_summary(lines))

            # 2. Rows, on any page (pages 2+ are usually images of the terms)
            raw_rows.extend(read_rows(lines))

    for key, what in (
        ("parts", 'the previous balance (the first amount under "Billing date")'),
        ("due", 'the amount due (the line starting with the due date, "9 Nov 2026 ... PHP")'),
        ("end", 'the period ("21 Sep 2026 to 20 Oct 2026")'),
    ):
        if key not in statement_summary:
            raise StatementError(f"Could not find {what} on the statement.")

    # 3. Add the year to the dates, and the balance after each row
    rows = []
    balance = statement_summary["parts"][0]  # the previous balance
    for raw in raw_rows:
        balance += raw["Amount"]
        rows.append(
            {
                "Transaction date": _row_date(raw["transaction"], statement_summary["end"]),
                "Posting date": _row_date(raw["posting"], statement_summary["end"]),
                "Description": raw["Description"],
                "Amount": f"{raw['Amount']:.2f}",
                "Balance": f"{balance:.2f}",
            }
        )

    # 4. Verify the rows against the balance's parts and the amount due
    check_totals(rows, statement_summary)
    return rows


# =============================================================================
# CHECKING - verify the rows and the amount due
# =============================================================================


def _total(rows) -> Decimal:
    return sum((Decimal(r["Amount"]) for r in rows), Decimal("0.00"))


def check_totals(rows: list[dict[str, str]], summary: dict) -> None:
    """Verify the two Maya credit card checks; raise StatementError on a
    mismatch, e.g. if a row couldn't be read.

    1. sum(Amount) == the sum of the parts after the previous balance
    2. amount due == previous balance + sum(Amount), or 0.00 for an overpaid
       card (a balance below 0)
    """
    previous, changes = summary["parts"][0], summary["parts"][1:]
    total = _total(rows)
    expected = sum(changes, Decimal("0.00"))
    if total != expected:
        raise StatementError(
            f"the rows add up to {total:,.2f}, "
            f"but the statement's summary adds up to {expected:,.2f}"
        )
    balance = previous + total
    due = max(balance, Decimal("0.00"))
    if summary["due"] != due:
        raise StatementError(
            f"amount due check failed: previous balance {previous:,.2f} + amounts "
            f"{total:,.2f} = {balance:,.2f}, so the amount due should be {due:,.2f}, "
            f"not {summary['due']:,.2f}"
        )


def statement_balances(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    """The card's balance in YNAB before and after this statement, for the
    YNAB balance checks.

    The statement counts what is owed as positive; YNAB shows a credit card's
    debt as a negative balance, so both are flipped:
      starting = -(first row's Balance - its Amount)   (-previous balance)
      ending   = -(last row's Balance)                 (-the statement's balance)

    Returns {"starting": Decimal, "ending": Decimal}, or {} if there are no rows.
    """
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    previous = Decimal(first["Balance"]) - Decimal(first["Amount"])
    return {"starting": -previous, "ending": -Decimal(last["Balance"])}


def summary(rows: list[dict[str, str]]) -> str | None:
    """One line shown under the parsed rows: previous balance + amounts = ending balance.

    check_totals() already stopped parsing if this doesn't add up.
    """
    if not rows:
        return None
    previous = Decimal(rows[0]["Balance"]) - Decimal(rows[0]["Amount"])
    return (
        f"Previous balance {previous:,.2f} + amounts {_total(rows):,.2f} "
        f"= ending balance {Decimal(rows[-1]['Balance']):,.2f} · matches statement ✓"
    )


# =============================================================================
# CONVERTING - rows -> neutral Transactions (registered in sources.py)
#
# Input row example:
#   {
#     "Transaction date": "20 Sep 2026",
#     "Posting date":     "21 Sep 2026",
#     "Description":      "SAMPLE STORE QUEZON CITY PH",
#     "Amount":           "2099.00",
#     "Balance":          "500.95"
#   }
#
# The statement has no reference numbers, so the Transaction key is
# "{Transaction date}|{Posting date}|{Amount}|{Description}|{n}", where n
# counts earlier rows in the same statement with the same four values (almost
# always 0). YNAB's import_id is make_import_id("maya_credit_card", key)
# (ynab_api/format.py).
#
# The memo is the Maya Card Ref: transaction_ref("maya_credit_card", key), the
# hash part of that import_id, so searching it in YNAB finds the row.
# =============================================================================


def to_transactions(rows: list[dict[str, str]]) -> list[Transaction]:
    """Convert Maya credit card rows into neutral Transactions, one per row.

    A charge is money out of the card (a negative YNAB amount), a payment money in.
    """
    counts: dict[str, int] = {}
    transactions = []
    for row in rows:
        base_key = (
            f"{row['Transaction date']}|{row['Posting date']}|{row['Amount']}|{row['Description']}"
        )
        occurrence = counts.get(base_key, 0)
        counts[base_key] = occurrence + 1

        key = f"{base_key}|{occurrence}"
        transactions.append(
            Transaction(
                source="maya_credit_card",
                key=key,
                date=_date(row["Transaction date"]),
                payee=row["Description"],
                amount=-Decimal(row["Amount"]),
                memo=f"Maya Card Ref: {transaction_ref('maya_credit_card', key)}",
                flagged=False,
            )
        )
    return transactions


# =============================================================================
# PASSWORD - rule for the statement PDF (auto-import builds it from inputs)
# =============================================================================


def build_password(inputs: Mapping[str, str]) -> str:
    """Maya Credit Card PDF password: date of birth as DDMmmYYYY + last 6 of mobile.

    Takes the `Source.build_password` input dict, with the date as the page's
    date field sends it (YYYY-MM-DD):
        {"birth_date": "2000-01-01", "mobile_last6": "123456"} -> "01Jan2000123456"
    Raises ValueError for a date that doesn't exist.
    """
    return date.fromisoformat(inputs["birth_date"]).strftime("%d%b%Y") + inputs["mobile_last6"]


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output
# =============================================================================


def main() -> None:
    # 1. Getting the PDF password: -p, $MAYA_CREDIT_CARD_PDF_PASSWORD, or a prompt
    ap = argparse.ArgumentParser(description="Parse a Maya credit card statement PDF.")
    ap.add_argument("pdf", help="Path to the Maya credit card statement PDF")
    ap.add_argument(
        "-p", "--password", help="PDF password (defaults to $MAYA_CREDIT_CARD_PDF_PASSWORD)"
    )
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()
    password = (
        args.password
        or os.environ.get("MAYA_CREDIT_CARD_PDF_PASSWORD")
        or getpass.getpass("PDF password: ")
    )

    # 2. Parsing: open the PDF and read the transactions
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
