"""
ph-statement-import - GCash parser.

Parse a password-protected GCash Transaction History PDF.

Output fields per transaction (same columns as the PDF table, text as shown):
    Date and Time  - e.g. "2025-09-01 10:30 AM"
    Description    - full description, wrapped lines joined
    Reference No.
    Debit          - amount text, or "" if blank
    Credit         - amount text, or "" if blank
    Balance

transform.py converts these rows into YNAB transactions.

How it works:
    1. Open      - open the PDF with its password                (open_pdf)
    2. Lines     - group the words on each page into text lines  (group_lines)
    3. Header    - find the table header to learn where the
                   Debit / Credit columns are                    (_find_columns)
    4. Rows      - every line that starts with a date is one
                   transaction; leftover lines are wrapped
                   description text, joined to the nearest row   (_parse_transaction_line,
                                                                  _attach_wrapped_lines)
    5. Check     - compare the parsed totals with the statement's
                   own "Total Debit" / "Total Credit"            (_check_totals)
    6. Balance   - check the running balance on every row;
                   split 0.00-balance rows into "Payment to" +
                   "Refund from"; add STARTING BALANCE
                   CORRECTION / "Refund from <payee>" rows
                   where it breaks, and a MISSING TRANSACTIONS
                   row if the ending balance is still off
                   (a warning, not an error; uploaded to YNAB)   (_correct_balances,
                                                                  balance_warning)

This source is registered in sources.py. To add another e-wallet or bank,
see docs/adding-an-e-wallet.md.

Requirements:
    pip install pdfplumber

Usage:
    python -m statement_etl.parsers.gcash statement.pdf  # prompts for password
    python -m statement_etl.parsers.gcash statement.pdf -p PASSWORD
    python -m statement_etl.parsers.gcash statement.pdf -o out.json
"""

import argparse
import getpass
import hashlib
import json
import re
import sys
from datetime import datetime

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl.parsers.common import StatementError, group_lines, open_pdf, to_decimal

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell


# =============================================================================
# STATEMENT SETTINGS - GCash-specific; change these for another e-wallet / bank
# =============================================================================

# Words in the table header row. The page's header line must contain all of them;
# the Debit and Credit positions tell us which column an amount belongs to.
DEBIT_LABEL = "Debit"
CREDIT_LABEL = "Credit"
BALANCE_LABEL = "Balance"

# Every column of the transaction table, in the order the PDF prints them.
# Each parsed row is a dict with exactly these keys (see _to_output_row).
COLUMNS = (
    "Date and Time",
    "Description",
    "Reference No.",
    "Debit",
    "Credit",
    "Balance",
)

# How the date and time are written at the start of each row: "2026-10-05 08:15 AM"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # "2026-10-05"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")  # "08:15"
DATE_TIME_FORMAT = "%Y-%m-%d %I:%M %p"  # used to read and to write it back out

# Money amounts: "1,234.50" or "-59.00"
AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")

# GCash reference numbers: 10 or more digits, e.g. "1000000000002"
REF_RE = re.compile(r"^\d{10,}$")

# Payee in a description: "Payment to Sample Store ..." -> "Sample Store ..."
GCASH_PAYMENT_RE = re.compile(r"^Payment to\s+(.*)$", re.IGNORECASE)

# Removes the ", Merchant Transaction Number: ..." tail from a payee name
GCASH_MERCHANT_SUFFIX_RE = re.compile(r",\s*Merchant Transaction Number:.*$", re.IGNORECASE)

# Statement totals, used to check nothing was missed: "Total Debit 9,321.00"
TOTAL_RE = re.compile(r"^Total (Debit|Credit)\s+([\d,]+\.\d{2})$", re.IGNORECASE)

# Opening / closing balance lines: "STARTING BALANCE 0.00", "ENDING BALANCE 5924.56"
BALANCE_RE = re.compile(r"^(STARTING|ENDING) BALANCE\s+(-?[\d,]+\.\d{2})$", re.IGNORECASE)

# Summary lines inside the table that are not transactions
SUMMARY_RE = re.compile(
    r"^(STARTING BALANCE|ENDING BALANCE|Total Debit|Total Credit)\b", re.IGNORECASE
)


# =============================================================================
# PAGE LAYOUT - find the table and join wrapped lines (REUSABLE)
#
# pdfplumber gives every word with its position on the page:
#   {"text": "Debit", "x0": 410.2, "x1": 432.8, "top": 180.5, ...}
#   x0 / x1 = left / right edge, top = distance from the top of the page.
# group_lines() (parsers/common.py) turns them into text lines first.
# =============================================================================


