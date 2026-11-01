# Adding an e-wallet or bank

This guide walks you through adding a new statement source (Maya, GoTyme, a bank, …) next to GCash. The examples use **Maya** with the key `maya`.

A new source is **one parser module plus one `Source` entry**, with tests. It never touches YNAB code, HTML or the web app.

| File | What you add |
|---|---|
| `statement_etl/parsers/maya.py` | The parser: reads the PDF into rows, and `to_transactions(rows)` turns them into neutral `Transaction`s |
| `statement_etl/sources.py` | One `Source(...)` entry that describes Maya's columns and names its functions |
| `tests/test_parsers_maya.py`, `tests/test_sources.py` | Tests with made-up rows |

## How a statement flows

```
statement PDF
   │  parsers/maya.py      parse_transactions(pdf, password, on_page)
   ▼
rows, exactly as printed   [{"Date": "01/10/2026", "Details": "...", "Amount": "-50.00", ...}, ...]
   │  parsers/maya.py      to_transactions(rows)   (called by convert.rows_to_transactions)
   ▼
Transactions               Transaction(source="maya", key, date, payee, amount, memo, flagged)
   │  ynab_api/format.py   to_ynab(transaction): milliunits, import_id, flag_color
   ▼
YNAB transactions          {"date", "payee_name", "amount", "import_id", "memo", "flag_color"}
   │  ynab_api/            upload, plus the starting / ending balance checks
   ▼
YNAB (unapproved, ready for review)
```

Two rules keep this simple:

1. **The parser only reads.** Its job is to return every row and every column of the statement, with the column names and text exactly as printed. Don't rename, merge or skip columns, and don't drop rows. Every statement looks different, so there is no shared row format.
2. **Your source never knows about YNAB.** `to_transactions` reads each row by your statement's column names and returns a neutral `Transaction` (`statement_etl/transaction.py`): pesos as `Decimal`, no milliunits, no `import_id`. `ynab_api/format.py` does the YNAB part for every source.

The `Source` entry in `sources.py` names your functions and tells the command line and the web page how to show your columns.

## 1. Set up

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m unittest discover tests     # should pass before you change anything
```

## 2. Study your statement

Download **your own** statement and put it in `statements/`. That folder is gitignored, so the file can't be committed by accident. Never commit a real statement.

Print every word with its position to see how the table is laid out:

```python
# python -I scratch.py statements/maya.pdf
import sys, getpass, pdfplumber

with pdfplumber.open(sys.argv[1], password=getpass.getpass("PDF password: ")) as pdf:
    for word in pdf.pages[0].extract_words():
        print(f"{word['x0']:7.1f} {word['x1']:7.1f} {word['top']:7.1f}  {word['text']}")
```

Write down:

- **Every column name**, in the order printed. This becomes `COLUMNS`.
- Which columns hold **money**, and which of those are **money in** and **money out**. Some statements have Debit and Credit columns; others have one signed Amount column.
- The **date format**, e.g. `2026-10-05 08:15 AM` or `05 Oct 2026`.
- A **reference number** per transaction, if there is one.
- **Summary lines**: totals, starting balance, ending balance.
- How **long descriptions wrap** onto the next line.

## 3. Write the parser

Start from the GCash parser:

```bash
cp statement_etl/parsers/gcash.py statement_etl/parsers/maya.py
```

Work through these parts of `maya.py`:

| Section | What to change |
|---|---|
| Docstring and `main()` | Say "Maya". `main()` is the `python -m statement_etl.parsers.maya` command. |
| **STATEMENT SETTINGS** | `COLUMNS` (Maya's column names), the header labels, date/time regexes and format, amount and reference regexes, and the totals / balance / summary line patterns. |
| **PAGE LAYOUT** | `_find_columns()` finds the header row and the column edges. `_attach_wrapped_lines()` joins wrapped description lines. Adjust them if Maya's layout differs. |
| **ROW PARSING** | `_parse_transaction_line()` reads one table line. `_to_output_row()` returns the row: **one key per entry in `COLUMNS`, text exactly as printed**. |
| **CHECKING** | Keep `_check_totals()` if the statement prints totals: a mismatch means a row was misread, so parsing stops. |
| **BALANCE CORRECTION** | GCash adds correction rows when its own running balance doesn't add up. Optional: delete this section if you don't need it. |

Your parser must also provide these functions for the `Source` entry:

| Function | Returns | Used for |
|---|---|---|
| `parse_transactions(pdf, password, on_page=None)` | list of row dicts | everything. `pdf` is a file path or a file object (the web upload). Call `on_page(page, pages)` at the start of each page, if given: the page and the run log show it as progress. Raise `StatementError` (from `parsers/common.py`) with a clear message for a wrong password or a misread statement. The web page shows that message as it is. |
| `statement_balances(rows)` | `{"starting": Decimal, "ending": Decimal}` | the YNAB balance checks before and after the upload |
| `to_transactions(rows)` | list of `Transaction`s, one per row | the upload. See step 4. |
| `summary(rows)` *(optional)* | one line of text, or `None` | shown under the parsed rows, e.g. "Total debit … · Total credit … · matches statement ✓" |

`statement_etl/parsers/common.py` has helpers you can reuse: `open_pdf()` (handles a wrong password), `group_lines()` (words → text lines) and `to_decimal()` (`"1,234.50"` → `Decimal`).

Check your parser on your own PDF and compare the output with the PDF: the row count, the first and last rows, and a wrapped description.

```bash
python -m statement_etl.parsers.maya statements/maya.pdf -o /tmp/maya.json
```

## 4. Write `to_transactions`

At the end of `maya.py`, add `to_transactions(rows)`. Use `gcash.to_transactions` as the example, and put a made-up input row in a comment above it, as GCash does:

```python
from statement_etl.transaction import Transaction


