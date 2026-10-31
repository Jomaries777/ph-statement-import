# P1. Refactors (no behavior change)

Status: Not started · Estimate: about 2 days, 3 PRs · Needs: P0

## Goal

Remove duplication **before** the new features copy it a third time, so each job has one obvious place in the code. After P1:

- **One import function.** The CLI, the web upload and (in P5) the Gmail runner all call `pipeline.import_statement()`. Today `run_pipeline()` and `api_upload()` each run the same four steps by hand.
- **One registry per source, and no YNAB inside sources.** A source turns its rows into neutral `Transaction`s (`to_transactions`, registered once on `Source`). YNAB formatting lives in `ynab_api/`. Today `transform.py` imports from `parsers/gcash.py` and keeps a second registry, `TRANSFORMERS`, that a test keeps in sync with `SOURCES`. The split also leaves room for other budgeting apps later.
- **Smaller web modules.** `web/app.py` (601 lines) is split into Flask Blueprints by job.

**Nothing users can see changes:** the same URLs, page behavior and CLI output, and **the same `import_id` for every transaction**. If import_ids changed, YNAB would import duplicates.

## Before you start

- P0 merged: `main` protected, `test` and `lint` checks required.
- Save the current CLI output to compare against at the end of each PR (Verification). You need a real GCash statement in `statements/`, which is never committed.
  ```bash
  mkdir -p /tmp/p1-baseline
  python -m statement_etl.pipeline statements/<gcash>.pdf --dry-run > /tmp/p1-baseline/dry-run.txt
  python -m statement_etl.parsers.gcash statements/<gcash>.pdf -o /tmp/p1-baseline/rows.json
  python -m statement_etl.transform /tmp/p1-baseline/rows.json --source gcash > /tmp/p1-baseline/ynab.json
  ```
  These commands prompt for the PDF password or read it from `.env`. Check each tool's `--help` for its exact options.

## Steps

### PR 1: `refactor/import-statement`, "refactor: share one import function between the CLI and the web app"

