"""
ph-statement-import - the e-wallets and banks it can import.

Every statement looks different, so there is no common row format: a parser
returns each row with the statement's own column names. A Source tells the
rest of the pipeline (command line and web app) what it needs to know about
those columns, and which parser functions to call.

The transformer that turns a row into a YNAB transaction is registered
separately, in transform.TRANSFORMERS, under the same key.

To add another e-wallet / bank, see docs/adding-an-e-wallet.md.
"""

from collections.abc import Callable
from dataclasses import dataclass

from statement_etl.parsers import gcash


def _no_summary(rows):
    return None


def _never_correction(row):
    return False


def _no_corrections(rows):
    return []


def _no_warning(rows):
    return None


@dataclass(frozen=True)
class Source:
    """One e-wallet or bank statement format.

    Identity:
        key          - "gcash": the --source value, the key in
                       transform.TRANSFORMERS and part of every YNAB import_id.
                       Never rename it once released: YNAB would see every
                       transaction as new and import duplicates.
        name         - "GCash": shown on the page and in the command-line output
        password_env - environment variable with the PDF password, for the
                       command line, e.g. "GCASH_PDF_PASSWORD"

    The statement's own columns (names exactly as the parser returns them):
        columns           - every column, in the order the statement prints them
        amount_columns    - money columns: right-aligned on the page and in the CLI
        money_in_columns  - shown green on the page, e.g. ("Credit",)
        money_out_columns - shown red on the page, e.g. ("Debit",)
        text_column       - long free text that may wrap, e.g. "Description"

    Parser functions:
        parse              - (pdf, password) -> list of rows, each a dict with
                             one key per column. pdf is a path or a file object.
        statement_balances - rows -> {"starting": Decimal, "ending": Decimal},
                             for the YNAB balance checks before / after upload
        summary            - rows -> one line shown under the parsed rows, or None

    Optional, for parsers that add balance-correction rows (GCash does):
        is_correction_row   - row -> True for a row the parser added
        balance_corrections - rows -> [{"date_time", "description",
                              "reference", "amount" (signed Decimal)}]
        balance_warning     - rows -> warning text, or None if the statement adds up
    """

    key: str
    name: str
    password_env: str
    columns: tuple
    parse: Callable
    statement_balances: Callable
    amount_columns: tuple = ()
    money_in_columns: tuple = ()
    money_out_columns: tuple = ()
    text_column: str | None = None
    summary: Callable = _no_summary
    is_correction_row: Callable = _never_correction
    balance_corrections: Callable = _no_corrections
    balance_warning: Callable = _no_warning


# =============================================================================
# REGISTRY - one entry per e-wallet / bank
# =============================================================================

SOURCES = {
    s.key: s
    for s in [
        Source(
            key="gcash",
            name="GCash",
            password_env="GCASH_PDF_PASSWORD",
            columns=gcash.COLUMNS,
            amount_columns=("Debit", "Credit", "Balance"),
            money_in_columns=("Credit",),
            money_out_columns=("Debit",),
            text_column="Description",
            parse=gcash.parse_transactions,
            statement_balances=gcash.statement_balances,
            summary=gcash.summary,
            is_correction_row=gcash.is_correction_row,
            balance_corrections=gcash.balance_corrections,
            balance_warning=gcash.balance_warning,
        ),
    ]
}

# Picked when no source is given (--source, or the page's Source field)
DEFAULT_SOURCE = "gcash"


def get_source(key):
    """The Source for `key`; ValueError naming the known keys if there is none."""
    if key not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        raise ValueError(f"Unknown source {key!r}. Known sources: {known}")
    return SOURCES[key]