def to_transactions(rows):
    """Convert Maya rows into neutral Transactions, one per row."""
    return [
        Transaction(
            source="maya",  # the Source key
            key=row["Reference ID"],  # stable id for this transaction
            date=datetime.strptime(row["Date"], "%d/%m/%Y"),  # Maya's own column names
            payee=row["Details"],
            amount=to_decimal(row["Amount"]),  # pesos; negative = money out
            memo="Maya Ref: " + row["Reference ID"],
        )
        for row in rows
    ]
```

- **Sign**: money out is negative, money in is positive.
- **`key`** becomes YNAB's `import_id` (`make_import_id("maya", key)` in `ynab_api/format.py`), which stops duplicates when the same statement is uploaded twice. It must be **unique and stable** for each transaction. Use the statement's reference number. If there isn't one, build a value that never changes for that row, e.g. the date, amount, running balance and a count of identical rows. `to_transactions` gets all the rows at once so it can count them. Then also end the memo with `transaction_ref("maya", key)` (`statement_etl/transaction.py`), the hash part of the `import_id`, as GoTyme does, so the row can be found by searching YNAB.
- **Correction rows** (if your parser adds them) get `flagged=True`, so they show with a red flag for review in YNAB.

## 5. Register the source

In `statement_etl/sources.py`, import your parser and add an entry to `SOURCES`:

```python
from statement_etl.parsers import gcash, maya

        Source(
            key="maya",
            name="Maya",
            password_env="MAYA_PDF_PASSWORD",
            columns=maya.COLUMNS,
            amount_columns=("Amount", "Running Balance"),
            money_in_columns=(),
            money_out_columns=(),
            text_column="Details",
            parse=maya.parse_transactions,
            statement_balances=maya.statement_balances,
            to_transactions=maya.to_transactions,
            summary=maya.summary,
        ),
