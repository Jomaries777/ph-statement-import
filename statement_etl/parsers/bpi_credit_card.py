"""
ph-statement-import - BPI Credit Card parser.

Parse a BPI credit card statement PDF (e.g. "BPI eStatement-October 15, 2026.pdf",
emailed monthly). One statement bills every card on the account: the
principal card and any supplementary ones, all in one YNAB account.

Output fields per transaction (amounts without commas):
    Transaction Date - e.g. "Sep 20, 2026". The statement prints
                       "September 20"; the year comes from its STATEMENT DATE.
                       A row printed without dates (a finance charge) gets
                       the statement date, when BPI charged it
    Post Date        - e.g. "Sep 21, 2026"
    Description      - e.g. "SAMPLE STORE  Taguig": the merchant, then two
                       spaces, then what BPI prints after it (the city, or an
                       installment's "Qu: 3/24")
    Amount           - signed like the statement: a charge "2801.61",
                       a payment "-60000.00"
    Balance          - what is owed after this row. Not printed on the
                       statement: the previous balance plus every Amount so
                       far, so the last row's is the ending balance

to_transactions() turns these rows into neutral Transactions (transaction.py).

BPI's PDF text is EBCDIC, the IBM mainframe encoding: the font gives each
letter its EBCDIC code, so pdfplumber reads "Prepared for" as
"(cid:215)(cid:153)(cid:133)..." and digits as "æ", "ø" and the like.
decode_chars() turns each one back into the letter it shows.

How it works:
    1. Open      - open the password-protected PDF                (open_pdf)
    2. Decode    - turn each EBCDIC char into its letter          (decode_chars)
    3. Lines     - group the letters into words and lines         (page_lines)
    4. Summary   - read STATEMENT DATE, TOTAL AMOUNT DUE,
                   Previous Balance and Ending Balance            (read_summary)
    5. Header    - find "Transaction Post Date Description Amount"
                   to learn the column edges                      (find_columns)
    6. Rows      - every line under it with an Amount, except
                   the balance lines                              (read_rows)
    7. Check     - Previous Balance + amounts == Ending Balance
                   == TOTAL AMOUNT DUE                            (check_totals)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.bpi_credit_card statement.pdf
    python -m statement_etl.parsers.bpi_credit_card statement.pdf -p YYYYMMDD -o out.json
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
from pdfminer.glyphlist import glyphname2unicode
from pdfminer.latin_enc import ENCODING

from statement_etl.parsers.common import StatementError, group_lines, open_pdf, to_decimal
from statement_etl.transaction import Transaction, transaction_ref

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - BPI-card-specific; change these for another card
# =============================================================================

# Every column of the transaction table, in the order the PDF prints them,
# plus the computed Balance. Each parsed row is a dict with exactly these keys.
COLUMNS = ("Transaction Date", "Post Date", "Description", "Amount", "Balance")

DATE_FORMAT = "%b %d, %Y"  # the date columns: "Sep 20, 2026"

# Money amounts: "1,234.56", "-60,000.00"
AMOUNT = r"-?[\d,]+\.\d{2}"
AMOUNT_RE = re.compile(rf"^{AMOUNT}$")

# Summary lines
STATEMENT_DATE_RE = re.compile(r"STATEMENT DATE\s+([A-Z]+ \d{1,2}, \d{4})", re.IGNORECASE)
TOTAL_DUE_RE = re.compile(rf"^TOTAL AMOUNT DUE\s+({AMOUNT})$", re.IGNORECASE)
# Lines in the transaction table that are balances, not transactions
BALANCE_LINES = {
    "Previous Balance": "previous",
    "Past Due": "past_due",
    "Ending Balance": "ending",
    "Unbilled Installment Amount": "unbilled",
}

# Words of the table header: "Transaction Post Date Description Amount"
HEADER_LABELS = ("Transaction", "Post", "Description", "Amount")

# A row's date: "September 20"
ROW_DATE_RE = re.compile(r"^([A-Z][a-z]+) (\d{1,2})$")

# Letters of one word are at most this far apart (pt); a space is a char of its own
LETTER_GAP = 1.0
# Words further apart than this (pt) have a run of spaces between them: BPI
# pads the merchant's name to a fixed width before the city. One space is 2.6 pt.
WIDE_GAP = 6.0


# =============================================================================
# DECODING - EBCDIC chars -> letters
#
# pdfplumber gives every char as a dict with its position and "text". On
# BPI's pages "text" is either "(cid:N)", where N is the EBCDIC code, or the
# glyph Adobe's StandardEncoding gives that code ("æ" is code 0xF1, which in
# EBCDIC is "1"). Both turn into the code, and the code into the letter with
# Python's EBCDIC codec, cp037:
#     "(cid:215)" -> 215 -> "P"        "æ" -> 0xF1 -> "1"        "@" -> 0x40 -> " "
# =============================================================================

CID_RE = re.compile(r"^\(cid:(\d+)\)$")
EBCDIC = "cp037"

# StandardEncoding glyph -> its code
_STANDARD_CODES = {}
for _name, _standard_code, *_ in ENCODING:
    if _standard_code is not None and _name in glyphname2unicode:
        _STANDARD_CODES.setdefault(glyphname2unicode[_name], _standard_code)


def _is_ebcdic(chars: list[dict]) -> bool:
    """True for a page in BPI's EBCDIC font: lowercase EBCDIC letters have no
    StandardEncoding glyph, so pdfplumber shows them as "(cid:N)". A page with
    plain text has none."""
    return any(CID_RE.match(c["text"]) for c in chars)


def _decode(text: str) -> str:
    cid = CID_RE.match(text)
    code = int(cid.group(1)) if cid else _STANDARD_CODES.get(text)
    if code is None or code > 255:
        return text
    return bytes([code]).decode(EBCDIC)


def decode_chars(chars: list[dict]) -> list[dict]:
    """The page's chars with their letters: each EBCDIC char decoded, or all
    of them as they are if the page is plain text."""
    if not _is_ebcdic(chars):
        return chars
    return [{**c, "text": _decode(c["text"])} for c in chars]


# =============================================================================
# PAGE LAYOUT - words and lines, the summary, column edges
# =============================================================================


def _words(chars: list[dict]) -> list[dict]:
    """Group chars into words: a space, a new line or a gap ends a word."""
    words = []
    current = None
    for c in sorted(chars, key=lambda c: (round(c["top"]), c["x0"])):
        if c["text"].isspace():
            current = None
            continue
        if (
            current is not None
            and round(c["top"]) == round(current["top"])
            and c["x0"] - current["x1"] <= LETTER_GAP
        ):
            current["text"] += c["text"]
            current["x1"] = c["x1"]
        else:
            current = {"text": c["text"], "x0": c["x0"], "x1": c["x1"], "top": c["top"]}
            words.append(current)
    return words


def page_lines(page) -> list[dict]:
    """A page's text lines (common.group_lines), from its decoded chars."""
    return group_lines(_words(decode_chars(page.chars)))


