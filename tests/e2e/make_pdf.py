"""
Made-up GCash statement PDFs for the tests, written by hand (no dependencies).

The page is laid out the way parsers/gcash.py reads a GCash Transaction History:

    Transaction History                       (title, above the table: ignored)
    Date and Time  Description  Reference No.  Debit  Credit  Balance
    STARTING BALANCE                                           1000.00
    2025-09-01 08:15 AM  Received GCash ...  9990000000001     500.00  1500.00
    2025-09-02 12:40 PM  Payment to ...      9990000000002  150.25     1349.75
                         Merchant ...        (wrapped description line)
    ...
    ENDING BALANCE                                             1250.00
    Total Debit   500.00
    Total Credit  750.00

Text is Helvetica at fixed x / y positions; amounts are right-aligned under
the Debit / Credit / Balance labels. The PDF isn't encrypted, so it opens with
any password. Every name and reference number here is made up.

    make_statement(path, rows, starting, ending)  - any statement
    balanced(path)  - adds up: no correction rows
    with_gap(path)  - doesn't add up: STARTING BALANCE CORRECTION, "Refund from"
                      and MISSING TRANSACTIONS rows

Usage:
    python -m tests.e2e.make_pdf out.pdf            # balanced
    python -m tests.e2e.make_pdf out.pdf --gap      # with_gap
"""

import sys
from collections import namedtuple
from decimal import Decimal

# One statement row. Amounts are text as printed ("150.25"), "" for a blank
# cell. A "\n" in the description wraps the rest onto a second line below.
Row = namedtuple("Row", "date_time description reference debit credit balance")

PAGE_WIDTH, PAGE_HEIGHT = 612, 792
FONT_SIZE = 7

# Column positions in points from the left edge (see the layout above)
DATE_X = 30
DESC_X = 110
REF_X = 330
DEBIT_X1 = 440    # right edge of the "Debit" label and debit amounts
CREDIT_X1 = 500   # right edge of the "Credit" label and credit amounts
BALANCE_X1 = 570  # right edge of the "Balance" label and balances

TITLE_TOP = 50
HEADER_TOP = 90
ROW_GAP = 24      # distance between table lines
WRAP_GAP = 9      # a wrapped description line sits this far below its row

# Helvetica widths (1/1000 of the font size) of the characters that get
# right-aligned: amounts and the Debit / Credit / Balance labels
WIDTHS = {
    **dict.fromkeys("0123456789", 556),
    ".": 278, ",": 278, "-": 333,
    "B": 667, "C": 722, "D": 722,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556,
    "i": 222, "l": 222, "n": 556, "r": 333, "t": 278,
}


def _width(text):
    return sum(WIDTHS[c] for c in text) * FONT_SIZE / 1000


# =============================================================================
# READY-MADE STATEMENTS
# =============================================================================

# Adds up: 1000.00 + 750.00 credit - 500.00 debit = 1250.00
BALANCED_STARTING = "1000.00"
BALANCED_ENDING = "1250.00"
BALANCED_ROWS = [
    Row("2025-09-01 08:15 AM", "Received GCash from Sample Sender", "9990000000001",
        "", "500.00", "1500.00"),
    Row("2025-09-02 12:40 PM",
        "Payment to Sample Coffee Shop,\nMerchant Transaction Number: TEST0000001",
        "9990000000002", "150.25", "", "1349.75"),
    Row("2025-09-03 06:05 PM", "Cash-in from Test Bank", "9990000000003",
        "", "250.00", "1599.75"),
    Row("2025-09-05 09:30 AM", "Payment to Example Groceries", "9990000000004",
        "349.75", "", "1250.00"),
]

# Doesn't add up. The parser adds three correction rows:
#   S9990000000011  STARTING BALANCE CORRECTION        +100.00  (row 1 is 100 too high)
#   R9990000000013  Refund from Sample Coffee Shop      +50.00  (row 3 is 50 too high)
#   M0000.........  MISSING TRANSACTIONS IN GCASH STATEMENT  -50.00  (row 4 is 50 too low)
GAP_STARTING = "1000.00"
GAP_ENDING = "1400.00"
GAP_ROWS = [
    Row("2025-09-01 08:15 AM", "Received GCash from Sample Sender", "9990000000011",
        "", "500.00", "1600.00"),
    Row("2025-09-02 12:40 PM", "Payment to Sample Coffee Shop", "9990000000012",
        "200.00", "", "1400.00"),
    Row("2025-09-03 06:05 PM", "Payment to Example Groceries", "9990000000013",
        "100.00", "", "1350.00"),
    Row("2025-09-04 09:30 AM", "Cash-in from Test Bank", "9990000000014",
        "", "100.00", "1400.00"),
]


