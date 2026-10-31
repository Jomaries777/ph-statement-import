# P2. GoTyme parser (manual upload and CLI)

Status: Not started · Estimate: about 2–3 days, 2 PRs · Needs: P1 · Can run at the same time as P3

## Goal

Import **GoTyme Bank Everyday Deposit Account** statements, through the web page and the CLI, exactly like GCash. When this phase ends, picking "GoTyme" as the Source and uploading a GoTyme PDF parses every row, checks the totals and the running balance, and creates the transactions in YNAB, unapproved.

The source key is **`gotyme`**. It's final once released, because it's part of every `import_id`.

Out of scope: Gmail auto-import for GoTyme (P6), and the separate GoalSave savings statement (`gotyme_savings`, which needs a sample first).

## Before you start

- P1 merged: `Transaction`, `Source.to_transactions`, `ynab_api/format.py` and `pipeline.import_statement()` exist.
- A real GoTyme Everyday statement in `statements/`, **never committed**. Unlock it once to look at it, using your date of birth as `MMDDYYYY` (the PDF password rule).
- Read `docs/adding-an-e-wallet.md`. This phase follows it step by step.
- **Tests use made-up rows only.** Never copy names, account digits or amounts from the real statement into code, tests, docs or PR descriptions.

## What the statement looks like

- **Columns:** `Date`, `Details`, `Credits`, `Debits`, `Running Balance`.
  - "Running Balance" prints on two header lines; "Running" sits about 4 pt above the rest of the header.
  - Left edges (pt): Date ≈ 18.8, Details ≈ 71.2.
  - Credits, Debits and Running Balance are right-aligned, ending near 352.6, 388.2 and 440.3.
- **Dates:** `05 Oct 2026` (`%d %b %Y`), with no time.
- **Amounts:** `1,234.56` with no sign. Each row has a credit **or** a debit; the other is `0.00`.
- **No reference numbers.**
- **Summary box on page 1:** `Period: DD Mon YYYY - DD Mon YYYY`, `Opening balance PHP …`, `Total Credit PHP …`, `Total Debit PHP …`, `Closing balance PHP …`.
- **Wrapped Details:** a long Details value prints on two lines. The first sits about 4 pt **above** the date and amount line, the second about 4 pt **below** it. For example: "Transfer to JUAN DELA CRUZ BPI, Account No." above, and "•••••••1234" below.
- The header repeats on every page.
- Ignore the footer lines ("All figures are in PHP.", the address, "Document ID", "Request Date", "Page N of M") and any page with no table.

## Steps

### PR 1: `feat/gotyme-parser`, "feat: parse GoTyme Everyday Deposit statements"

- [ ] Create `statement_etl/parsers/gotyme.py`. Use the same layout and comment style as `parsers/gcash.py`: a module docstring with usage, `# ===` sections and numbered step comments. Reuse `parsers/common.py`: `open_pdf()` (a wrong password raises `StatementError`), `group_lines()` and `to_decimal()`.
  - `COLUMNS = ("Date", "Details", "Credits", "Debits", "Running Balance")`
  - `parse_transactions(pdf, password) -> list[dict]`: one dict per row, with keys equal to `COLUMNS`.
    - Amounts are plain text without commas (`"249.00"`), like GCash's output. Wrapped Details lines are joined with one space.
    - Steps:
      1. `_read_summary(page)`: Period, Opening, Total Credit, Total Debit, Closing
      2. `_find_columns(page)`: column edges from the header words, on every page
      3. `_anchor_rows(lines, columns)`: one row per date line. Details words between this date's band and the next date's band belong to this row, which covers text both above and below the amounts.
      4. `_to_output_row(...)`
      5. `_check_totals(rows, summary)`
  - `_check_totals` raises `StatementError` with an actionable message when any of these fails:
    - Σ Credits = Total Credit
    - Σ Debits = Total Debit
    - Opening + Σ Credits − Σ Debits = Closing
    - each row's Running Balance = previous + Credits − Debits
  - `statement_balances(rows)`: `{"starting": first Running Balance − Credits + Debits, "ending": last Running Balance}`, computed the same way as GCash. The running-balance check guarantees these equal the printed Opening and Closing balances.
  - `summary(rows)`: e.g. `"Total credit 1,000.00 · Total debit 249.00 · matches statement ✓"`.
  - A `main()` CLI like GCash's: `python -m statement_etl.parsers.gotyme <pdf> [-o rows.json]`, with the password from `GOTYME_PDF_PASSWORD` or a prompt.
