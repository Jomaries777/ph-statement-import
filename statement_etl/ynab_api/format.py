"""
ph-statement-import - turn neutral Transactions into what YNAB's API expects.

The YNAB side of Source -> Transaction -> YNAB (see transaction.py). Sources
never import this file; everything YNAB-specific about a transaction is here:

    date        - "YYYY-MM-DD"
    payee_name  - who the money went to / came from
    import_id   - unique id so YNAB skips rows it has already imported
    amount      - milliunits (1 peso = 1000), negative = money out, positive = money in
    memo        - free text shown in YNAB
    flag_color  - "red" for a row the parser added, so it gets reviewed

Usage:
    from statement_etl.ynab_api.format import to_ynab
    ynab_transactions = [to_ynab(t) for t in transactions]
"""

from statement_etl.transaction import Transaction, transaction_ref


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

    ("gcash", "1000000000002") -> "ETLPH:" + transaction_ref(...)  (36 chars, YNAB's max)
    """
    return IMPORT_ID_PREFIX + transaction_ref(source, source_txn_id)


def fee_import_id(import_id):
    """The import_id of the fee split off a transfer row (ynab_api/transfers.py):
    built from the row's own import_id, so it's as stable, and never the same."""
    return IMPORT_ID_PREFIX + transaction_ref("fee", import_id)


def ynab_transaction(date, payee_name, amount, import_id, memo=None, flag_color=None):
    """Build one transaction in the shape YNAB expects.

    to_ynab() returns its result through this function, so every e-wallet and
    bank produces exactly the same fields.

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


def to_ynab(t: Transaction) -> dict:
    """One neutral Transaction -> one YNAB transaction dict.

    Transaction(source="gcash", key="1000000000002", amount=Decimal("-249.00"), ...)
    -> {"amount": -249000, "import_id": make_import_id("gcash", "1000000000002"), ...}
    """
    return ynab_transaction(
        date=t.date,
        payee_name=t.payee,
        amount=t.amount,
        import_id=make_import_id(t.source, t.key),
        memo=t.memo,
        # Rows the parser added aren't on the statement: flag them red for review
        flag_color="red" if t.flagged else None,
    )
