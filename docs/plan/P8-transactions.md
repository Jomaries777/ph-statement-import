# P8. Less hand work after an import

Status: Not started · Estimate: about 6–8 days, 11 PRs · Needs: P5, P5b and P6 (auto-import with GCash and GoTyme), and the Gemini categorizer (#42)

## Goal

What lands in YNAB needs less fixing by hand, and the auto-import page shows what it's doing:

- New transactions get a category from Gemini, when Gemini is at least 80% sure (hosted, opt-in).
- Every GoTyme row carries a reference you can search for in YNAB, like GCash rows already do.
- Transfers between your own wallets arrive as YNAB transfers, not two unrelated payments.
- Two or more GCash accounts can be mapped, told apart by their mobile number.
- Recent imports keeps each run's wallet and account as they were, can be cleared, and shows an import's progress live. The manual upload shows its progress too.
- "Edit mappings" no longer asks you to connect YNAB again when auto-import is already connected.

## Order

Each PR is a branch stacked on the one before it, one commit each; the first branches from `main`.

```
docs/p8-plan ─► fix/run-log-account-name ─► fix/edit-mappings-ynab-connected ─► feat/clear-run-log
  ─► feat/run-progress ─► feat/upload-progress ─► feat/memo-references ─► feat/categorize-after-import
  ─► feat/multiple-gcash-accounts ─► feat/transfer-rules ─► feat/import-transfers
```

The early PRs are small and independent in logic; they're stacked only because they share `web/auto_import.py` and its template. `feat/multiple-gcash-accounts` must come before the transfer PRs: a transfer's destination is a wallet, and after that PR a wallet is "GCash · 0917123", not just "gcash".

## Steps

### PR 1: `fix/run-log-account-name`, "fix: keep each run's wallet and account as they were at import"

Cause: `format_run()` in `web/auto_import.py` looks the account name up in today's `mappings`; the run record (`store.write_run()`) only stores `account_id`. Changing a mapping rewrites the history.

- [ ] `autoimport/runner.py` `process_message()`: also pass `account_name` (and `source_name`) to `store.write_run()`.
- [ ] Run log table: split "Wallet → account" into two columns, **Wallet | YNAB account**. `format_run()` returns `wallet` and `account` instead of `target`.
- [ ] `format_run()`: use the run's own `account_name`; fall back to the current mapping, then `account_id`, only for runs written before this PR.
- [ ] `autoimport/store.py` docstring: `account_name` in the `runs/…` field list.
- [ ] Tests: a run stored with account "Sample6" still shows "Sample6" after the mapping changes; an old run without `account_name` still falls back.

### PR 1b: `fix/edit-mappings-ynab-connected`, "fix: use the saved YNAB connection when editing auto-import mappings"

Cause: `static/auto_import.js` decides "connected" only from `GET /api/status`, which looks at the **browser session's** YNAB token. Hosted sessions are forgotten after 30 minutes idle (`web/state.py`), and saving the settings moves the session's refresh token into Firestore. So a later "Edit mappings" shows "Connect with YNAB login", although auto-import still has a working YNAB connection.

- [ ] `web/auto_import.py`: `POST /api/auto-import/ynab-session` (signed in, invited, rate limited). If the session already has a working token: connected. Otherwise, with a stored refresh token: hold the runner's lock (`runner.ynab_refresh_lock()`; one gunicorn worker, so an in-memory lock covers the background runs too), refresh it, **save the rotated refresh token to Firestore first**, then keep only the access token in the session. A refresh YNAB rejects → not connected, and the page shows Connect / Reconnect as today.
- [ ] `static/auto_import.js`: "Edit mappings" shows "Checking YNAB…", calls `/api/status`, then the new route if needed, loads the plans (saved plan and accounts preselected), and only then shows the setup view. The YNAB token is refreshed only when the setup view is opened, not on every visit.
- [ ] Tests: stored token → connected, Firestore gets the new refresh token, the session holds none; revoked token → not connected; no stored token → not connected; 401/403 guards.

### PR 2: `feat/clear-run-log`, "feat: clear the auto-import run log"

The runs are also the "already imported" list (`store.run_exists()`), so deleting them could import the last hour's emails again. Clearing **hides** them:

- [ ] `store.py`: `clear_runs(sub)` sets `runs_cleared_at`; `list_runs()` only lists runs after it. "Turn off and delete" still deletes everything.
- [ ] `POST /api/auto-import/runs/clear`, with the same guards as the other routes.
- [ ] A "Clear" button next to "last 20", with a confirm: "Hides these imports from the list. Emails already imported won't be imported again."
- [ ] Tests: cleared runs aren't listed; `run_exists()` is still true for them; guards.

### PR 2b: `feat/run-progress`, "feat: show each auto-import's progress in Recent imports"

Wireframe (add to `docs/design/wireframes.html`, W3): the running import sits at the top of Recent imports with an "Importing" badge, "started 12s ago", the steps (✓ PDF downloaded · ✓ 72 rows read · → Uploading to YNAB · Balance check · Categorize) and a thin progress bar. Emails waiting in the same run show "Waiting". Finished rows add "Took 41s"; a failed one says "Stopped at: <step>."

A run record can't be written at the start: `run_exists()` would then treat an email as done before it is, and a passing failure would never be retried. Progress lives on the user document instead.

- [ ] `pipeline.py`: one list of step names for every caller, and `import_statement(..., on_step=None)` calls `on_step(step, detail)` before each step.
- [ ] `store.py`: `set_progress()` / `clear_progress()` (a few writes per email; the free tier allows 20,000 a day).
- [ ] `runner.py`: report the download and read steps, pass `on_step` to `import_statement()`, add `step` and `duration_seconds` to the run record, and clear the progress in a `finally`. Progress not updated for 2 minutes shows as "Stopped — will retry at the next check".
- [ ] `GET /api/auto-import/runs` → `{"progress", "runs"}`. The page polls it every 3 s while an import runs, every 30 s otherwise, and only while the tab is visible.
- [ ] Tests: progress per step, cleared after success and after an error; stale progress shown as stopped; route guards.

### PR 2c: `feat/upload-progress`, "feat: show the manual upload's progress step by step"

Local and hosted alike. Wireframe (add to `docs/design/wireframes.html`, W1): card ① shows "✓ PDF opened · Reading rows… page 3 of 5 · Totals and running balance checked" while parsing; card ④ shows a checklist while uploading ("Statement read · Starting balance matches YNAB · 3 rows marked as transfers · Uploading 286 transactions… · Ending balance check · Categorize with Gemini"), with elapsed time and a progress bar, then the finished summary. A failed step turns red with its message.

- [ ] `pipeline.parse_statement(..., on_step=None)`; the parsers' `parse_transactions()` take an optional `on_page(page, pages)`.
- [ ] `web/upload.py`: the callbacks write the step into the browser's session state (in memory, like the statement itself); `GET /api/progress` returns it. The page polls it while `/api/parse` or `/api/upload` runs.
- [ ] `static/app.js`: one small stepper helper for cards ① and ④.
- [ ] Tests: `on_step` order for parse and import, and when YNAB fails half-way; `on_page` per page; `/api/progress` during a slow fake upload.

### PR 3: `feat/memo-references`, "feat: add a GoTyme Ref to every GoTyme memo"

GoTyme statements have no reference numbers, so the ref is the hash part of the row's import_id: search it in YNAB to find the row. It never changes for the same row.

- [ ] Keep the layers (sources never import `ynab_api`): the hash moves to a neutral `transaction_ref(source, key)` in `transaction.py`, and `make_import_id()` becomes `IMPORT_ID_PREFIX + transaction_ref(source, key)`, giving **exactly the same import_ids as before** (a test pins one), so nothing already imported is duplicated.
- [ ] `parsers/gotyme.py`: memo `GoTyme: <Details> | Ref: <transaction_ref>`. Example:

  | | |
  |---|---|
  | key | `28 Oct 2026\|0.00\|500.00\|3568.34\|0` |
  | import_id | `ETLPH:9ac288af95f6925425a5154d159507` |
  | GoTyme Ref | `9ac288af95f6925425a5154d159507` |
  | memo | `GoTyme: Card payment at STARBUCKS COFFEE WEB, Card •••• 1234 \| Ref: 9ac288af95f6925425a5154d159507` |

- [ ] GCash: correction rows already get `GCash Ref: R1000000000002` (and `S…`, `P…`, `M0000123456789`). Add tests for the S, P and M memos next to the R one.
- [ ] Rows already in YNAB keep their old memo; only new imports get the ref.

### PR 4: `feat/categorize-after-import`, "feat: categorize a statement's new transactions with Gemini (hosted, opt-in)"

Runs only when all hold: hosted mode, `GEMINI_API_KEY` set (in `APP_CONFIG`), and the user turned it on. Only the transactions **this import created**, never anything else in the plan.

- [ ] `categorize.py`: `categorize_created(plan_id, created, …)` skips rows that have a category, are transfers or splits, or are flagged red (balance corrections aren't purchases).
- [ ] **80% rule**: Gemini also answers a `confidence` (0–100) for each pick; below `MIN_CONFIDENCE = 80` the transaction stays uncategorized. The command line follows the same rule and shows the confidence.
- [ ] `ynab_api/categories.py`: one `transaction_dict()` shared by both paths.
- [ ] `gemini.py`: a `deadline`, so retries never run past Cloud Run's 60-second request.
- [ ] Manual upload: a "Categorize new transactions with Gemini" checkbox (only when available); after the upload, `POST /api/categorize` categorizes exactly the ids that upload created.
- [ ] Auto-import: a `categorize` setting on the mappings card; the run log shows "240 categorized".
- [ ] A Gemini failure never fails the import: "Imported, but not categorized: Gemini was busy."
- [ ] Privacy page, README, `.env.example`, `docs/architecture.md`: what goes to Google, and only when you opt in.
- [ ] Tests: only created ids; flagged, transfer and categorized rows skipped; a pick at 79 is left alone and one at 80 is set; the deadline.

### PR 5: `feat/multiple-gcash-accounts`, "feat: map several GCash accounts, told apart by mobile number"

The GCash email says `Mobile number: 0917123****` (first 7 digits), and its PDF is named `enc-0917123_<timestamp>.pdf`.

- [ ] `mappings` is keyed by a **wallet id** (`gcash`, `gcash-2`, …), each entry naming its `source`. An entry without `source` uses its key, so existing settings need no migration. The 7 digits are stored encrypted with the password inputs (together with the password's last 4 they make the full number).
- [ ] `Source.account_input` (`"mobile_first7"` for GCash) and `Source.email_account(body, filename)`; GCash reads the body first, then the filename.
- [ ] Runner: one Gmail search per source as before (run ids unchanged); each email goes to the wallet whose 7 digits match. A single GCash wallet without digits works as today. No match → a "⚠ No account" run.
- [ ] Page (wireframe in `docs/design/wireframes.html`): the mapping table's columns are **Wallet | YNAB account | To open the PDF**, the Wallet cell holds "First 7 digits of mobile", and "+ Add another GCash account" adds a row. The "Auto-import is on" card lists **Wallet | YNAB account**.
- [ ] `docs/adding-an-e-wallet.md`: `account_input` and `email_account`.
- [ ] Tests: the 7 digits from the body, from the filename, neither; routing with one, two and no matching wallets; old settings still render.

### PR 6: `feat/transfer-rules`, "feat: save transfer rules (Firestore when hosted, a local file otherwise)"

A rule is a **description** (text the statement row contains, ignoring case), a **source wallet** and a **destination wallet**. Example: GCash row "Transfer to 0917123…" → from GCash, to GoTyme.

- [x] `transfers.py` (no YNAB, no storage): `TransferRule` and `find_transfers()`, matching each source's `text_column`, so a new e-wallet needs no code here.
- [x] Hosted: rules on the Google user's Firestore record; wallets are the auto-import mappings. Local: a JSON file (`~/.config/ph-statement-import/settings.json`, or `LOCAL_SETTINGS_PATH`) with the rules and a small wallet → YNAB account list.
- [x] `/transfer-rules` page, linked from the upload and auto-import pages.
- [x] Tests: matching; the local file; routes in both modes.

### PR 7: `feat/import-transfers`, "feat: import rows matching a transfer rule as YNAB transfers"

- [ ] Spike first, on a test plan: does YNAB match the other side's own import to a transfer created through the API, or make a duplicate? Write the answer in the PR. Not done yet: it needs a real YNAB plan. The code doesn't depend on the answer, because it skips the other side itself (next box), as long as there's a rule for each side.
- [x] `ynab_api/transfers.py`: a matching row gets the destination account's transfer payee. If the other side is already in YNAB (same amount, within 3 days), the row isn't uploaded again and counts as "already in YNAB as a transfer".
- [x] `import_statement(..., transfers=None)`; the starting balance check counts those rows like already-imported ones. No separate progress step: it runs inside the upload step, and the page's checklist lists "N rows marked as transfers".
- [x] The parsed table marks matching rows "→ GoTyme"; the results and the run log show "12 transfers".
- [x] Tests: transfer payees, the balance math, the runner.

## Tests

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- Changing a mapping leaves past runs unchanged; "Clear" empties Recent imports and nothing is imported twice.
- "Edit mappings" shows "Connected to YNAB" an hour after auto-import was turned on, and the next poll still imports.
- An auto-import and a manual upload both show their steps as they go.
- Searching a GoTyme `Ref:` in YNAB finds exactly its row; re-importing creates nothing.
- With categorizing on, only the new import's rows get categories, and only Gemini's ≥ 80% picks.
- Two GCash numbers each import into their own YNAB account.
- A GCash → GoTyme transfer shows as one YNAB transfer after both statements are imported, and both ending balances still match.
- Billing → Reports still shows **$0.00**.
