"""
ph-statement-import - helpers shared by every statement parser.

Each parser (e.g. parsers/gcash.py) reads one e-wallet's or bank's PDF and
returns every row with the statement's own column names. These helpers don't
depend on any statement format:

    StatementError - a problem with the statement, worded for the user
    open_pdf()     - open a password-protected PDF
    group_lines()  - turn a page's words into text lines
    to_decimal()   - turn amount text into a number

To add another e-wallet / bank, see docs/adding-an-e-wallet.md.
"""

from decimal import Decimal

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

# Words whose tops differ by less than this many points are on the same line
LINE_TOLERANCE = 3


# =============================================================================
# ERRORS - problems with the statement that the user can fix
# =============================================================================


class StatementError(ValueError):
    """A problem with the statement itself, with a message for the user.

    The web page shows the message as it is, e.g. "Incorrect PDF password.",
    so put only text in it that is safe and useful to show. Any other error
    gets a fixed message instead, because a library's message can carry
    internal details. A ValueError subclass, so `except ValueError` still works.
    """


# =============================================================================
# OPENING - open the password-protected PDF
# =============================================================================


def open_pdf(pdf_path, password=""):
    """Open a password-protected PDF with pdfplumber; use it in a `with` block.

    A PDF that isn't encrypted opens with any password.
    Raises StatementError if the password is wrong.
    """
    try:
        return pdfplumber.open(pdf_path, password=password)
    except PdfminerException as e:
        if e.args and isinstance(e.args[0], PDFPasswordIncorrect):
            raise StatementError("Incorrect PDF password.") from None
        raise


# =============================================================================
# PAGE LAYOUT - turn a page's words into lines
#
# pdfplumber gives every word with its position on the page:
#   {"text": "Debit", "x0": 410.2, "x1": 432.8, "top": 180.5, ...}
#   x0 / x1 = left / right edge, top = distance from the top of the page.
# =============================================================================


def group_lines(words):
    """Group pdfplumber words into lines (sorted top-to-bottom, left-to-right).

    Returns a list of {"top": float, "words": [word, ...], "text": "joined words"}.
    """
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        # Same height as the previous line -> same line; otherwise start a new one
        if lines and abs(lines[-1]["top"] - w["top"]) < LINE_TOLERANCE:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["text"] = " ".join(w["text"] for w in line["words"])
    return lines


# =============================================================================
# AMOUNTS
# =============================================================================


def to_decimal(text):
    """Turn amount text into a number: "1,234.50" -> Decimal("1234.50"), "" -> 0."""
    return Decimal(text.replace(",", "")) if text else Decimal(0)
