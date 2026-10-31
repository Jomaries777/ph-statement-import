# P1. Refactors (no behavior change)

Status: Done (Oct 31, 2026) · Estimate: about 2 days, 3 PRs · Needs: P0

## Goal

Remove duplication **before** the new features copy it a third time, so each job has one obvious place in the code. After P1:

- **One import function.** The CLI, the web upload and (in P5) the Gmail runner all call `pipeline.import_statement()`. Today `run_pipeline()` and `api_upload()` each run the same four steps by hand.
- **One registry per source, and no YNAB inside sources.** A source turns its rows into neutral `Transaction`s (`to_transactions`, registered once on `Source`). YNAB formatting lives in `ynab_api/`. Today `transform.py` imports from `parsers/gcash.py` and keeps a second registry, `TRANSFORMERS`, that a test keeps in sync with `SOURCES`. The split also leaves room for other budgeting apps later.
- **Module names that say what they do.** `transform.py` becomes `convert.py`: all it has left to do is pick the source and call its converter, and the old name was easy to confuse with the new `transaction.py`.
- **Smaller web modules.** `web/app.py` (601 lines) is split into Flask Blueprints by job.

**Nothing users can see changes:** the same URLs, page behavior and CLI output, and **the same `import_id` for every transaction**. If import_ids changed, YNAB would import duplicates. The one exception is a developer command: `python -m statement_etl.transform` becomes `python -m statement_etl.convert`, with the same options and the same output.

## Before you start

- P0 merged: `main` protected, `test` and `lint` checks required.
- [x] **Baseline saved (Oct 31, 2026).** The current output for a real GCash statement is in `statements/` (gitignored, never committed): `before-pipeline.txt` (dry run), `before-rows.json` (25 parsed rows) and `before-transform.json` (25 YNAB transactions with their `import_id`s). If you need to make it again on `main` before PR 1:
  ```bash
  source .venv/bin/activate
  read -rs "GCASH_PDF_PASSWORD?GCash PDF password: " && export GCASH_PDF_PASSWORD && echo
  python -m statement_etl.pipeline statements/<gcash>.pdf --dry-run > statements/before-pipeline.txt
  python -m statement_etl.parsers.gcash statements/<gcash>.pdf -o statements/before-rows.json
  python -m statement_etl.transform statements/before-rows.json --source gcash -o statements/before-transform.json
  ```
  - `<gcash>` is your statement's file name, e.g. `enc-09XXXXX_….pdf`.
  - The pipeline reads the password only from `GCASH_PDF_PASSWORD`; the parser asks for it again.
  - Run them in one terminal tab, so the venv and the password variable are both set.

## Steps

### PR 1: `refactor/import-statement`, "refactor: share one import function between the CLI and the web app"

- [x] **First, pin an import_id.** In `tests/test_transform.py`, add a test that `make_import_id("gcash", "1000000000002")` equals its exact current string. Run it once, then copy the value into the test. This must pass before and after every P1 PR (PR 2 moves it to `tests/test_ynab_format.py`).
- [x] In `statement_etl/pipeline.py`, add next to the other pipeline steps:
  ```python
  @dataclass
  class ImportResult:
      """What import_statement() did, for the caller to show or record."""
      new_account: dict | None   # {"from", "to"} if a new account's Starting Balance was set
      ynab_before: Decimal       # YNAB account balance just before the upload, in pesos
      created: list              # YNAB TransactionDetail objects that were created
      skipped: int               # transactions YNAB already had (same import_id)
      ending_check: dict | None  # from ending_balance_check(); None if it failed
      ending_error: ynab.ApiException | None  # why the ending check failed, if it did

  def import_statement(rows, transactions, account_id, plan_id, access_token=None,
                       source=DEFAULT_SOURCE) -> ImportResult:
  ```
  Body, with numbered step comments:
  1. `new_account_starting_balance(...)`
  2. `get_account_balance(...)` → `ynab_before`
  3. `upload_transactions(...)`
  4. `ending_balance_check(..., ynab_before)`

  Errors:
  - A `ynab.ApiException` from steps 1–3 is **not caught**, so each caller reports it its own way.
  - A failure in step 4 is caught and stored in `ending_error`, because the upload is already done. It's the exception itself, so each caller words it as before (the CLI: "YNAB returned 503 Unavailable"; the page: "YNAB returned 503: …").

  The docstring explains both, with a short example.
- [x] `web/app.py` → `api_upload()`:
  - Replace the three separate `_ynab_call(...)` blocks with one call: `_ynab_call(lambda token: import_statement(rows, transactions, account_id, plan_id, token, source=state["source"]))`.
  - Build the **same JSON** as today from the `ImportResult`.
  - Add a comment saying why a retry after a token refresh is safe: `import_id` dedupes the upload, and the starting-balance step does nothing once the balance matches.
  - The page's wording for a YNAB error moves into `_api_error_message()`, used by `_ynab_call()` and for the ending-check error.
