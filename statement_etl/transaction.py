"""
ph-statement-import - one statement transaction, in no budgeting app's format.

The code has three parts, so a new e-wallet / bank and a new budgeting app can
each be added without touching the other:

    Source (parsers/<source>.py)  ->  Transaction (this file)  ->  YNAB (ynab_api/)
    PDF -> rows -> to_transactions()    the shape only             format.to_ynab(), upload

This file holds the data shape and one helper, transaction_ref(). convert.py
picks the source and calls its to_transactions(); ynab_api/format.py turns a
Transaction into what YNAB's API expects (milliunits, import_id). Another
budgeting app would start from the same Transaction.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# Hex characters in a transaction_ref(); with format.py's "ETLPH:" prefix this
# is YNAB's 36-character import_id limit. Never change it: every import_id
# already in YNAB would change, and re-imports would duplicate them.
REF_LENGTH = 30


def transaction_ref(source: str, key: str) -> str:
    """A short, stable reference for one transaction: the hash of its source and key.

    The same row always gets the same ref. ynab_api/format.py builds YNAB's
    import_id from it, so a source with no reference numbers of its own (GoTyme)
    can put it in the memo, and searching it in YNAB finds the row.

    ("gotyme", "28 Oct 2026|0.00|500.00|3568.34|0") -> "9ac288af95f6925425a5154d159507"
    """
    return hashlib.sha256(f"{source}:{key}".encode()).hexdigest()[:REF_LENGTH]


@dataclass(frozen=True)
class Transaction:
    """One statement transaction, in no particular tracker's format.

    Transaction(source="gcash", key="1000000000002", date=datetime(2026, 10, 5, 8, 15),
                payee="Sample Store", amount=Decimal("-249.00"),
                memo="GCash Ref: 1000000000002")
    """

    source: str  # source key, e.g. "gcash"; part of every tracker's duplicate check
    key: str  # stable id within the source, e.g. GCash Reference No.
    date: date  # a datetime is fine when the statement has times
    payee: str
    amount: Decimal  # pesos; inflow positive, outflow negative
    memo: str | None = None
    flagged: bool = False  # a row the parser added (balance correction): show it for review
