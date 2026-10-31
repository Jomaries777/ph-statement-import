"""
ph-statement-import - transform parsed statement rows into YNAB transactions.

Each e-wallet or bank parser (e.g. parsers/gcash.py) outputs rows using the
column names from its own PDF. This file converts those rows into the format
YNAB's API expects:

    date        - "YYYY-MM-DD"
    payee_name  - who the money went to / came from
    import_id   - unique id so YNAB skips rows it has already imported
    amount      - milliunits (1 peso = 1000), negative = money out, positive = money in
    memo        - free text shown in YNAB

How to add a new e-wallet or bank (e.g. Maya, BPI):
    1. Write a function that takes ONE parsed row, reads it by that statement's
       own column names, and returns ynab_transaction(...).
       Use gcash_to_ynab() below as the example.
    2. Add it to the TRANSFORMERS dictionary at the bottom of this file, under
       the same key as its Source in sources.py.
    3. Check it:  python -m statement_etl.transform rows.json --source maya
    Full walkthrough: docs/adding-an-e-wallet.md

Usage:
    python -m statement_etl.transform gcash_rows.json --source gcash              # print YNAB JSON
    python -m statement_etl.transform gcash_rows.json --source gcash -o ynab.json # write to a file
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal

from statement_etl.parsers.gcash import gcash_payee, is_correction_row

# =============================================================================
# SHARED HELPERS - used by every e-wallet / bank
# =============================================================================


def to_decimal(text):
    """Turn amount text from a PDF into a number.

    "1,234.50" -> Decimal("1234.50")
    ""         -> Decimal("0")   (blank Debit / Credit cell)
    """
    if not text:
        return Decimal(0)
    return Decimal(text.replace(",", ""))


def to_milliunits(amount):
    """Convert pesos to YNAB milliunits: Decimal("-249.00") -> -249000."""
    return int(amount * 1000)


# Marks import_ids made by ph-statement-import so they never clash with YNAB's own format.
# Never rename: changing it would duplicate every transaction already imported.
IMPORT_ID_PREFIX = "ETLPH:"


def make_import_id(source, source_txn_id):
    """Build a deterministic YNAB import_id from a stable source transaction id.

    The same transaction always gets the same id, so re-running a statement
    never creates duplicates. Only the source id is hashed, not the date,
    amount or memo, so later corrections to those still match.

    ("gcash", "1000000000002") -> "ETLPH:" + 30 hex chars  (36 chars, YNAB's max)
    """
    digest = hashlib.sha256(f"{source}:{source_txn_id}".encode()).hexdigest()
    return IMPORT_ID_PREFIX + digest[: 36 - len(IMPORT_ID_PREFIX)]


def ynab_transaction(date, payee_name, amount, import_id, memo=None, flag_color=None):
    """Build one transaction in the shape YNAB expects.

    Every transformer returns its result through this function, so all
    e-wallets and banks produce exactly the same fields.

    Args:
        date:       datetime (or date) of the transaction
        payee_name: who was paid / who paid you
        amount:     Decimal in pesos; negative = outflow, positive = inflow
        import_id:  unique id for this transaction, from make_import_id()
        memo:       optional note shown in YNAB
        flag_color: optional YNAB flag, e.g. "red" for balance corrections
    """
    return {
        "date": date.strftime("%Y-%m-%d"),
        "payee_name": payee_name,
        "import_id": import_id,
        "amount": to_milliunits(amount),
        "memo": memo,
        "flag_color": flag_color,
    }


# =============================================================================
# GCASH - rows from parsers.gcash.parse_transactions()
#
# Input row example:
#   {
#     "Date and Time": "2026-10-05 08:15 AM",
#     "Description":   "Payment to Sample Store, Merchant Transaction Number: X1",
#     "Reference No.": "1000000000002",
#     "Debit":         "249.00",
#     "Credit":        "",
#     "Balance":       "1750.25"
#   }
#
# The payee clean-up (gcash_payee) lives in parsers/gcash.py, which also uses it
# for "Refund from <payee>" rows.
# =============================================================================


def gcash_to_ynab(row):
    """Convert one GCash row into a YNAB transaction.

    Balance-correction rows added by parsers.gcash (STARTING BALANCE CORRECTION,
    "Payment to" / "Refund from" legs added by a split or a running-balance gap,
    MISSING TRANSACTIONS IN GCASH STATEMENT) are converted too, with a red
    flag. Every row's memo is "GCash Ref: <Reference No.>".
    """
    # "2026-10-05 08:15 AM" -> datetime
    date = datetime.strptime(row["Date and Time"], "%Y-%m-%d %I:%M %p")

    # Money in (Credit) is positive, money out (Debit) is negative.
    # One of the two cells is always blank, which to_decimal() treats as 0.
    amount = to_decimal(row["Credit"]) - to_decimal(row["Debit"])

    # Reference No. is unique per transaction; correction rows use a kind
    # letter + digits (e.g. R1000000000002, M0000123456789), also unique and stable
    reference = row["Reference No."]
    memo = "GCash Ref: " + reference
    # Correction rows aren't on the statement: flag them red for review
    flag_color = "red" if is_correction_row(row) else None

    return ynab_transaction(
        date=date,
        payee_name=gcash_payee(row["Description"]),
        amount=amount,
        import_id=make_import_id("gcash", reference),
        memo=memo,
        flag_color=flag_color,
    )


# =============================================================================
# REGISTRY - one entry per e-wallet / bank
#
# To add a new source, write a <source>_to_ynab(row) function above and
# add it here, e.g.  "maya": maya_to_ynab,
# The keys must match sources.SOURCES (tests/test_sources.py checks this).
# =============================================================================

TRANSFORMERS = {
    "gcash": gcash_to_ynab,
}


def transform(rows, source):
    """Convert a list of parsed rows from `source` into YNAB transactions.

    Args:
        rows:   list of dicts from that source's parser
        source: key in TRANSFORMERS, e.g. "gcash"
    """
    if source not in TRANSFORMERS:
        known = ", ".join(sorted(TRANSFORMERS))
        raise ValueError(f"Unknown source {source!r}. Known sources: {known}")

    to_ynab = TRANSFORMERS[source]
    return [to_ynab(row) for row in rows]


# =============================================================================
# MAIN - read parser JSON -> transform -> output YNAB JSON
# =============================================================================


def main():
    # 1. Read the command-line options
    ap = argparse.ArgumentParser(description="Transform parsed rows into YNAB transactions.")
    ap.add_argument("input", help="JSON file written by a parser (e.g. parsers/gcash.py -o)")
    ap.add_argument(
        "--source",
        required=True,
        choices=sorted(TRANSFORMERS),
        help="Which e-wallet / bank the rows came from",
    )
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()

    # 2. Load the parsed rows
    with open(args.input) as f:
        rows = json.load(f)

    # 3. Transform them into YNAB transactions
    transactions = transform(rows, args.source)

    # 4. Output: print the JSON, or write it to a file with -o
    out = json.dumps(transactions, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out + "\n")
        print(f"Wrote {len(transactions)} transactions to {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
