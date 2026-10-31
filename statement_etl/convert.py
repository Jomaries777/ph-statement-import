"""
ph-statement-import - turn a statement's parsed rows into neutral Transactions,
using the source's own converter.

    parsed rows  ->  rows_to_transactions(rows, "gcash")  ->  list of Transactions
                     (calls sources.SOURCES["gcash"].to_transactions)

Each source's converter lives next to its parser (e.g. gcash.to_transactions in
parsers/gcash.py) and is registered on its Source in sources.py, so this file
never changes when a source is added. Turning a Transaction into YNAB's format
is ynab_api/format.py's job.

Usage (prints the same YNAB JSON that pipeline.py uploads):
    python -m statement_etl.convert gcash_rows.json --source gcash              # print YNAB JSON
    python -m statement_etl.convert gcash_rows.json --source gcash -o ynab.json # write to a file
"""

import argparse
import json
import sys

from statement_etl.sources import SOURCES, get_source
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import to_ynab


def rows_to_transactions(rows, source) -> list[Transaction]:
    """Convert a list of parsed rows from `source` into neutral Transactions.

    Args:
        rows:   list of dicts from that source's parser
        source: key in sources.SOURCES, e.g. "gcash"; ValueError if unknown
    """
    return get_source(source).to_transactions(rows)


# =============================================================================
# MAIN - read parser JSON -> convert -> output YNAB JSON
# =============================================================================


def main():
    # 1. Read the command-line options
    ap = argparse.ArgumentParser(description="Convert parsed rows into YNAB transactions.")
    ap.add_argument("input", help="JSON file written by a parser (e.g. parsers/gcash.py -o)")
    ap.add_argument(
        "--source",
        required=True,
        choices=sorted(SOURCES),
        help="Which e-wallet / bank the rows came from",
    )
    ap.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = ap.parse_args()

    # 2. Load the parsed rows
    with open(args.input) as f:
        rows = json.load(f)

    # 3. Convert them into Transactions, then into YNAB's format
    transactions = [to_ynab(t) for t in rows_to_transactions(rows, args.source)]

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