- [ ] **First, pin an import_id.** In `tests/test_transform.py`, add a test that `make_import_id("gcash", "1000000000002")` equals its exact current string. Run it once, then copy the value into the test. This must pass before and after every P1 PR.
- [ ] In `statement_etl/pipeline.py`, add next to the other pipeline steps:
  ```python
  @dataclass
  class ImportResult:
      """What import_statement() did, for the caller to show or record."""
      new_account: dict | None   # from new_account_starting_balance(); None if not a new account
      ynab_before: int           # YNAB account balance just before the upload (milliunits)
      created: list              # YNAB TransactionDetail objects that were created
      skipped: int               # rows YNAB already had (duplicate import_ids)
      ending_check: dict | None  # from ending_balance_check(); None if the check failed
      ending_error: str | None   # why the ending check failed, if it did

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
  - A failure in step 4 is caught and stored in `ending_error`, because the upload is already done.

  The docstring explains both, with a short example.
- [ ] `web/app.py` → `api_upload()`:
  - Replace the three separate `_ynab_call(...)` blocks with one call: `_ynab_call(lambda token: import_statement(rows, transactions, account_id, plan_id, token, source=state["source"]))`.
  - Build the **same JSON** as today from the `ImportResult`.
  - Add a comment saying why a retry after a token refresh is safe: `import_id` dedupes the upload, and the starting-balance step does nothing once the balance matches.
- [ ] `pipeline.run_pipeline()`:
  - Keep STEP 1 (parse) and STEP 2 (the starting-balance check and its printout).
  - Replace the new-account, upload and ending-check code with one `import_statement()` call inside the existing `try/except ynab.ApiException`.
  - Print STEP 3 and 4 from the result. **The printed text must stay identical.**
  - Watch one detail: today the CLI uses `new_account["to"]` as `ynab_before` for a new account. `import_statement()` reads the balance after setting it, which gives the same number. The output must not change.
- [ ] Update the module docstring of `pipeline.py`. It currently says the web app calls the steps "directly"; change it to say both use `import_statement()`.
- [ ] Update `docs/architecture.md`: the pipeline layer now has one entry point.

### PR 2: `refactor/neutral-transaction`, "refactor: sources produce neutral transactions; YNAB formatting moves to ynab_api"

Splits the pipeline into three parts, so a new e-wallet and a new budgeting app (tracker) can each be added without touching the other:

```
Source (GCash, GoTyme, …)  →  Transaction (neutral)  →  YNAB (today's only destination)
parsers/<source>.py           statement_etl/transaction.py   statement_etl/ynab_api/
```

- [ ] New `statement_etl/transaction.py`, with a module docstring explaining the three parts:
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
- [ ] New `statement_etl/ynab_api/format.py`: the YNAB side of the boundary. `ynab_api/` (with `client.py`, `accounts.py` and `balance.py`) is already the YNAB package; its `__init__` docstring now calls it "the YNAB destination".
  - **Move** from `transform.py` without changing them: `to_milliunits`, `IMPORT_ID_PREFIX`, `make_import_id`, `ynab_transaction`.
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
- [ ] `to_decimal`: `transform.to_decimal` and `parsers/common.to_decimal` do the same job. Keep `parsers/common.to_decimal` and use it everywhere; delete the copy in `transform.py`.
- [ ] Move `gcash_to_ynab` to `statement_etl/parsers/gcash.py` as `to_transactions(rows) -> list[Transaction]`, together with the `GCASH` input-row comment block. Keep the logic: same date parsing, amount, payee (`gcash_payee`), memo (`"GCash Ref: …"`), `key = Reference No.` and `flagged = is_correction_row(row)`.
  - It takes **all rows**, not one, so a source can number otherwise-identical rows. GoTyme needs this in P2.
  - Parsers no longer import anything YNAB-specific, so there's no import cycle.
- [ ] `statement_etl/sources.py`:
  - Add a **required** field `to_transactions: Callable`, documented as "rows -> list[Transaction]".
  - The `gcash` entry gets `to_transactions=gcash.to_transactions`.
  - Remove the docstring line saying transformers are registered separately.
- [ ] `statement_etl/transform.py`:
  - `transform(rows, source) -> list[Transaction]` becomes `get_source(source).to_transactions(rows)`. The "Unknown source" error now comes from `get_source()`.
  - Delete `TRANSFORMERS`.
  - The `python -m statement_etl.transform` CLI keeps **printing the same YNAB JSON** (`[to_ynab(t) for t in …]`), so P1's output diff stays empty. `--source` choices come from `SOURCES`.
  - Update the module docstring: point "How to add a new e-wallet" to `docs/adding-an-e-wallet.md`, and add one line on the three parts.
- [ ] `pipeline.parse_statement()`: returns `(rows, [to_ynab(t) for t in transform(rows, source)])`. Everything after it (web state, `import_statement()`, the YNAB client) still gets the same YNAB dicts, so nothing downstream changes.
- [ ] **Don't build more than this.** No `Destination` registry, tracker picker or per-tracker settings until a second tracker is actually being added. Until then they'd be code that readers have to skip. `docs/architecture.md` explains where a second tracker would go.
- [ ] Tests:
  - `tests/test_transform.py`:
    - YNAB helper tests import from `ynab_api.format`
    - GCash tests check `gcash.to_transactions([PAYMENT])` gives the expected `Transaction`
    - **and** that `to_ynab()` of it equals today's expected dict, field for field, including `import_id`
  - `tests/test_sources.py`: replace the `SOURCES` vs `TRANSFORMERS` test with "every `Source` has a callable `to_transactions`".
  - Fix any other imports that `python -m unittest discover tests` reports.
- [ ] `docs/adding-an-e-wallet.md`:
  - A new source is now **one parser module** (`parse_transactions`, `statement_balances`, `summary`, `to_transactions`) **plus one `Source` entry**.
  - It returns `Transaction`s and never needs to know about YNAB.
  - Remove the `TRANSFORMERS` step.

### PR 3: `refactor/web-blueprints`, "refactor: split the web app into blueprints"

- [ ] Create these modules. **Move** code without changing it, and start each with a module docstring:

  | Module | Holds |
  |---|---|
  | `web/state.py` | `STORE`, `_state()`, `_error()` |
  | `web/ynab_oauth.py` | `exchange_token(**params)` (was `_exchange_token`) and `refresh(refresh_token)`: token exchange and refresh with **no Flask session**, so the P5 runner can use them |
  | `web/ynab_auth.py` (Blueprint `ynab_auth`) | `/oauth/login`, `/oauth/callback`, `/oauth/logout`, `/api/token`, `get_token()`, `_refresh()`, `_store_tokens()`, `_clear_tokens()`, `_oauth_configured()`, `_ynab_call()`, `_error_detail()` |
  | `web/upload.py` (Blueprint `upload`) | `/`, `/api/status`, `/api/parse`, `/api/plans`, `/api/plans/<plan_id>/accounts`, `/api/starting-balance`, `/api/upload`, `_check_json()`, `_money()` |
  | `web/app.py` | Creates `app`, loads config, registers both blueprints, `main()`. Keeps a module-level `app`, so `python -m statement_etl.web` and the future gunicorn target `statement_etl.web.app:app` still work. |

- [ ] Constants (`PORT`, `REDIRECT_URI`, the YNAB URLs) go to the module that uses them. If two modules need one, put it in `web/state.py`, or a small `web/config.py` if that reads better.
- [ ] The templates don't use `url_for`, so endpoint renames are safe. Check with `grep -rn url_for statement_etl/`.
- [ ] `tests/test_web_app.py`: change each `patch.object(web, "<name>")` to the module that **calls** the name:
  - `get_plans`, `get_accounts`, `parse_statement`, `starting_balance_check` → `web.upload`
  - `get_user` → `web.upload` for `/api/status` tests, `web.ynab_auth` for `/api/token` tests
  - `_exchange_token` → `web.ynab_oauth.exchange_token`
  - `web.app.run` and `web.main` stay in `web.app`
- [ ] Add a test that lists `app.url_map` and checks every route URL and method from before the split is still there.
- [ ] Update `docs/architecture.md` with the web module table above.

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
- every route is unchanged after the blueprint split

## Done when

- All tests and lint pass.
- **CLI output is unchanged.** After each PR, run the commands from "Before you start" again into `/tmp/p1-after/` and compare:
  ```bash
  diff /tmp/p1-baseline/dry-run.txt /tmp/p1-after/dry-run.txt
  diff /tmp/p1-baseline/ynab.json   /tmp/p1-after/ynab.json
  ```
  Both diffs must be empty, including every `import_id`.
- One real upload through the local web page to a **test YNAB account** works: created, skipped, and both balance messages.
- `grep -rn TRANSFORMERS statement_etl tests docs` finds nothing.

## Docs to update

`docs/architecture.md` (the Source → Transaction → YNAB parts, the pipeline entry point, the web modules), `docs/adding-an-e-wallet.md`, the module docstrings of `pipeline.py` and `transform.py`, and `docs/design/diagrams.html` D2 if P0 didn't already show the final module names.

## Risks and rollback

| Risk | Sign | What to do |
|---|---|---|
| `import_id`s change | The pinned test or the `ynab.json` diff fails | Stop. Check `Transaction.key` and `ynab_api.format.to_ynab`, and undo whatever changed the id. Never merge with a diff in import_ids. |
| CLI output text changes | The `dry-run.txt` diff isn't empty | Fix the print code in `run_pipeline()` until the diff is empty. |
| Token refresh retries `import_statement()` twice | A double "Starting Balance" in a test account | Check that `new_account_starting_balance()` does nothing when the balance already matches; add a test. |
| A test patches the wrong module after the split | The test calls the real YNAB, and the safety net in `tests/__init__.py` fails it | Patch the module that calls the name, as listed in PR 3. |

Each PR is a single squash commit, so reverting one is `git revert <sha>` in a new PR.