```

Check it. The `convert` command prints the YNAB transactions your rows become:

```bash
python -m statement_etl.convert /tmp/maya.json --source maya
```

Then add `MAYA_PDF_PASSWORD=` to `.env.example`.

### `Source` fields

| Field | Required | Meaning |
|---|---|---|
| `key` | yes | `"maya"`: the `--source` value, and part of every `import_id` (as `Transaction.source`). **Never rename it after release**, or YNAB will import duplicates. |
| `name` | yes | `"Maya"`: shown in the page's Source dropdown and in command-line output |
| `password_env` | yes | Environment variable with the PDF password, for the command line |
| `columns` | yes | Every column, in the order the statement prints them |
| `parse` | yes | `parse_transactions` |
| `statement_balances` | yes | `statement_balances` |
| `to_transactions` | yes | `to_transactions` |
| `amount_columns` | no | Money columns: right-aligned |
| `money_in_columns` | no | Shown green on the page (e.g. `Credit`). Leave empty for a single signed Amount column. |
| `money_out_columns` | no | Shown red on the page (e.g. `Debit`) |
| `text_column` | no | Long text that should wrap (e.g. `Description`) |
| `summary` | no | The line under the parsed rows |
| `is_correction_row`, `balance_corrections`, `balance_warning` | no | Only if your parser adds balance-correction rows. See the GCash parser. |
| `email_query`, `attachment_pattern`, `password_inputs`, `build_password` | no | Only if statement PDFs arrive by email and can be imported automatically. See [Auto-import from Gmail (optional)](#auto-import-from-gmail-optional). |

## 6. Add tests

`tests/test_sources.py` already checks every registered source automatically:

- it has a `to_transactions` function
- its column settings name real columns
- its password variable name ends in `_PDF_PASSWORD`

Add a `MayaTest` class modelled on `GCashTest`, using **made-up rows** in the shape your parser returns:

- every row has exactly the keys in `maya.COLUMNS`
- `to_ynab(maya.to_transactions(rows)[0])` gives the right date, amount (in milliunits), payee and memo
- the `import_id` is the same when the row is converted twice
- `maya.statement_balances` and `maya.summary` give the expected values

Then add `tests/test_parsers_maya.py` for the parser itself, modelled on `tests/test_parsers_gcash.py`. It fakes the PDF with word positions (helpers in `tests/fakes.py`), so you can test reading the table, wrapped lines and your totals check without a real statement. Test `to_transactions` there too, as `ToTransactionsTest` does for GCash.

```bash
python -m unittest discover tests
```

## 7. Check the web page

You don't edit the HTML. The page builds itself from your `Source` entry through the JSON that `/api/parse` returns:

| `Source` field | Where it shows on the page |
|---|---|
| `key`, `name` | The **Source** dropdown in step ①, and "Parsing successful: N Maya transactions…" |
| `columns` | The preview table's headers and cells, in this order |
| `amount_columns` | Right-aligned numbers |
| `money_in_columns` / `money_out_columns` | Green / red amounts |
| `text_column` | The cell that wraps long text |
| `summary` | The line under the preview table |
| `balance_corrections`, `balance_warning`, `is_correction_row` | The yellow "Balance does not add up" box and highlighted rows |
| `statement_balances` | The starting and ending balance checks in step ④ |

```bash
python -m statement_etl.web
```

Open http://localhost:5050, pick **Maya**, parse your PDF, and check:

- every column is shown, in order
- amounts are right-aligned and coloured correctly
- the summary line and the starting balance are right
- it also looks right at phone width (below 560px) and in dark mode

If something looks wrong, fix your `Source` fields, not the HTML.

## 8. Try it end to end

Use a **test YNAB plan or account**, not your real budget.

```bash
python -m statement_etl.pipeline statements/maya.pdf --source maya --dry-run
python -m statement_etl.pipeline statements/maya.pdf --source maya --plan-id <test plan> --account-id <test account>
```

Then upload the same statement again, from the command line or the web page. YNAB should skip every transaction ("Skipped N already imported"). The starting and ending balance checks should match.

## 9. Open a pull request

Checklist:

- [ ] No PDFs, JSON dumps, screenshots or real names, account numbers or reference numbers in the commits. Test data is made up.
- [ ] `python -m unittest discover tests` passes.
- [ ] The parser returns every row and column as printed, and the totals check passes on your statement.
- [ ] Uploading the same statement twice creates no duplicates.
- [ ] The key (`maya`) is final.
- [ ] `MAYA_PDF_PASSWORD=` is in `.env.example`, and Maya is in the README's "currently supports" line.
- [ ] The PR says which statement type you tested with (e.g. "Maya Wallet transaction history, Oct 2026"), without attaching it.

---

## Auto-import from Gmail (optional)

In hosted mode, `/auto-import` polls Gmail for new statement emails and imports each matching PDF attachment to the mapped YNAB account using the same `parse_statement()` and `import_statement()` pipeline as manual upload.

A source opts in by setting four optional fields on its `Source` entry in `statement_etl/sources.py`:

| Field | Type | Meaning |
|---|---|---|
| `email_query` | `str \| None` | The Gmail search query that finds this source's statement emails. Sources with `email_query=None` (the default) work normally on the CLI and manual upload page, and simply don't appear on `/auto-import`. |
| `attachment_pattern` | `str \| None` | Regex matched against attachment filenames to pick the right PDF when one email carries several attachments. Required whenever `email_query` is set. |
| `password_inputs` | `tuple[str, ...]` | Input names the user enters once on `/auto-import` (stored encrypted) so background runs can open the encrypted PDF, e.g. `("last_name", "mobile_last4")`. Leave empty `()` if the PDF is not password-protected. |
| `build_password` | `Callable \| None` | Function in the parser module taking the `dict` of `password_inputs` values (or positional inputs in unit tests) and returning the PDF password string. Required whenever `password_inputs` is non-empty. |

**Example: GCash (`gcash`)**

GCash statement emails explain the password rule (last name in lowercase without spaces + last 4 digits of the mobile number) without printing the digits, so the user enters those two inputs once:

```python
# statement_etl/parsers/gcash.py
def build_password(inputs: Mapping[str, str] | str, mobile_last4: str | None = None) -> str:
    """GCash PDF password: last name in lowercase (no spaces) + last 4 digits of mobile.

    Accepts the `Source.build_password` input dict or `(last_name, mobile_last4)`:
        {"last_name": "Dela Cruz", "mobile_last4": "1234"} -> "delacruz1234"
        "Dela Cruz", "1234" -> "delacruz1234"
    """
    ...