- [x] `pipeline.run_pipeline()`:
  - Keep STEP 1 (parse) and STEP 2 (the starting-balance check and its printout).
  - Replace the new-account, upload and ending-check code with one `import_statement()` call inside the existing `try/except ynab.ApiException`.
  - Print STEP 3 and 4 from the result. **The printed text must stay identical.**
  - Watch one detail: today the CLI uses `new_account["to"]` as `ynab_before` for a new account. `import_statement()` reads the balance after setting it, which gives the same number. The output must not change.
  - One error message changes on purpose: if YNAB fails while setting a new account's Starting Balance, the CLI now prints "ERROR: Import failed: YNAB returned …" instead of "ERROR: Could not set the Starting Balance: …". `import_statement()` raises every YNAB error from steps 1–3 the same way, and nothing is uploaded in either case.
- [x] Update the module docstring of `pipeline.py`. It currently says the web app calls the steps "directly"; change it to say both use `import_statement()`.
- [x] Update `docs/architecture.md`: the pipeline layer now has one entry point.

### PR 2: `refactor/neutral-transaction`, "refactor: sources produce neutral transactions; YNAB formatting moves to ynab_api"

Splits the pipeline into three parts, so a new e-wallet and a new budgeting app (tracker) can each be added without touching the other:

```
Source (GCash, GoTyme, …)          →  Transaction (neutral)          →  YNAB (today's only destination)
parsers/<source>.py                    statement_etl/transaction.py      statement_etl/ynab_api/format.py
  parse_transactions → rows              the data shape only               to_ynab(transaction)
  to_transactions(rows)
                    ↑
statement_etl/convert.py: rows_to_transactions(rows, source) picks the source and calls its to_transactions
```

What each module is, so the names don't get mixed up:

| Module | Is | Holds |
|---|---|---|
| `transaction.py` | a **data shape** | the `Transaction` dataclass; no logic |
| `convert.py` (was `transform.py`) | a **step** | `rows_to_transactions(rows, source)`, plus a CLI that prints the YNAB JSON |
| `ynab_api/format.py` | YNAB's **format** | milliunits, `import_id`, `to_ynab()` |

- [x] New `statement_etl/transaction.py`, with a module docstring explaining the three parts:
  ```python
  @dataclass(frozen=True)
  class Transaction:
      """One statement transaction, in no particular tracker's format."""

      source: str  # source key, e.g. "gcash"; part of every tracker's duplicate check
      key: str  # stable id within the source, e.g. GCash Reference No.
      date: date  # a datetime is fine when the statement has times
      payee: str
      amount: Decimal  # pesos; inflow positive, outflow negative
      memo: str | None = None
      flagged: bool = False  # a row the parser added (balance correction): show it for review
  ```
  No milliunits and no `ETLPH:` prefix here. Those are YNAB's format.
- [x] New `statement_etl/ynab_api/format.py`: the YNAB side of the boundary. `ynab_api/` (with `client.py`, `accounts.py` and `balance.py`) is already the YNAB package; its `__init__` docstring now calls it "the YNAB destination".
  - **Move** from `transform.py` without changing them: `to_milliunits`, `IMPORT_ID_PREFIX` (with its "never rename" comment), `make_import_id`, `ynab_transaction`.
  - Add `to_ynab(transaction) -> dict`:
    ```python
    ynab_transaction(
        date=t.date,
        payee_name=t.payee,
        amount=t.amount,
        import_id=make_import_id(t.source, t.key),
        memo=t.memo,
        flag_color="red" if t.flagged else None,
    )
    ```
  - **Why this keeps every existing import_id:** GCash's `key` is its Reference No., so `make_import_id("gcash", key)` is the same call as today. The pinned test from PR 1 proves it.
- [x] `to_decimal`: `transform.to_decimal` and `parsers/common.to_decimal` do the same job. Keep `parsers/common.to_decimal` and use it everywhere; delete the copy in `transform.py`.
- [x] Move `gcash_to_ynab` to `statement_etl/parsers/gcash.py` as `to_transactions(rows) -> list[Transaction]`, together with the `GCASH` input-row comment block. Keep the logic: same date parsing, amount, payee (`gcash_payee`), memo (`"GCash Ref: …"`), `key = Reference No.` and `flagged = is_correction_row(row)`.
  - It takes **all rows**, not one, so a source can number otherwise-identical rows. GoTyme needs this in P2.
  - Parsers no longer import anything YNAB-specific, so there's no import cycle.
