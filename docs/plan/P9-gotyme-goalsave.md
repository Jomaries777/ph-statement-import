# P9. GoTyme GoalSave

Status: Not started · Estimate: about 4–5 days, 5 PRs · Needs: P6 (GoTyme auto-import) and P8 (several accounts per source, transfers)

## Goal

The GoTyme statement email carries two PDFs, and only the Everyday one is imported today. The second, the savings statement, holds **one to five GoalSave accounts**. After this phase it's handled like GoTyme Everyday and GCash:

- A manual upload imports it, **one GoalSave account at a time** (you pick which after parsing).
- Gmail auto-import imports it, **each GoalSave account to its own YNAB account**.
- "Transfer to/from Deposit account" rows become YNAB transfers with GoTyme Everyday, **without a transfer rule**.

## What the GoalSave PDF looks like

`GoTyme_BankStatement_SavingsAccounts_YYYYMMDD_HHMMSS.pdf`, in the same email as the Everyday PDF:

- **One section per account.** Each has its own summary box (Opening balance, Total Credit, Total Debit, Closing balance), a `GoalSave No.: *******22222` line, an Interest rate line, `Period: 01 Oct 2026 - 31 Oct 2026` and the same table as Everyday: **Date | Details | Credits | Debits | Running Balance**. In the sample, each account fills one page.
- **The rows:** "Earned interest", "Withholding tax from earned interest", "Transfer to Deposit account", "Transfer from Deposit account".
- **Password:** assumed to be the date of birth as `MMDDYYYY`, like the Everyday PDF in the same email. The sample was unlocked, so the Test button and the first real email confirm it.
- `parsers/gotyme.py`'s page helpers already read each section correctly (checked on a real sample).

## How the accounts map

- **The key** is the last 5 digits of each section's GoalSave No. (`*******22222` → `22222`). The parser adds them to every row, as the `GoalSave No.` column.
- **Auto-import:** one GoalSave row per account on `/auto-import`, up to 5 (`MAX_WALLETS_PER_SOURCE`). Each row has its 5 digits and its YNAB account; only the first asks for the date of birth, because one PDF opens every account. The PDF is opened once, and each account's rows go to the YNAB account mapped to its digits. An account in the PDF with no row is a "⚠ No account" line; the others still import.
- **Manual upload:** after parsing, a "GoalSave No." picker lists the accounts found (shown only with two or more). Pick one, pick its YNAB account as usual, upload; then pick the next. The balance checks are per account.
- **Command line:** `--statement-account 22222`.

## Order

Each PR is a branch stacked on the one before it, one commit each; the first branches from `main`.

```
docs/p9-plan ─► refactor/gotyme-page-helpers ─► feat/gotyme-goalsave-parser
  ─► feat/gotyme-goalsave-auto-import ─► feat/goalsave-transfers
```

## Steps

### PR 0: `docs/p9-plan`, "docs: plan GoTyme GoalSave import"

- [ ] This file, a P9 row in `README.md`, and P6's "Later" pointing here.
- [ ] `docs/design/wireframes.html`: W1 (GoalSave No. picker), W2 (GoalSave rows instead of "Coming soon"), W3 (one row per account in Recent imports).

### PR 1: `refactor/gotyme-page-helpers`, "refactor: share GoTyme's page-reading helpers"

No behavior change.

- [ ] `parsers/gotyme.py`: `_page_lines`, `_read_summary`, `_find_columns`, `_anchor_rows`, `_to_output_row` and `_check_totals` become public (`page_lines`, `read_summary`, …), so the GoalSave parser reads its pages with the same code.
- [ ] `autoimport/runner.py`: step 4 of `_import_email()` (transfers, import, counts, ending check) moves into `_import_rows()`, so it can run once per GoalSave account. `_categorize()` is called after it.

### PR 2: `feat/gotyme-goalsave-parser`, "feat: import GoTyme GoalSave statements by hand, one account at a time"