def _find_columns(lines):
    """Locate the table header on a page.

    Returns (header_top, debit_x, credit_x), or None if this page has no header.
        header_top - lines at or above this are page titles, not transactions
        debit_x    - right edge of the "Debit" label
        credit_x   - right edge of the "Credit" label
    """
    for line in lines:
        texts = [w["text"] for w in line["words"]]
        if DEBIT_LABEL in texts and CREDIT_LABEL in texts and BALANCE_LABEL in texts:
            debit = next(w for w in line["words"] if w["text"] == DEBIT_LABEL)
            credit = next(w for w in line["words"] if w["text"] == CREDIT_LABEL)
            return line["top"], debit["x1"], credit["x1"]
    return None


def _attach_wrapped_lines(rows, other_lines):
    """Add wrapped description text to the transaction rows it belongs to.

    A long description wraps onto extra lines above / below the row's main
    line (the one with the date). Only words that fall inside the
    Description column are kept, and each line goes to the nearest row.

    Args:
        rows:        rows from _parse_transaction_line() on this page
        other_lines: lines on this page that weren't a transaction row
    """
    desc_left = min(r["desc_left"] for r in rows)
    desc_right = max(r["desc_right"] for r in rows)
    for line in other_lines:
        words = [w for w in line["words"] if w["x0"] >= desc_left and w["x1"] <= desc_right]
        if words:
            nearest = min(rows, key=lambda r: abs(r["top"] - line["top"]))
            nearest["desc_words"].extend(words)


# =============================================================================
# ROW PARSING - GCash-specific; rewrite these two for another e-wallet / bank
#
# GCash table line, left to right:
#   2026-10-05  08:15  AM  Payment to Sample Store ...  1000000000002  249.00  1750.25
#   date        time   am  description words            reference      amount  balance
# =============================================================================


def _parse_transaction_line(line, debit_x, credit_x):
    """Read one table line. Returns a row dict, or None if it isn't a transaction.

    A line is a transaction if it starts with date, time, AM/PM and ends with
    a reference number followed by the amount and balance.

    The returned dict holds the values plus what _attach_wrapped_lines() needs:
        top        - line height on the page
        desc_left  - left edge of the Description column (end of the AM/PM word)
        desc_right - right edge of the Description column (start of the reference)
        desc_words - description words found so far
    """
    words = line["words"]

    # 1. Must start with "YYYY-MM-DD HH:MM AM/PM"
    if (
        len(words) < 5
        or not DATE_RE.match(words[0]["text"])
        or not TIME_RE.match(words[1]["text"])
        or words[2]["text"].upper() not in ("AM", "PM")
    ):
        return None

    # 2. Walk back from the end over the amount words; the word just before
    #    them must be the reference number.
    i = len(words)
    while i > 0 and AMOUNT_RE.match(words[i - 1]["text"]):
        i -= 1
    amounts = words[i:]
    if i == 0 or not REF_RE.match(words[i - 1]["text"]) or len(amounts) < 2:
        return None
    ref = words[i - 1]

    # 3. Last amount is the balance; the one before it sits under Debit or Credit.
    #    Amounts are right-aligned, so compare right edges with the header labels.
    amt = amounts[-2]
    is_debit = abs(amt["x1"] - debit_x) < abs(amt["x1"] - credit_x)

    date_time = datetime.strptime(" ".join(w["text"] for w in words[:3]), DATE_TIME_FORMAT)
    return {
        "top": line["top"],
        "desc_left": words[2]["x1"],
        "desc_right": ref["x0"],
        "desc_words": words[3 : i - 1],  # between AM/PM and the reference
        "datetime": date_time,
        "reference": ref["text"],
        "debit": amt["text"] if is_debit else "",
        "credit": "" if is_debit else amt["text"],
        "balance": amounts[-1]["text"],
    }


def _to_output_row(row):
    """Build the final output row, with the same column names as the PDF table."""
    # Wrapped lines were added out of order; sort words top-to-bottom, left-to-right
    desc_words = sorted(row["desc_words"], key=lambda w: (round(w["top"]), w["x0"]))
    return {
        "Date and Time": row["datetime"].strftime(DATE_TIME_FORMAT),
        "Description": " ".join(w["text"] for w in desc_words),
        "Reference No.": row["reference"],
        "Debit": row["debit"],
        "Credit": row["credit"],
        "Balance": row["balance"],
    }


def gcash_payee(description):
    """Get a clean payee name from a GCash description.

    "Payment to Sample Store, Merchant Transaction Number: X" -> "Sample Store"
    Anything else (e.g. "Received GCash from ...", "Refund from Angkas") is kept
    as it is. Used for "Refund from <payee>" rows and by transform.py.
    """
    match = GCASH_PAYMENT_RE.match(description)
    if not match:
        return description
    return GCASH_MERCHANT_SUFFIX_RE.sub("", match.group(1)).strip()