def _statement_date(text: str) -> date:
    return datetime.strptime(text.title(), "%B %d, %Y").date()


def read_summary(lines: list[dict]) -> dict:
    """Read the summary lines on a page.

    Returns a dict with any of "statement_date", "total_due" and the
    BALANCE_LINES values ("previous", "ending", ...) found on the page.
    """
    summary = {}
    for line in lines:
        text = line["text"]
        statement_date = STATEMENT_DATE_RE.search(text)
        total_due = TOTAL_DUE_RE.search(text)
        if statement_date:
            summary["statement_date"] = _statement_date(statement_date.group(1))
        elif total_due:
            summary["total_due"] = to_decimal(total_due.group(1))
        else:
            for label, key in BALANCE_LINES.items():
                match = re.fullmatch(rf"{label} ({AMOUNT})", text)
                if match:
                    summary[key] = to_decimal(match.group(1))
    return summary


def find_columns(lines: list[dict]) -> dict | None:
    """Locate the transaction table's header and return its column edges, or
    None if this page has no table."""
    for line in lines:
        by_text = {w["text"]: w for w in line["words"]}
        if set(HEADER_LABELS).issubset(by_text) and line["words"][0]["text"] == "Transaction":
            return {
                "header_top": line["top"],
                "post_x0": by_text["Post"]["x0"],
                "description_x0": by_text["Description"]["x0"],
            }
    return None


# =============================================================================
# ROW PARSING - every line with an Amount under the header
# =============================================================================


def _description(words: list[dict]) -> str:
    """The Description's words joined, with two spaces where BPI printed a run
    of them (between the merchant and the city)."""
    text = ""
    for previous, w in zip([None, *words], words, strict=False):
        if previous is not None:
            text += "  " if w["x0"] - previous["x1"] > WIDE_GAP else " "
        text += w["text"]
    return text