def balanced(path):
    """A statement whose running balance adds up (4 rows, one wrapped)."""
    make_statement(path, BALANCED_ROWS, BALANCED_STARTING, BALANCED_ENDING)


def with_gap(path):
    """A statement whose running balance breaks (4 rows + 3 correction rows)."""
    make_statement(path, GAP_ROWS, GAP_STARTING, GAP_ENDING)


# =============================================================================
# PAGE LAYOUT - rows -> (x, top, text) placements
# =============================================================================


def _right(text, x1, top):
    """Text right-aligned so it ends at x1."""
    return (x1 - _width(text), top, text)


def _layout(rows, starting, ending):
    items = [
        (DATE_X, TITLE_TOP, "Transaction History"),
        (DATE_X, TITLE_TOP + 12, "Sample Account Holder 09990000000"),
        (DATE_X, HEADER_TOP, "Date and Time"),
        (DESC_X, HEADER_TOP, "Description"),
        (REF_X, HEADER_TOP, "Reference No."),
        _right("Debit", DEBIT_X1, HEADER_TOP),
        _right("Credit", CREDIT_X1, HEADER_TOP),
        _right("Balance", BALANCE_X1, HEADER_TOP),
    ]
    top = HEADER_TOP + ROW_GAP
    if starting is not None:
        items += [(DATE_X, top, "STARTING BALANCE"), _right(str(starting), BALANCE_X1, top)]
        top += ROW_GAP

    debit = credit = Decimal(0)
    for row in rows:
        first, _, wrapped = row.description.partition("\n")
        items += [(DATE_X, top, row.date_time), (DESC_X, top, first), (REF_X, top, row.reference)]
        if wrapped:
            items.append((DESC_X, top + WRAP_GAP, wrapped))
        if row.debit:
            items.append(_right(row.debit, DEBIT_X1, top))
            debit += Decimal(row.debit)
        if row.credit:
            items.append(_right(row.credit, CREDIT_X1, top))
            credit += Decimal(row.credit)
        items.append(_right(row.balance, BALANCE_X1, top))
        top += ROW_GAP

    if ending is not None:
        items += [(DATE_X, top, "ENDING BALANCE"), _right(str(ending), BALANCE_X1, top)]
        top += ROW_GAP
    items += [(DATE_X, top, "Total Debit"), _right(f"{debit:.2f}", DEBIT_X1, top)]
    top += ROW_GAP
    items += [(DATE_X, top, "Total Credit"), _right(f"{credit:.2f}", CREDIT_X1, top)]
    return items


# =============================================================================
# PDF WRITER - one page, Helvetica text only
# =============================================================================


def _escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_bytes(items):
    """A one-page PDF with each (x, top, text) placed in Helvetica.

    top is measured from the top of the page, like pdfplumber's "top".
    """
    content = "".join(
        f"BT /F1 {FONT_SIZE} Tf {x:.2f} {PAGE_HEIGHT - top - FONT_SIZE:.2f} Td ({_escape(text)}) Tj ET\n"
        for x, top, text in items
    ).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
        f"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def make_statement(path, rows, starting=None, ending=None):
    """Write a GCash-style statement PDF to `path`.

    rows:     list of Row; Total Debit / Total Credit are their sums
    starting: STARTING BALANCE text (e.g. "1000.00"), or None to leave it out
    ending:   ENDING BALANCE text, or None to leave it out
    """
    with open(path, "wb") as f:
        f.write(_pdf_bytes(_layout(rows, starting, ending)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m tests.e2e.make_pdf out.pdf [--gap]")
    (with_gap if "--gap" in sys.argv[2:] else balanced)(sys.argv[1])