# =============================================================================
# PARSING - go through every page and collect the rows
# =============================================================================


def parse_transactions(pdf_path, password=""):
    """Parse a GCash PDF into a list of transaction dicts.

    Raises StatementError if the password is wrong or the parsed totals don't
    match the statement's totals.
    """
    transactions = []
    totals = {}  # {"debit": Decimal, "credit": Decimal} from the statement
    balances = {}  # {"starting": Decimal, "ending": Decimal} from the statement
    columns = None  # kept from the last page with a header, for pages without one

    with open_pdf(pdf_path, password) as pdf:
        for page in pdf.pages:
            lines = group_lines(page.extract_words(keep_blank_chars=False, use_text_flow=False))

            # Find the Debit / Credit columns; skip pages before the table starts
            columns = _find_columns(lines) or columns
            if columns is None:
                continue
            header_top, debit_x, credit_x = columns

            # Sort each line below the header into a transaction row or leftover text
            rows, other_lines = [], []
            for line in lines:
                if line["top"] <= header_top:
                    continue

                # Remember the statement totals for _check_totals()
                total = TOTAL_RE.match(line["text"])
                if total:
                    totals[total.group(1).lower()] = to_decimal(total.group(2))
                    continue

                # Remember the starting / ending balance for _correct_balances()
                balance = BALANCE_RE.match(line["text"])
                if balance:
                    balances[balance.group(1).lower()] = to_decimal(balance.group(2))
                    continue
                if SUMMARY_RE.match(line["text"]):
                    continue

                row = _parse_transaction_line(line, debit_x, credit_x)
                if row:
                    rows.append(row)
                else:
                    other_lines.append(line)

            if not rows:
                continue

            _attach_wrapped_lines(rows, other_lines)
            transactions.extend(_to_output_row(r) for r in rows)

    _check_totals(transactions, totals)
    return _correct_balances(transactions, balances)


# =============================================================================
# CHECKING - make sure no transaction was missed (REUSABLE)
# Stops with StatementError: a mismatch here means the PDF was read wrongly.
# =============================================================================


def _check_totals(transactions, totals):
    """Fail if parsed debits/credits don't match the statement's totals.

    Skipped for a total the statement doesn't print.
    """
    debit = sum(to_decimal(t["Debit"]) for t in transactions)
    credit = sum(to_decimal(t["Credit"]) for t in transactions)
    for name, parsed in (("debit", debit), ("credit", credit)):
        if name in totals and totals[name] != parsed:
            raise StatementError(f"parsed total {name} {parsed} != statement total {totals[name]}")


# =============================================================================
# BALANCE CORRECTION - when the statement itself doesn't add up (REUSABLE)
#
# Every row's printed Balance should equal the previous balance + Credit - Debit
# (the first row starts from the STARTING BALANCE). Where it doesn't, rows are
# split or correction rows are added. This is a warning, not an error:
#
#   Balance 0.00 on a row with a      -> split into "Payment to <payee>" (out) and
#   Credit / Debit (wallet unchanged)    "Refund from <payee>" (in), netting to 0;
#                                        the running balance stays as before
#   gap at the first checked row      -> STARTING BALANCE CORRECTION (in or out),
#                                        inserted before that row
#   later gap, balance too high       -> "Refund from <payee of that row>" (in),
#                                        inserted just before that row
#   later gap, balance too low        -> no row there; it rolls into the last check:
#   ending balance still off          -> MISSING TRANSACTIONS IN GCASH STATEMENT,
#                                        added after the last row
#
# Statement rows keep their own Reference No. (a split keeps it on the leg that
# matches the statement's Debit / Credit column). Rows the pipeline adds are
# "correction rows", with a Reference No. that starts with a letter:
#   S / R / P + Reference No. of the row they belong to   e.g. R1000000000002
#   M0000 + 9-digit hash of the statement's first and last Reference No.
#                                                         e.g. M0000123456789
# The same statement always gives the same numbers, so re-importing it doesn't
# duplicate them in YNAB. Correction rows ARE uploaded to YNAB (unapproved,
# red flag, memo "GCash Ref: <Reference No.>").
#
# Registered for GCash in sources.py, which pipeline.py and app.py use:
#   is_correction_row(), balance_corrections(), balance_warning(),
#   statement_balances() (for the YNAB balance checks in ynab_api/balance.py /
#   ynab_api/client.py) and summary(). transform.py uses is_correction_row().
# =============================================================================