def _parse_row_line(line: dict, columns: dict) -> dict | None:
    """Read "September 20 September 21 DESCRIPTION 2,801.61", or a line
    without dates. None if the line has no Amount, or is a balance line."""
    words = line["words"]
    if len(words) < 2 or not AMOUNT_RE.match(words[-1]["text"]):
        return None
    if " ".join(w["text"] for w in words[:-1]) in BALANCE_LINES:
        return None
    by_column = {"transaction": [], "post": [], "description": []}
    for w in words[:-1]:
        if w["x0"] < columns["post_x0"] - 2:
            by_column["transaction"].append(w["text"])
        elif w["x0"] < columns["description_x0"] - 2:
            by_column["post"].append(w["text"])
        else:
            by_column["description"].append(w)
    return {
        "transaction": " ".join(by_column["transaction"]),
        "post": " ".join(by_column["post"]),
        "Description": _description(by_column["description"]),
        "Amount": to_decimal(words[-1]["text"]),
    }


def read_rows(lines: list[dict], columns: dict) -> list[dict]:
    """The rows on one page under the header, with their dates as printed.

    Lines without an Amount (each card's "<card no.> - NAME" heading,
    "Installment Amortization:") are skipped, and so are the balance lines.
    """
    rows = []
    for line in lines:
        if line["top"] <= columns["header_top"]:
            continue
        row = _parse_row_line(line, columns)
        if row is not None:
            rows.append(row)
    return rows


def _row_date(month_day: str, statement_date: date) -> str:
    """ "September 20" on a statement dated `statement_date` -> "Sep 20, 2026":
    its year, or the year before for a month after its month ("December 28"
    on a statement of January 15, 2027 is 2026). "" -> the statement date."""
    if not month_day:
        return statement_date.strftime(DATE_FORMAT)
    match = ROW_DATE_RE.match(month_day)
    if not match:
        raise StatementError(f"Could not read the date {month_day!r} on the statement.")
    day = datetime.strptime(f"{match.group(1)} {match.group(2)} {statement_date.year}", "%B %d %Y")
    if day.month > statement_date.month:
        day = day.replace(year=statement_date.year - 1)
    return day.strftime(DATE_FORMAT)


# =============================================================================
# PARSING - go through every page, collect rows, and verify the totals
# =============================================================================


def parse_transactions(
    pdf_path, password: str, on_page: Callable[[int, int], None] | None = None
) -> list[dict[str, str]]:
    """Parse a BPI credit card statement PDF into a list of row dicts.

    Each dict has keys equal to COLUMNS. on_page(page, pages), if given, is
    called as each page is read: (1, 6), (2, 6), ... Raises StatementError if
    the statement date or balances can't be found, or they don't add up;
    PasswordError if the password is wrong.
    """
    raw_rows = []
    statement_summary = {}

    with open_pdf(pdf_path, password) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if on_page:
                on_page(number, len(pdf.pages))  # progress, for the page or the run log
            lines = page_lines(page)

            # 1. Summary lines may be on any page
            statement_summary.update(read_summary(lines))

            # 2. Find the table header on this page; skip pages with no table
            columns = find_columns(lines)
            if columns is not None:
                raw_rows.extend(read_rows(lines, columns))

    for key, what in (
        ("statement_date", "STATEMENT DATE"),
        ("previous", "Previous Balance"),
        ("ending", "Ending Balance"),
    ):
        if key not in statement_summary:
            raise StatementError(f"Could not find the statement's {what}.")

    # 3. Add the year to the dates, and the balance after each row
    rows = []
    balance = statement_summary["previous"]
    for raw in raw_rows:
        balance += raw["Amount"]
        rows.append(
            {
                "Transaction Date": _row_date(
                    raw["transaction"], statement_summary["statement_date"]
                ),
                "Post Date": _row_date(raw["post"], statement_summary["statement_date"]),
                "Description": raw["Description"],
                "Amount": f"{raw['Amount']:.2f}",
                "Balance": f"{balance:.2f}",
            }
        )

    # 4. Verify Previous Balance + amounts == Ending Balance == TOTAL AMOUNT DUE
    check_totals(rows, statement_summary)
    return rows


# =============================================================================
# CHECKING - verify the ending balance
# =============================================================================


def _total(rows) -> Decimal:
    return sum((Decimal(r["Amount"]) for r in rows), Decimal("0.00"))