- [ ] `tests/test_parsers_gotyme.py`, with **made-up** words and positions faked the way `tests/test_parsers_gcash.py` does it:
  - a one-line row
  - a wrapped Details row, with the first line above and the second below the amounts
  - the header repeated on page 2, and the footer ignored
  - a page with no table ignored
  - each of the four checks failing → `StatementError` naming the check
  - a wrong password → `StatementError`
  - `statement_balances` and `summary` on made-up rows
- [ ] Check locally against your real statement. Don't paste the output into the PR:
  ```bash
  python -m statement_etl.parsers.gotyme statements/<gotyme>.pdf -o /tmp/gotyme-rows.json
  ```
  Expect no error, the same number of rows as the statement shows, and `statement_balances()` equal to the printed Opening and Closing balances.

### PR 2: `feat/gotyme-source`, "feat: add GoTyme as a source for manual upload and CLI"

- [ ] In `parsers/gotyme.py`, add `gotyme_payee(details)` and `to_transactions(rows) -> list[Transaction]`, with an input-row comment block like GCash's. It knows nothing about YNAB; `ynab_api.format.to_ynab()` does the YNAB part.
  - **Payee** rules:
    - `Card payment at MERCHANT, Card •••• NNNN` → `MERCHANT`
    - `Transfer to/from GoalSave` → `GoalSave`
    - `Transfer to/from NAME BANK, Account No. •••NNNN` → the text before `, Account No.`
    - anything else (`Qr Payment`, `Bnpl - Deposit`, `Refund for card payment`, `ATM withdrawal fee`) → Details as is
  - **Memo:** `"GoTyme: " + Details`, always, so the card or account digits the payee drops are still visible.
  - **`key`:** `f"{Date}|{Credits}|{Debits}|{Running Balance}|{n}"`. `n` counts earlier rows in the same statement with the same four values, so it's almost always 0. This is why `to_transactions` takes all the rows at once.
    - YNAB's `import_id` becomes `make_import_id("gotyme", key)`.
    - Running Balance makes the key stable: a later statement covering the same days gives the same keys, so YNAB skips them.
  - **`date`** from `Date` (`%d %b %Y`); **`amount`** = Credits − Debits in pesos; **`flagged`** = `False`, since GoTyme has no correction rows.
- [ ] Register the source in `statement_etl/sources.py`:
  ```python
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
  )
  ```
  No auto-import fields yet; those come in P6. `is_correction_row` and the other correction hooks keep their defaults, because GoTyme's running balance is checked instead.
- [ ] `.env.example`: add `GOTYME_PDF_PASSWORD=` with a comment: "your date of birth as MMDDYYYY".
- [ ] README: change "currently supports **GCash**" to "**GCash** and **GoTyme**" (Everyday Deposit Account).
- [ ] `docs/getting-started.md`:
  - "What you need": how to get a GoTyme statement (requested in the GoTyme app; it arrives by email).
  - Step 5: GoTyme in the source list.
  - Troubleshooting, "Wrong password": GoTyme's password is your date of birth as `MMDDYYYY`, e.g. `01311990`.
