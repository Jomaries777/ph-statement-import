"""
ph-statement-import - the e-wallets and banks it can import.

Every statement looks different, so there is no common row format: a parser
returns each row with the statement's own column names. A Source tells the
rest of the pipeline (command line and web app) what it needs to know about
those columns, and which parser functions to call, including
to_transactions, which turns the rows into neutral Transactions
(transaction.py). Nothing here knows about YNAB.

To add another e-wallet / bank, see docs/adding-an-e-wallet.md.
"""

from collections.abc import Callable
from dataclasses import dataclass

from statement_etl.parsers import gcash, gotyme


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
        key          - "gcash": the --source value and part of every YNAB
                       import_id (via Transaction.source).
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
        parse              - (pdf, password, on_page=None) -> list of rows, each a
                             dict with one key per column. pdf is a path or a file
                             object; on_page(page, pages) is called as each page is
                             read, for progress.
        statement_balances - rows -> {"starting": Decimal, "ending": Decimal},
                             for the YNAB balance checks before / after upload
        to_transactions    - rows -> list of Transactions (transaction.py), one
                             per row, with Transaction.source set to this key
        summary            - rows -> one line shown under the parsed rows, or None

    Optional, for parsers that add balance-correction rows (GCash does):
        is_correction_row   - row -> True for a row the parser added
        balance_corrections - rows -> [{"date_time", "description",
                              "reference", "amount" (signed Decimal)}]
        balance_warning     - rows -> warning text, or None if the statement adds up

    Optional, for Gmail auto-import (/auto-import in hosted mode):
        email_query        - Gmail search for this source's statement emails.
                             Sources without it don't appear on /auto-import.
        attachment_pattern - regex for the PDF attachment's filename (an email
                             may carry several PDFs)
        password_inputs    - names of the inputs the user enters once, e.g.
                             ("last_name", "mobile_last4"). A new name gets its
                             label and field type from INPUT_FIELDS
                             (web/auto_import.py).
        build_password     - dict of inputs -> PDF password
        account_input      - for sources where one person can have several
                             accounts: the input that tells them apart, e.g.
                             "mobile_first7" (INPUT_FIELDS in web/auto_import.py)
        email_account      - (email body text, PDF filename) -> the account_input
                             value the email is for, or None if it doesn't say.
                             Auto-import sends each email to the wallet with
                             that value.
    """

    key: str
    name: str
    password_env: str
    columns: tuple
    parse: Callable
    statement_balances: Callable
    to_transactions: Callable
    amount_columns: tuple = ()
    money_in_columns: tuple = ()
    money_out_columns: tuple = ()
    text_column: str | None = None
    summary: Callable = _no_summary
    is_correction_row: Callable = _never_correction
    balance_corrections: Callable = _no_corrections
    balance_warning: Callable = _no_warning
    email_query: str | None = None
    attachment_pattern: str | None = None
    password_inputs: tuple = ()
    build_password: Callable | None = None
    account_input: str | None = None
    email_account: Callable | None = None

    def wallet_name(self, account: str | None = None) -> str:
        """How one of this source's wallets is shown: "GCash", or "GCash · 0917123"
        with its account_input value."""
        return f"{self.name} · {account}" if account else self.name


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
            to_transactions=gcash.to_transactions,
            summary=gcash.summary,
            is_correction_row=gcash.is_correction_row,
            balance_corrections=gcash.balance_corrections,
            balance_warning=gcash.balance_warning,
            email_query=(
                'from:noreply-transhistory@gcash.com subject:"Your GCash Transaction History" '
                "has:attachment filename:pdf"
            ),
            attachment_pattern=r"\.pdf$",
            password_inputs=("last_name", "mobile_last4"),
            build_password=gcash.build_password,
            account_input="mobile_first7",
            email_account=gcash.email_account,
        ),
        Source(
            key="gotyme",
            name="GoTyme",
            password_env="GOTYME_PDF_PASSWORD",
            columns=gotyme.COLUMNS,
            amount_columns=("Credits", "Debits", "Running Balance"),
            money_in_columns=("Credits",),
            money_out_columns=("Debits",),
            text_column="Details",
            parse=gotyme.parse_transactions,
            statement_balances=gotyme.statement_balances,
            summary=gotyme.summary,
            to_transactions=gotyme.to_transactions,
            email_query=(
                'from:no-reply@gotyme.com.ph subject:"GoTyme Bank Statement of Account" '
                "has:attachment filename:pdf"
            ),
            # One email carries two PDFs: this Everyday account's, and the savings
            # (GoalSave) one, which needs its own source once its layout is known.
            attachment_pattern=r"^GoTymeStatementForEverydayDepositAccount_\d{8}_\d{6}\.pdf$",
            password_inputs=("birth_date",),
            build_password=gotyme.build_password,
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