- [x] `statement_etl/sources.py`:
  - Add a **required** field `to_transactions: Callable`, documented as "rows -> list[Transaction]".
  - The `gcash` entry gets `to_transactions=gcash.to_transactions`.
  - Remove the docstring line saying transformers are registered separately.
- [x] **Rename `transform.py` to `convert.py`** with `git mv statement_etl/transform.py statement_etl/convert.py`, so `git log --follow` keeps its history. What's left in it:
  - `rows_to_transactions(rows, source) -> list[Transaction]`: `return get_source(source).to_transactions(rows)`. It replaces `transform()`. The "Unknown source" error now comes from `get_source()`.
  - Delete `TRANSFORMERS`.
  - `main()`: `python -m statement_etl.convert rows.json --source gcash [-o out.json]` keeps **printing the same YNAB JSON** as the old `transform` CLI (`[to_ynab(t) for t in rows_to_transactions(...)]`), so P1's output diff stays empty. `--source` choices come from `SOURCES`.
  - Module docstring, first line: "ph-statement-import - turn a statement's parsed rows into neutral Transactions, using the source's own converter." Then usage, one line on the three parts, and a pointer to `docs/adding-an-e-wallet.md`.
  - Afterwards `grep -rn "statement_etl.transform\|import transform\|transform(" statement_etl tests` finds nothing.