- [ ] `docs/adding-an-e-wallet.md`: add GoTyme as a second worked example, including this table:

  **GoTyme row → `Transaction` → YNAB `NewTransaction`**. The middle column is the neutral `Transaction` that `gotyme.to_transactions()` returns; the YNAB column is what `ynab_api.format.to_ynab()` makes of it. `ynab_api/client.import_transactions()` sends them all in one `POST /plans/{plan_id}/transactions`. Another budgeting app would start from the same `Transaction`.

  | Statement column | `Transaction` field | YNAB field | Rule | Example (made up) |
  |---|---|---|---|---|
  | — | — | `account_id` | the account picked on the page or in auto-import | `"3f2a…"` |
  | `Date` | `date` | `date` | `strptime(Date, "%d %b %Y")`; YNAB gets `YYYY-MM-DD` | `2026-10-05` → `"2026-10-05"` |
  | `Credits`, `Debits` | `amount` (pesos) | `amount` (milliunits) | `Credits − Debits`; YNAB gets × 1000; inflow +, outflow − | `Decimal("-249.00")` → `-249000` |
  | `Details` | `payee` | `payee_name` | `gotyme_payee()`; cut to 200 characters by the YNAB client | `"SHOPEE PH"` |
  | `Details` | `memo` | `memo` | `"GoTyme: " + Details` | `"GoTyme: Card payment at SHOPEE PH, Card •••• 1234"` |
  | `Date`, `Credits`, `Debits`, `Running Balance` | `source`, `key` | `import_id` | `key = "…\|n"`; YNAB gets `make_import_id(source, key)` | `"05 Oct 2026\|0.00\|249.00\|4751.00\|0"` → `"ETLPH:9c1e…"` |
  | — | `flagged` = `False` | `flag_color` = `null` | no correction rows | |
  | — | — | `cleared` / `approved` | fixed by the YNAB client: `"cleared"` / `False` (you review them in YNAB) | |
  | — | — | `payee_id`, `category_id` | not sent; YNAB matches the payee name and applies your payee rules | |

  Statement values that aren't sent as transactions:
  - Opening and Closing balance feed the starting- and ending-balance checks.
  - Period and the totals are used only by `_check_totals()`.
  - Running Balance is used only in `key` (so `import_id`) and the checks.
- [ ] A `GoTymeTest` class in `tests/test_sources.py`:
  - every parsed row has exactly `COLUMNS`
  - one made-up row → the exact `Transaction`, and its `to_ynab()` → the exact YNAB dict, both from the table above
  - signs: credit positive, debit negative
  - each payee rule
  - a stable `import_id`, and different ids for two otherwise identical rows (`n`)
  - `statement_balances` and `summary`
- [ ] `docs/architecture.md`: list GoTyme under sources.

## Tests

```bash
python -m unittest tests.test_parsers_gotyme tests.test_sources -v
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- All tests pass. The GoTyme tests use only made-up data.
- With your real statement, locally:
  ```bash
  python -m statement_etl.pipeline statements/<gotyme>.pdf --source gotyme --dry-run
  ```
  This shows every row, a summary with ✓, and starting and ending balances equal to the printed Opening and Closing balances.
- On the local web page:
  1. Source "GoTyme", upload the PDF, pick a **test** YNAB account, upload.
  2. The transactions appear unapproved, with sensible payees and `GoTyme:` memos.
  3. The ending balance matches.
  4. Uploading again creates 0 and skips them all.

## Docs to update

README, `docs/getting-started.md`, `.env.example`, `docs/adding-an-e-wallet.md` (worked example and mapping table), `docs/architecture.md`.

## Risks and rollback

| Risk | What to do |
|---|---|
| A different GoTyme layout (another month or account type) breaks column detection | `_find_columns` reads the header on each page instead of hard-coding x positions. Add the new layout as a made-up test case. |
| Two genuinely identical rows on the same day | `n` keeps their ids different; there's a test for it. |
| Real data leaks into a test or PR | Rewrite the test with invented values before merging. If it's already on `main`, see P0's risks table. |
| GoTyme changes the PDF | The checks raise `StatementError`, so nothing wrong is uploaded. Fix the parser and add a made-up test for the new shape. |

Rollback: revert PR 2 to remove GoTyme from the Source list. The parser module can stay unused.
