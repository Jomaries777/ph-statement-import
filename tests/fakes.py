"""
Shared fakes for the tests: pdfplumber words and pages, GCash rows, YNAB SDK
objects. Importing this module also loads the safety net in tests/__init__.py.

All data here is made up. Never put a real statement, name, account or
reference number in a test.
"""

from types import SimpleNamespace
from unittest import mock

# =============================================================================
# PDF WORDS - in the shape pdfplumber's page.extract_words() returns
# =============================================================================

CHAR_WIDTH = 5  # every fake word is 5 points wide per character


def word(text, x0, top=100.0):
    """One pdfplumber word starting at x0."""
    return {"text": text, "x0": float(x0), "x1": float(x0 + CHAR_WIDTH * len(text)), "top": float(top)}


def right_word(text, x1, top=100.0):
    """One right-aligned word (an amount) ending at x1."""
    return word(text, x1 - CHAR_WIDTH * len(text), top)


def words_from(text, x0, top=100.0):
    """Words of `text` laid out left to right from x0, one space apart."""
    words = []
    for part in text.split():
        words.append(word(part, x0, top))
        x0 += CHAR_WIDTH * (len(part) + 1)
    return words


class FakePage:
    def __init__(self, words):
        self.words = words

    def extract_words(self, **kwargs):
        return list(self.words)


class FakePDF:
    """Stands in for pdfplumber.open(): a context manager with .pages."""

    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# =============================================================================
# GCASH TABLE LAYOUT - x positions of each column on a fake GCash page
# =============================================================================

DESC_X = 130     # Description starts here (after the AM/PM word)
REF_X = 330      # Reference No. starts here
DEBIT_X1 = 450   # right edge of the "Debit" label and debit amounts
CREDIT_X1 = 510  # right edge of the "Credit" label and credit amounts
BALANCE_X1 = 580  # right edge of the "Balance" label and balances


def gcash_header(top):
    """The table header line of a GCash page."""
    return (
        words_from("Date and Time", 20, top)
        + [word("Description", DESC_X, top), word("Reference", REF_X, top), word("No.", REF_X + 50, top)]
        + [right_word("Debit", DEBIT_X1, top), right_word("Credit", CREDIT_X1, top),
           right_word("Balance", BALANCE_X1, top)]
    )


def gcash_line(top, date_time, description, reference, debit="", credit="", balance="0.00"):
    """One GCash transaction line: "2026-10-01 09:00 AM <description> <ref> <amount> <balance>"."""
    day, clock, am_pm = date_time.split()
    words = [word(day, 20, top), word(clock, 75, top), word(am_pm, 105, top)]
    words += words_from(description, DESC_X, top)
    words.append(word(reference, REF_X, top))
    if debit:
        words.append(right_word(debit, DEBIT_X1, top))
    if credit:
        words.append(right_word(credit, CREDIT_X1, top))
    words.append(right_word(balance, BALANCE_X1, top))
    return words


def gcash_row(date_time, description, reference, debit="", credit="", balance="0.00"):
    """A parsed GCash row, as gcash.parse_transactions() returns it."""
    return {
        "Date and Time": date_time,
        "Description": description,
        "Reference No.": reference,
        "Debit": debit,
        "Credit": credit,
        "Balance": balance,
    }


# =============================================================================
# YNAB SDK - stand-ins for the SDK's models and API client
# =============================================================================

PLAN_ID = "11111111-1111-1111-1111-111111111111"
ACCOUNT_ID = "22222222-2222-2222-2222-222222222222"


def fake_client():
    """A MagicMock usable as `with ynab_client(token) as api_client:`."""
    client = mock.MagicMock()
    client.__enter__.return_value = client
    return client


def response(**data):
    """An SDK response: response(transactions=[...]).data.transactions."""
    return SimpleNamespace(data=SimpleNamespace(**data))


def ynab_txn(**fields):
    """A YNAB transaction as the SDK returns it."""
    defaults = {
        "id": "txn-1",
        "import_id": None,
        "deleted": False,
        "payee_name": "Test Payee",
        "category_name": None,
        "amount": 0,
        "memo": None,
        "var_date": "2026-10-01",
        "flag_color": None,
    }
    return SimpleNamespace(**{**defaults, **fields})


def ynab_account(**fields):
    """A YNAB account as the SDK returns it."""
    defaults = {
        "id": ACCOUNT_ID,
        "name": "Test Wallet",
        "type": SimpleNamespace(value="checking"),
        "balance": 0,
        "balance_formatted": "₱0.00",
        "deleted": False,
        "closed": False,
    }
    return SimpleNamespace(**{**defaults, **fields})