# Descriptions of correction rows that have no payee
START_DESCRIPTION = "STARTING BALANCE CORRECTION"
MISSING_DESCRIPTION = "MISSING TRANSACTIONS IN GCASH STATEMENT"

# Reference No. prefixes of correction rows (see the header above)
START_KIND = "S"  # starting balance correction
REFUND_KIND = "R"  # "Refund from <payee>" added by the pipeline
PAYMENT_KIND = "P"  # "Payment to <payee>" added by a split
MISSING_KIND = "M"  # missing transactions at the end
CORRECTION_KINDS = (START_KIND, REFUND_KIND, PAYMENT_KIND, MISSING_KIND)


def _row(date_time, description, reference, amount, balance):
    """Build a row in the same shape as a parsed statement row.

    amount > 0 goes in Credit (money in), amount < 0 in Debit (money out).
    """
    return {
        "Date and Time": date_time,
        "Description": description,
        "Reference No.": reference,
        "Debit": f"{-amount:.2f}" if amount < 0 else "",
        "Credit": f"{amount:.2f}" if amount > 0 else "",
        "Balance": f"{balance:.2f}",
    }


def _correction_row(kind, description, anchor, amount, balance, reference=None):
    """A row added by the pipeline, belonging to the statement row `anchor`.

    Reference No. is `kind` + the anchor's Reference No. (e.g. R1000000000002),
    unless `reference` is given.
    """
    return _row(
        anchor["Date and Time"],
        description,
        reference or f"{kind}{anchor['Reference No.']}",
        amount,
        balance,
    )


def _missing_reference(transactions):
    """Reference No. for the MISSING row: "M0000" + 9-digit hash.

    The hash is made from the statement's first and last Reference No., so the
    same statement always gets the same number and YNAB skips it on re-upload.
    """
    key = f"{transactions[0]['Reference No.']}:{transactions[-1]['Reference No.']}"
    number = int(hashlib.sha256(key.encode()).hexdigest(), 16) % 10**9
    return f"{MISSING_KIND}0000{number:09d}"


def _split_zero_balance(row, previous):
    """Split a row whose Balance shows 0.00 into a payment and its refund.

    The statement leg (Debit -> the payment, Credit -> the refund) keeps the
    row's Reference No.; the other leg is a correction row. Balances show the
    wallet going down by the payment and back up to `previous`.
    """
    amount = to_decimal(row["Debit"]) or to_decimal(row["Credit"])
    payee = gcash_payee(row["Description"])
    payment_text, refund_text = f"Payment to {payee}", f"Refund from {payee}"

    if row["Debit"]:
        # Statement shows the payment; the refund is what's missing
        payment = _row(
            row["Date and Time"],
            row["Description"],
            row["Reference No."],
            -amount,
            previous - amount,
        )
        refund = _correction_row(REFUND_KIND, refund_text, row, amount, previous)
    else:
        # Statement shows the money coming back; the payment is what's missing
        payment = _correction_row(PAYMENT_KIND, payment_text, row, -amount, previous - amount)
        refund = _row(row["Date and Time"], refund_text, row["Reference No."], amount, previous)
    return [payment, refund]


def _correct_balances(transactions, balances):
    """Check the running balance row by row; split rows and add correction rows.

    Args:
        transactions: statement rows from _to_output_row(), in statement order
        balances:     {"starting": Decimal, "ending": Decimal}, either may be missing

    Returns a new list with 0.00-balance rows split, STARTING BALANCE CORRECTION
    / "Refund from" rows inserted where the running balance breaks, and a
    MISSING row at the end if the ending balance still doesn't match. Without a
    STARTING BALANCE line the first row can't be checked, so checking starts
    from the first row's balance.
    """
    if not transactions:
        return transactions

    result = []
    previous = balances.get("starting")  # running balance before the current row
    checked_any = False  # has any row's balance been checked yet?
    # Who an unlisted refund most likely came from (used in step 3):
    last_split_payee = None  # payee of the last 0.00-balance row (e.g. Angkas)
    last_payment_payee = None  # payee of the last "Payment to ..." row
    for row in transactions:
        credit = to_decimal(row["Credit"])
        debit = to_decimal(row["Debit"])
        printed = to_decimal(row["Balance"])
        expected = None if previous is None else previous + credit - debit

        # 1. Balance 0.00 but money moved (and it isn't a real zero balance):
        #    paid and refunded, wallet unchanged -> split, keep the running balance
        if printed == 0 and (credit or debit) and expected != 0 and previous is not None:
            result.extend(_split_zero_balance(row, previous))
            last_split_payee = gcash_payee(row["Description"])
            continue

        if expected is not None:
            gap = printed - expected
            if gap and not checked_any:
                # 2. First checked row: the statement's starting balance is wrong
                result.append(
                    _correction_row(START_KIND, START_DESCRIPTION, row, gap, previous + gap)
                )
            elif gap > 0:
                # 3. Balance higher than expected: money came in that isn't listed.
                #    Name it after the merchant that most likely refunded it,
                #    not the row where the gap shows up.
                payee = last_split_payee or last_payment_payee or gcash_payee(row["Description"])
                refund_text = f"Refund from {payee}"
                result.append(_correction_row(REFUND_KIND, refund_text, row, gap, previous + gap))
            # 4. gap < 0 (money left without a row): left for the MISSING row below
            checked_any = True

        result.append(row)
        previous = printed
        if GCASH_PAYMENT_RE.match(row["Description"]):
            last_payment_payee = gcash_payee(row["Description"])

    # 5. Ending balance: whatever still doesn't add up becomes one MISSING row
    if "starting" in balances and "ending" in balances:
        credit = sum(to_decimal(t["Credit"]) for t in result)
        debit = sum(to_decimal(t["Debit"]) for t in result)
        difference = balances["ending"] - (balances["starting"] + credit - debit)
        if difference:
            result.append(
                _correction_row(
                    MISSING_KIND,
                    MISSING_DESCRIPTION,
                    transactions[-1],
                    difference,
                    balances["ending"],
                    reference=_missing_reference(transactions),
                )
            )
    return result


