"""
ph-statement-import - one statement transaction, in no budgeting app's format.

The code has three parts, so a new e-wallet / bank and a new budgeting app can
each be added without touching the other:

    Source (parsers/<source>.py)  ->  Transaction (this file)  ->  YNAB (ynab_api/)
    PDF -> rows -> to_transactions()    the shape only             format.to_ynab(), upload

This file holds only the data shape, no logic. convert.py picks the source and
calls its to_transactions(); ynab_api/format.py turns a Transaction into what
YNAB's API expects (milliunits, import_id). Another budgeting app would start
from the same Transaction.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


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