```

```python
# statement_etl/sources.py
        Source(
            key="gcash",
            ...,
            email_query=(
                'from:noreply-transhistory@gcash.com subject:"Your GCash Transaction History" '
                "has:attachment filename:pdf"
            ),
            attachment_pattern=r"\.pdf$",
            password_inputs=("last_name", "mobile_last4"),
            build_password=gcash.build_password,
        ),
```

**Example: GoTyme (`gotyme`)**

One GoTyme statement email (from `no-reply@gotyme.com.ph`, subject "GoTyme Bank Statement of Account as of DD Mon YYYY") carries **two** PDFs:

- `GoTymeStatementForEverydayDepositAccount_YYYYMMDD_HHMMSS.pdf`: the Everyday account, which `gotyme` parses
- `GoTyme_BankStatement_SavingsAccounts_YYYYMMDD_HHMMSS.pdf`: savings (GoalSave), a different layout that isn't supported yet

So `attachment_pattern` matches the Everyday file name exactly, rather than any `.pdf`. The savings PDF is never downloaded. The password is the date of birth as `MMDDYYYY`, which the email doesn't contain, so the user enters the date once:

```python
# statement_etl/parsers/gotyme.py
def build_password(inputs: Mapping[str, str]) -> str:
    """GoTyme PDF password: date of birth as MMDDYYYY.

    ...
        {"birth_date": "1990-01-31"} -> "01311990"
    """
    return date.fromisoformat(inputs["birth_date"]).strftime("%m%d%Y")
```

```python
# statement_etl/sources.py
        Source(
            key="gotyme",
            ...,
            email_query=(
                'from:no-reply@gotyme.com.ph subject:"GoTyme Bank Statement of Account" '
                "has:attachment filename:pdf"
            ),
            attachment_pattern=r"^GoTymeStatementForEverydayDepositAccount_\d{8}_\d{6}\.pdf$",
            password_inputs=("birth_date",),
            build_password=gotyme.build_password,
        ),