def is_correction_row(row):
    """True for a row added by the pipeline: its Reference No. is a kind letter
    (S / R / P / M) followed by digits; GCash's own are digits only."""
    reference = row["Reference No."]
    return reference[:1] in CORRECTION_KINDS and reference[1:].isdigit()


def balance_corrections(rows):
    """The correction rows in the parsed output, for display.

    Returns [{"date_time", "description", "reference", "amount"}] with amount a
    signed Decimal (+ money in, - money out). Empty if the statement adds up.
    """
    return [
        {
            "date_time": r["Date and Time"],
            "description": r["Description"],
            "reference": r["Reference No."],
            "amount": to_decimal(r["Credit"]) - to_decimal(r["Debit"]),
        }
        for r in rows
        if is_correction_row(r)
    ]


def balance_warning(rows):
    """Warning text listing the correction rows, or None if the statement adds up."""
    corrections = balance_corrections(rows)
    if not corrections:
        return None
    total = sum(c["amount"] for c in corrections)
    lines = [
        f"Balance does not add up: {len(corrections)} correction row(s) added, "
        f"total {total:+,.2f}. They are uploaded to YNAB, flagged red for review."
    ]
    lines += [
        f"  {c['date_time']}  {c['description']:<40} {c['amount']:>+12,.2f}" for c in corrections
    ]
    return "\n".join(lines)


def statement_balances(rows):
    """The statement's starting and ending balance, for the YNAB balance checks.

    Read from the parsed rows, which the correction rows make add up:
      starting = first row's Balance - its Credit + its Debit
      ending   = last row's Balance
    These are the printed STARTING / ENDING BALANCE when the statement has them.

    Returns {"starting": Decimal, "ending": Decimal}, or {} if there are no rows.
    """
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    starting = (
        to_decimal(first["Balance"]) - to_decimal(first["Credit"]) + to_decimal(first["Debit"])
    )
    return {"starting": starting, "ending": to_decimal(last["Balance"])}


def summary(rows):
    """One line shown under the parsed rows: the statement rows' totals.

    _check_totals() already stopped parsing if these differ from the
    statement's printed totals, so they always match it here.
    """
    statement_rows = [r for r in rows if not is_correction_row(r)]
    debit = sum(to_decimal(r["Debit"]) for r in statement_rows)
    credit = sum(to_decimal(r["Credit"]) for r in statement_rows)
    return f"Total debit {debit:,.2f} · Total credit {credit:,.2f} · matches statement ✓"


# =============================================================================
# MAIN - runs the steps in order: get PDF -> parse -> output (REUSABLE)
# =============================================================================


def main():
    # 1. Getting the PDF: read the file path and password from the command line
    ap = argparse.ArgumentParser(description="Parse a GCash Transaction History PDF.")
    ap.add_argument("pdf", help="Path to the encrypted GCash PDF")
    ap.add_argument("-p", "--password", help="PDF password (prompted if omitted)")
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()

    password = args.password if args.password is not None else getpass.getpass("PDF password: ")

    # 2. Parsing: open the PDF with the password and read the transaction table
    transactions = parse_transactions(args.pdf, password)
    warning = balance_warning(transactions)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)

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