def check_totals(rows: list[dict[str, str]], summary: dict) -> None:
    """Verify the two BPI credit card checks; raise StatementError on a mismatch.

    1. Previous Balance + sum(Amount) == Ending Balance
    2. Ending Balance == TOTAL AMOUNT DUE (page 3), when that line is found
    """
    expected = summary["previous"] + _total(rows)
    if expected != summary["ending"]:
        raise StatementError(
            f"ending balance check failed: previous balance {summary['previous']:,.2f} "
            f"+ amounts {_total(rows):,.2f} = {expected:,.2f} "
            f"!= statement ending balance {summary['ending']:,.2f}"
        )
    if "total_due" in summary and summary["total_due"] != summary["ending"]:
        raise StatementError(
            f"ending balance {summary['ending']:,.2f} "
            f"!= statement total amount due {summary['total_due']:,.2f}"
        )


def statement_balances(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    """The card's balance in YNAB before and after this statement, for the
    YNAB balance checks.

    The statement counts what is owed as positive; YNAB shows a credit card's
    debt as a negative balance, so both are flipped:
      starting = -(first row's Balance - its Amount)   (-Previous Balance)
      ending   = -(last row's Balance)                 (-Ending Balance)

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
#     "Transaction Date": "Sep 28, 2026",
#     "Post Date":        "Sep 29, 2026",
#     "Description":      "SAMPLE STORE  Taguig",
#     "Amount":           "2801.61",
#     "Balance":          "12801.61"
#   }
#
# The statement has no reference numbers, so the Transaction key is
# "{Transaction Date}|{Post Date}|{Amount}|{Description}|{n}", where n counts
# earlier rows in the same statement with the same four values (almost always
# 0). YNAB's import_id is make_import_id("bpi_credit_card", key)
# (ynab_api/format.py).
#
# The memo is the BPI Card Ref: transaction_ref("bpi_credit_card", key), the
# hash part of that import_id, so searching it in YNAB finds the row.
# =============================================================================


def bpi_card_payee(row: dict[str, str]) -> str:
    """The merchant: the Description up to its two spaces.

    "SAMPLE STORE  Taguig" -> "SAMPLE STORE", "Payment - Thank You" -> as it is
    """
    return row["Description"].split("  ")[0]


def to_transactions(rows: list[dict[str, str]]) -> list[Transaction]:
    """Convert BPI credit card rows into neutral Transactions, one per row.

    A charge is money out of the card (a negative YNAB amount), a payment money in.
    """
    counts: dict[str, int] = {}
    transactions = []
    for row in rows:
        base_key = (
            f"{row['Transaction Date']}|{row['Post Date']}|{row['Amount']}|{row['Description']}"
        )
        occurrence = counts.get(base_key, 0)
        counts[base_key] = occurrence + 1

        key = f"{base_key}|{occurrence}"
        transactions.append(
            Transaction(
                source="bpi_credit_card",
                key=key,
                date=datetime.strptime(row["Transaction Date"], DATE_FORMAT).date(),
                payee=bpi_card_payee(row),
                amount=-Decimal(row["Amount"]),
                memo=f"BPI Card Ref: {transaction_ref('bpi_credit_card', key)}",
                flagged=False,
            )
        )
    return transactions


# =============================================================================
# PASSWORD - rule for the statement PDF (auto-import builds it from inputs)
# =============================================================================


def build_password(inputs: Mapping[str, str]) -> str:
    """BPI Credit Card PDF password: date of birth as YYYYMMDD.

    Takes the `Source.build_password` input dict, with the date as the page's
    date field sends it (YYYY-MM-DD):
        {"birth_date": "2000-01-31"} -> "20000131"
    Raises ValueError for a date that doesn't exist.
    """
    return date.fromisoformat(inputs["birth_date"]).strftime("%Y%m%d")


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output
# =============================================================================


def main() -> None:
    # 1. Getting the PDF password: -p, $BPI_CREDIT_CARD_PDF_PASSWORD, or a prompt
    ap = argparse.ArgumentParser(description="Parse a BPI credit card statement PDF.")
    ap.add_argument("pdf", help="Path to the BPI credit card statement PDF")
    ap.add_argument(
        "-p", "--password", help="PDF password (defaults to $BPI_CREDIT_CARD_PDF_PASSWORD)"
    )
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()
    password = (
        args.password
        or os.environ.get("BPI_CREDIT_CARD_PDF_PASSWORD")
        or getpass.getpass("PDF password: ")
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