```

`/auto-import` builds each source's row from these fields, so adding a source needs no page or runner change. The one exception is a new input name. Give it an entry in `INPUT_FIELDS` in `statement_etl/web/auto_import.py`, with its label, field type and format check. For example, `birth_date` is a date field labelled "Date of birth". A name without an entry still works, but shows as a plain password field. The first line of `build_password`'s docstring is shown under the inputs as the password rule.

`tests/test_sources.py` verifies automatically that every source with `email_query` also sets `attachment_pattern`, and every source with `password_inputs` also sets `build_password`.

---

## Worked example: GoTyme (`gotyme`)

GoTyme Bank Everyday Deposit Account statements (`statement_etl/parsers/gotyme.py`) show how a source without transaction reference numbers works:

- **Columns:** `("Date", "Details", "Credits", "Debits", "Running Balance")`.
- **Wrapped Details:** a long `Details` line wraps onto two lines—the first about 4 pt above the date and amounts line, and the second about 4 pt below it—so `_anchor_rows()` anchors one row per date line and collects `Details` words in that row's vertical band.
- **Four checks in `_check_totals()`:** `Σ Credits = Total Credit`, `Σ Debits = Total Debit`, `Opening + Σ Credits − Σ Debits = Closing`, and every row's `Running Balance = previous + Credits − Debits`.

**GoTyme row → `Transaction` → YNAB `NewTransaction`**. The middle column is the neutral `Transaction` that `gotyme.to_transactions()` returns; the YNAB column is what `ynab_api.format.to_ynab()` makes of it. `ynab_api/client.import_transactions()` sends them all in one `POST /plans/{plan_id}/transactions`. Another budgeting app would start from the same `Transaction`.

| Statement column | `Transaction` field | YNAB field | Rule | Example (made up) |
|---|---|---|---|---|
| — | — | `account_id` | the account picked on the page or in auto-import | `"3f2a…"` |
| `Date` | `date` | `date` | `strptime(Date, "%d %b %Y")`; YNAB gets `YYYY-MM-DD` | `2026-10-05` → `"2026-10-05"` |
| `Credits`, `Debits` | `amount` (pesos) | `amount` (milliunits) | `Credits − Debits`; YNAB gets × 1000; inflow +, outflow − | `Decimal("-249.00")` → `-249000` |
| `Details` | `payee` | `payee_name` | `gotyme_payee()`; cut to 200 characters by the YNAB client | `"SHOPEE PH"` |
| `Details` | `memo` | `memo` | `"GoTyme: " + Details + " \| Ref: " + transaction_ref("gotyme", key)` | `"GoTyme: Card payment at SHOPEE PH, Card •••• 1234 \| Ref: 447d649bcac761c22c6a2807652d43"` |
| `Date`, `Credits`, `Debits`, `Running Balance` | `source`, `key` | `import_id` | `key = "…\|n"`; YNAB gets `make_import_id(source, key)` | `"05 Oct 2026\|0.00\|249.00\|4751.00\|0"` → `"ETLPH:9c1e…"` |
| — | `flagged` = `False` | `flag_color` = `null` | no correction rows | |
| — | — | `cleared` / `approved` | fixed by the YNAB client: `"cleared"` / `False` (you review them in YNAB) | |
| — | — | `payee_id`, `category_id` | not sent; YNAB matches the payee name and applies your payee rules | |

Statement values that aren't sent as transactions:
- Opening and Closing balance feed the starting- and ending-balance checks.
- Period and the totals are used only by `_check_totals()`.
- Running Balance is used only in `key` (so `import_id`) and the checks.

---

## Changing the web page itself

For UI fixes or improvements, unrelated to adding a source.

**Where things are.** The page is a single file, `statement_etl/web/templates/index.html`, with no build step:

- `<style>` at the top: colours for light and dark mode (`prefers-color-scheme`) and the phone layout (`max-width: 560px`)
- four cards in the HTML: ① Statement, ② YNAB, ③ Plan & Account, ④ Upload
- `<script>` at the bottom, in sections that match the cards (`// ---- ① Statement ----` and so on), plus shared helpers (`renderTable`, `message`, `api`) and the restore-on-load code at the end

**The API it calls**, in `statement_etl/web/upload.py` (② YNAB connection: `statement_etl/web/ynab_auth.py`):

| Route | Used by |
|---|---|
| `POST /api/parse` | ① Parse. Form fields: `source`, `pdf`, `password` |
| `GET /api/status` | Restoring the page after a reload or the OAuth redirect |
| `POST /api/token` | ② Connect with a personal access token |
| `/oauth/login`, `/oauth/callback`, `/oauth/logout` | ② YNAB login (only when OAuth is set up) |
| `GET /api/plans`, `GET /api/plans/<plan_id>/accounts` | ③ Plan and Account dropdowns |
| `POST /api/starting-balance` | ④ The check before the upload |
| `POST /api/upload` | ④ Upload, plus the ending balance check |

`/api/parse` returns the fields below, so UI changes never need to know a statement's column names:

| Field | Meaning |
|---|---|
| `source`, `source_name`, `filename` | What was parsed |
| `columns`, `rows` | The statement's own columns, and each row as a list in that order |
| `amount_columns`, `money_in_columns`, `money_out_columns`, `text_column` | How to style the columns |
| `summary` | The line under the table, or `null` |
| `statement_count`, `count` | Rows on the statement, and transactions to upload (corrections included) |
| `correction_rows`, `corrections`, `balance_warning` | Balance-correction rows (indexes into `rows`, details, warning text) |
| `starting_balance`, `ending_balance` | From the statement, formatted |

**Rules for the page:**

- Keep everything in the one file. Don't add CDNs, external scripts, fonts or trackers. The app runs on the user's own computer, and statement data must never leave it.
- Don't use `localStorage`, `sessionStorage` or cookies for data. The access token and the parsed rows stay on the server, in memory.
- Insert statement text with `textContent` (as `renderTable` does), never `innerHTML`: it comes from a PDF.
- Don't hardcode column names in JavaScript. Use the `/api/parse` fields.
- Keep the step order ① → ④. A balance mismatch is always a warning, never a block.

**Testing a UI change.** There is no JavaScript test suite, so test by hand and say in the PR what you checked:

- run `python -m statement_etl.web` and go through all four steps with your own statement and a test YNAB account
- reload the page partway through; it should restore where you were
- check phone width and dark mode
- `python -m unittest discover tests` still passes