- [x] `pipeline.parse_statement()`: returns `(rows, [to_ynab(t) for t in rows_to_transactions(rows, source)])`. Everything after it (web state, `import_statement()`, the YNAB client) still gets the same YNAB dicts, so nothing downstream changes.
- [x] **Don't build more than this.** No `Destination` registry, tracker picker or per-tracker settings until a second tracker is actually being added. Until then they'd be code that readers have to skip. `docs/architecture.md` explains where a second tracker would go.
- [x] Tests, one file per module (the README's rule):
  - `tests/test_ynab_format.py` (new): `to_milliunits`, `make_import_id` (including the pinned id from PR 1), `ynab_transaction`, and `to_ynab()` of a `Transaction`.
  - `tests/test_parsers_gcash.py`: `gcash.to_transactions([PAYMENT])` gives the expected `Transaction`, **and** `to_ynab()` of it equals today's expected dict, field for field, including `import_id`.
  - `tests/test_convert.py`: `git mv tests/test_transform.py tests/test_convert.py`, then keep only what's about `convert.py`: `rows_to_transactions` picks the right source, an unknown source raises, and the CLI prints the same YNAB JSON as before.
  - `tests/test_sources.py`: replace the `SOURCES` vs `TRANSFORMERS` test with "every `Source` has a callable `to_transactions`".
  - Fix any other imports that `python -m unittest discover tests` reports.
- [x] Docs:
  - `docs/adding-an-e-wallet.md`: a new source is now **one parser module** (`parse_transactions`, `statement_balances`, `summary`, `to_transactions`) **plus one `Source` entry**. It returns `Transaction`s and never needs to know about YNAB. Remove the `TRANSFORMERS` step and every mention of `transform.py`.
  - `docs/architecture.md`: the three parts and the module table above.
  - README "Project layout": replace the `transform.py` line with `transaction.py` (the neutral Transaction) and `convert.py` (rows → Transactions, using the source's converter), and add `format.py` under `ynab_api/`.
  - CONTRIBUTING, Money: drop "(currently `transform.py` until P1)".

### PR 3: `refactor/web-blueprints`, "refactor: split the web app into blueprints"

- [x] Create these modules. **Move** code without changing it, and start each with a module docstring:

  | Module | Holds |
  |---|---|
  | `web/state.py` | `STORE`, `session_state()` (was `_state`), `error_response()` (was `_error`) |
  | `web/ynab_oauth.py` | `exchange_token(**params)` (was `_exchange_token`), `refresh(refresh_token)` and `oauth_configured()` (was `_oauth_configured`): token exchange and refresh with **no Flask session**, so the P5 runner can use them |
  | `web/ynab_auth.py` (Blueprint `ynab_auth`) | `/oauth/login`, `/oauth/callback`, `/oauth/logout`, `/api/token`, `get_token()`, `_refresh()`, `_store_tokens()`, `_clear_tokens()`, `ynab_call()` (was `_ynab_call`), `api_error_message()` (was `_api_error_message`), `_error_detail()` |
  | `web/upload.py` (Blueprint `upload`) | `/`, `/api/status`, `/api/parse`, `/api/plans`, `/api/plans/<plan_id>/accounts`, `/api/starting-balance`, `/api/upload`, `_check_json()`, `_money()` |
  | `web/app.py` | Creates `app`, loads config, registers both blueprints, `main()`. Keeps a module-level `app`, so `python -m statement_etl.web` and the future gunicorn target `statement_etl.web.app:app` still work. |

- [x] Names another module imports lose their leading underscore (listed above), since `_name` tells a reader it's private to one file.
- [x] Constants (`PORT`, `REDIRECT_URI`, the YNAB URLs) go to the module that uses them. `PORT` and `REDIRECT_URI` are used by three modules, so they're in `web/config.py`, which also loads `.env`. If two modules need one, put it in `web/state.py`, or a small `web/config.py` if that reads better.
- [x] The templates don't use `url_for`, so endpoint renames are safe. Check with `grep -rn url_for statement_etl/`.
- [x] `tests/test_web_app.py`: change each `patch.object(web, "<name>")` to the module that **calls** the name:
  - `get_plans`, `get_accounts`, `parse_statement`, `starting_balance_check` → `web.upload`
  - `get_user` → `web.upload` for `/api/status` tests, `web.ynab_auth` for `/api/token` tests
  - `_exchange_token` → `web.ynab_oauth.exchange_token`
  - `web.app.run` and `web.main` stay in `web.app`
- [x] Add a test that lists `app.url_map` and checks every route URL and method from before the split is still there.
- [x] Update `docs/architecture.md` with the web module table above.

## Tests

Run after every PR:

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

New tests:
- the pinned GCash `import_id`
- `tests/test_pipeline.py`, for `import_statement()`:
  - a new account gets its starting balance set, and `ynab_before` is read after that
  - created and skipped counts come from the YNAB response
  - an ending-balance mismatch is returned in `ending_check`, not raised
  - a failed ending check fills `ending_error` and doesn't raise
  - an `ApiException` from the upload propagates
  - calling it twice with the same rows creates nothing the second time (fake YNAB from `tests/fakes.py`)
- every `Source` has a callable `to_transactions`
- a GCash row → `Transaction` → `to_ynab()` gives exactly today's YNAB dict
- `rows_to_transactions` picks the source; the `convert` CLI prints the same JSON as the old `transform` CLI
- every route is unchanged after the blueprint split

## Done when

- All tests and lint pass.
- **CLI output is unchanged.** After each PR, in one terminal tab with the venv and `GCASH_PDF_PASSWORD` set (see "Before you start"), make the "after" files and compare:
  ```bash
  python -m statement_etl.pipeline statements/<gcash>.pdf --dry-run > statements/after-pipeline.txt
  python -m statement_etl.parsers.gcash statements/<gcash>.pdf -o statements/after-rows.json
  python -m statement_etl.convert statements/after-rows.json --source gcash -o statements/after-transform.json
  diff statements/before-pipeline.txt statements/after-pipeline.txt
  diff statements/before-rows.json statements/after-rows.json
  diff statements/before-transform.json statements/after-transform.json
  ```
  After PR 1, use `statement_etl.transform` in the third command; `convert` exists from PR 2. All three diffs must be empty, including every `import_id`.
- One real upload through the local web page to a **test YNAB account** works: created, skipped, and both balance messages.
- `grep -rn TRANSFORMERS statement_etl tests docs` finds nothing, and `grep -rn "transform.py\|statement_etl.transform" statement_etl tests README.md CONTRIBUTING.md docs/architecture.md docs/adding-an-e-wallet.md` finds nothing.

## Docs to update

`docs/architecture.md` (the Source → Transaction → YNAB parts, `convert.py`, the pipeline entry point, the web modules), `docs/adding-an-e-wallet.md`, README "Project layout", CONTRIBUTING (Money), the module docstrings of `pipeline.py` and `convert.py`, and `docs/design/diagrams.html` if a name differs from what it shows.

## Risks and rollback

| Risk | Sign | What to do |
|---|---|---|
| `import_id`s change | The pinned test or the `before-transform.json` diff fails | Stop. Check `Transaction.key` and `ynab_api.format.to_ynab`, and undo whatever changed the id. Never merge with a diff in import_ids. |
| CLI output text changes | The `before-pipeline.txt` diff isn't empty | Fix the print code in `run_pipeline()` until the diff is empty. |
| Something still calls the old name | `ModuleNotFoundError: statement_etl.transform`, or a doc still says `transform.py` | Run the two `grep`s from "Done when" and fix every hit. |
| Token refresh retries `import_statement()` twice | A double "Starting Balance" in a test account | Check that `new_account_starting_balance()` does nothing when the balance already matches; add a test. |
| A test patches the wrong module after the split | The test calls the real YNAB, and the safety net in `tests/__init__.py` fails it | Patch the module that calls the name, as listed in PR 3. |

Each PR is a single squash commit, so reverting one is `git revert <sha>` in a new PR.