- [ ] `parsers/gotyme_goalsave.py`: `COLUMNS = ("GoalSave No.", "Date", "Details", "Credits", "Debits", "Running Balance")`. A page with a GoalSave No. starts a section; a page without one continues the last (an account that runs over a page). The four statement checks run per section.
- [ ] `to_transactions()`: key `{GoalSave No.}|{Date}|{Credits}|{Debits}|{Running Balance}|{n}`, payee = Details, memo `GoTyme Ref: <transaction_ref>` like Everyday.
- [ ] `sources.py`: `Source.account_column`, for statements that hold several accounts; the `gotyme_goalsave` source.
- [ ] `pipeline.split_accounts(rows, source)` → `{account: (rows, transactions)}`; `--statement-account` on the command line.
- [ ] `/api/parse` takes `statement_account` and keeps only that account's rows; the page shows the GoalSave No. picker.
- [ ] Tests: two accounts, an account over two pages, a section whose totals don't add up, keys and memos, `split_accounts`, the parse route with and without `statement_account`.

### PR 3: `feat/gotyme-goalsave-auto-import`, "feat: import GoalSave statement emails, each account to its own YNAB account"

- [ ] `gotyme_goalsave` email settings: the Everyday `email_query`, `attachment_pattern=r"^GoTyme_BankStatement_SavingsAccounts_\d{8}_\d{6}\.pdf$"`, `password_inputs=("birth_date",)`, `account_input="goalsave_last5"`.
- [ ] `Source.shared_password`: the password inputs are asked once, on the first row, and saved with every row of that source.
- [ ] Runner: one email → parse once → each account to its wallet's YNAB account. One run record per email (unchanged id) with an `accounts` list; the run log shows a row per account. Gemini categorizes once per email.
- [ ] Test button: names the GoalSave No.s it found.
- [ ] Docs: `docs/adding-an-e-wallet.md` (`account_column`, `shared_password`), README, the privacy page.
- [ ] Tests: both PDFs in one email, two accounts into two YNAB accounts, an unmapped account, YNAB busy half-way, the shared date of birth.

### PR 4: `feat/goalsave-transfers`, "feat: link GoalSave and GoTyme Everyday transfers without a rule"

Every "Transfer to/from Deposit account" row is money moved to or from GoTyme Everyday.

- [ ] `Source.own_transfers`: on `gotyme_goalsave`, rows with "Deposit account" are transfers with the `gotyme` account, whose rows say "GoalSave".
- [ ] GoalSave side: sent as a transfer to the Everyday YNAB account. Everyday side: its "Transfer to/from GoalSave" row finds the transfer a GoalSave import already made (same amount, within 3 days) in any GoalSave account, and isn't uploaded again. That's how several GoalSave accounts are told apart.
- [ ] Auto-import imports the GoalSave PDF before the Everyday one from the same email. A manual Everyday upload whose GoalSave rows find nothing says "Import the GoalSave statement first, so its transfers link up."
- [ ] Spike, on a test plan: when Everyday was imported first, can the GoalSave import turn the Everyday row into the transfer (its payee changed to the GoalSave account's transfer payee)? If YNAB adds the GoalSave side then, do that instead of uploading the GoalSave row; if not, keep "import GoalSave first".
- [ ] Manual GoalSave upload: card ③ asks which YNAB account is GoTyme Everyday (prefilled from auto-import when hosted).

## Tests

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- The sample PDF parses into its two accounts, each adding up.
- A manual upload imports each GoalSave account into its own YNAB account, and both balances match.
- A real GoTyme email imports the Everyday account and every mapped GoalSave account in one poll; running again creates nothing.
- A GoalSave → Everyday move shows as one YNAB transfer after both PDFs are imported.
- Billing → Reports still shows **$0.00**.

## Risks and rollback

| Risk | What to do |
|---|---|
| The GoalSave PDF has a different password | The Test button says "Wrong password". Change `build_password` for `gotyme_goalsave` and add a test. |
| GoTyme prints more or fewer than 5 digits | The parser takes all the digits after the stars; change `goalsave_last5`'s pattern and label. |
| An account's section runs over a page without repeating the summary | Handled: a page without a GoalSave No. continues the last section. A test covers it. |
| Two GoalSave accounts move the same amount to Everyday on the same day | The Everyday rows each take one other side; either way the balances match, only the GoalSave account named may swap. |

Rollback: revert the PRs in reverse order. Without the source, GoalSave mappings are ignored (as for any source without `email_query`).
