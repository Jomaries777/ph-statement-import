# Architecture

`ph-statement-import` parses password-protected Philippine e-wallet and bank statement PDFs, checks their balances, and imports the transactions into a budgeting app (currently YNAB).

The Python package is named `statement_etl` (from the project's original name, `statement-etl-ph`). Keeping `statement_etl` avoids churning every import and test across the codebase. Similarly, the OS keychain service name (`statement-etl-ph` in `statement_etl/web/token_store.py`) and the `ETLPH:` `import_id` prefix are kept unchanged so existing users' saved tokens and imported YNAB transactions continue to match.

## Three-part pipeline

The data flow is split into three parts so statement sources and budgeting-app destinations stay independent:

```
Source (parsers)           →  Transaction (neutral)          →  YNAB (destination)
statement_etl/parsers/*.py    statement_etl/transaction.py      statement_etl/ynab_api/
statement_etl/sources.py                                        (format, upload, accounts,
                                                                 balance checks)
```

`statement_etl/convert.py` joins the first two: `rows_to_transactions(rows, source)` picks the `Source` and calls its `to_transactions`.

1. **Source (`statement_etl/parsers/` and `statement_etl/sources.py`):** Opens the encrypted PDF and extracts rows using the statement's own column names, verifies statement totals and running balances, and converts rows into neutral transactions.
2. **Transaction (`statement_etl/transaction.py`):** Represents each transaction in a tracker-neutral format (`Decimal` pesos, positive for inflow, negative for outflow, with source key and stable transaction reference, and a `flagged` marker for balance-correction rows). It holds the data shape and `transaction_ref()`, the stable hash of a row's source and key that YNAB's `import_id` is built from (GoTyme memos show it as `Ref:`).
3. **YNAB (`statement_etl/ynab_api/`):** Formats transactions for YNAB's API (`format.to_ynab()`: milliunits and deterministic `ETLPH:` `import_id`s), lists plans and accounts, checks starting and ending balances against YNAB, and uploads transactions in one batch.

Entry points sit on top of the pipeline:
- **Pipeline (`statement_etl/pipeline.py`):** Three steps every caller uses: `parse_statement()`, `starting_balance_check()` (shown before the upload), and `import_statement()`, the one place that imports: it sets a new account's Starting Balance, uploads, and checks the ending balance, returning an `ImportResult`. Also the CLI (`python -m statement_etl.pipeline`).
- **Web app (`statement_etl/web/`):** Local and hosted Flask web UI (`python -m statement_etl.web`), split into blueprints: `upload.py` (the page and the import steps), `ynab_auth.py` (connecting to YNAB), and `auto_import.py` (Gmail auto-import settings, Google sign-in, and the scheduler route, registered only in hosted mode when `GOOGLE_CLIENT_ID` is set). It calls the same three pipeline steps as the CLI and stores personal access tokens in the OS keyring (`token_store.py`) in local mode only. While `/api/parse` or `/api/upload` runs, their `on_step` callback keeps the current step in the browser's in-memory session state, and the page polls `GET /api/progress` every 700 ms to show it as a checklist (both servers answer requests in threads).
- **Categorize (`statement_etl/categorize.py`):** Optional CLI run after an import (`python -m statement_etl.categorize`). Reads the plan's categories and uncategorized transactions (`ynab_api/categories.py`), asks Gemini to pick one per transaction with its confidence (`gemini.py`), keeps only picks that are real categories and at least `MIN_CONFIDENCE` (80) sure, and saves them in one call without approving anything. In hosted mode with `GEMINI_API_KEY` (`config.CATEGORIZE_AVAILABLE`), `categorize_transactions()` also runs right after an import for only what it created (`uncategorized_created()`), when the person ticked it: the upload page calls `POST /api/categorize` after `/api/upload`, and the auto-import runner does it after `import_statement()` (user field `categorize`). Both pass a deadline so Gemini's retries fit in Cloud Run's 60 seconds, and a failure is noted without undoing the import.
- **Auto-import (`statement_etl/autoimport/`):** Hosted-only background polling for emailed statements (`runner.py`), using an encrypted Firestore settings store (`store.py`) and a `urllib` Gmail REST client (`gmail.py`), calling the same `parse_statement()` and `import_statement()`.

## Auto-import flow (hosted mode)

When both `PUBLIC_URL` and `GOOGLE_CLIENT_ID` are set, `web/app.py` registers `web/auto_import.py`:

1. **Opt-in setup (`/auto-import`):** An allowlisted Google user (`ALLOWED_GOOGLE_EMAILS` in `APP_CONFIG`) connects read-only Gmail (`/google/login` → `/google/callback`), connects YNAB via OAuth, maps each wallet (an account at a source with `Source.email_query`: `gcash`, `gcash-2`, …) to a distinct YNAB account, and enters that wallet's `Source.password_inputs` once. A source with `Source.account_input` (GCash: the first 7 digits of the mobile number) can have several wallets; the runner sends each email to the one `Source.email_account()` names, and records "⚠ No account" when none matches. On `POST /api/auto-import`, the app refreshes the single-use YNAB refresh token once via `ynab_oauth.refresh()`, stores the rotated refresh token encrypted in Firestore, and drops `refresh_token` from the browser session (keeping only the short-lived access token) so the browser and background runner never race on the same token.
2. **Encryption before Firestore (`autoimport/store.py`):** Refresh tokens and password inputs are encrypted in the app with `MultiFernet` (`ENCRYPTION_KEYS`, newest key first) before being written to `auto_import_users/{google_sub}`.
3. **15-minute poll (`POST /tasks/poll-gmail` → `runner.poll_all()`):** Cloud Scheduler sends an OIDC bearer token verified against `<PUBLIC_URL>/tasks/poll-gmail` and `scheduler-invoker@<PROJECT_ID>.iam.gserviceaccount.com` (403 on any failure before doing work). For each enabled user, `runner.process_user()` refreshes the Gmail token and searches Gmail with `Source.email_query` since `last_checked - 1h`, oldest first, skipping messages already recorded in `runs/{msg_id}:{source}`. Only if it finds a new statement email does it refresh the YNAB token (saving YNAB's rotated refresh token immediately). For each email it downloads the first attachment matching `Source.attachment_pattern` in memory, builds the PDF password via `Source.build_password`, runs `parse_statement()` and `import_statement()` (unapproved), and records the run status.
4. **Gmail push (`POST /tasks/gmail-push` → `runner.process_push()`), when `GMAIL_PUSH_TOPIC` is set:** Saving the settings calls `runner.start_watch()`, which asks Gmail (`gmail.watch()`) to publish to the Pub/Sub topic `gmail-push` whenever the user's inbox changes. Each message holds only the email address and a change number. A push subscription posts it with an OIDC token, verified against `<PUBLIC_URL>/tasks/gmail-push` and `pubsub-push@<PROJECT_ID>.iam.gserviceaccount.com`. The route finds the enabled user with that address and runs the same `process_user()` as the poll, so a statement is imported within seconds. It answers 204, or 503 when a poll holds `runner._lock`, so Pub/Sub sends the push again. The poll renews each watch before its 7 days end, and still imports anything a push missed.
5. **Edit mappings (`POST /api/auto-import/ynab-session`):** The browser's own YNAB session ends after 30 minutes idle, and saving moved its refresh token into Firestore. Before showing the setup view, the page asks this route to connect the browser with the saved YNAB refresh token: it holds the runner's lock (`runner.run_lock_held()`), refreshes, saves the rotated refresh token to Firestore first, and keeps only the access token in the session.
6. **Clear the run log (`POST /api/auto-import/runs/clear`):** Sets `runs_cleared_at` on the user's document; the page lists only runs after it. The runs stay stored, because `store.run_exists()` uses them to skip emails already imported.
7. **Progress (`GET /auto-import/runs`):** While a run imports, `runner.Progress` keeps the email list, the current step and the row count in the user's `progress` field (written only when the step changes; cleared when the run ends, even if it raised). Steps come from `pipeline.parse_statement()` / `import_statement()` through their `on_step` callback. The page re-fetches the Recent imports card (`templates/auto_import_runs.html`) every 3 s while an import runs and every 30 s otherwise, only while the tab is visible. Progress not updated for 2 minutes is shown as stopped. Each run records the step it ended on and how long it took.
8. **Delete (`POST /api/auto-import/delete`):** Removes the user's Firestore document and `runs` subcollection, stops Gmail push via `gmail.stop()`, revokes the Google token via `gmail.revoke()`, and clears the Google keys from the Flask session.

## Local and hosted mode

One setting, `PUBLIC_URL`, picks the mode. Empty (the default) is **local mode**: the app on your own computer, bound to `127.0.0.1`. Set, it is **hosted mode**: the app on a server for other people. `config.HOSTED` is read once at startup, and each rule lives next to the code it changes:

| Rule in hosted mode | Where |
|---|---|
| `FLASK_SECRET_KEY`, `YNAB_CLIENT_ID` and `YNAB_CLIENT_SECRET` are required; `APP_CONFIG` JSON is copied into the environment | `config.py` `load_hosted_config()` |
| OAuth redirect URI is `PUBLIC_URL/oauth/callback` | `config.py` `REDIRECT_URI` |
| No personal access tokens: `/api/token` answers 403 | `ynab_auth.py` `api_token()` |
| The OS secure store is never read or written | `ynab_auth.py` `get_token()` and `oauth_logout()`; `upload.py` `/api/status`; `app.py` `main()` |
| The page shows only **Connect with YNAB login** | `templates/index.html` (`token_allowed`), `static/app.js` |
| Session cookie is `Secure`, `HttpOnly`, `SameSite=Lax`; `ProxyFix` trusts Cloud Run's one proxy hop | `app.py` |
| Rate limits: `/api/parse` 10 per minute, `/oauth/*` 20 per minute, `/google/*` and `/api/auto-import*` 20 per minute, per client IP; a 429 JSON error | `limits.py`; the decorators in `upload.py`, `ynab_auth.py` and `auto_import.py`; the 429 handler in `app.py` |
| Gmail auto-import routes (`/auto-import`, `/google/*`, `/api/auto-import*`, `/tasks/*`) exist only when `GOOGLE_CLIENT_ID` is also set (which requires `GOOGLE_CLIENT_SECRET`, `ENCRYPTION_KEYS` and `ALLOWED_GOOGLE_EMAILS`) | `config.py` `load_hosted_config()`; `app.py`; `auto_import.py` |
| A browser's state is dropped after 30 minutes idle (`IDLE_SECONDS`) | `state.py` `session_state()` and `drop_idle_states()`, run before each request (`app.py`) |
| The parsed statement is dropped after a successful upload | `upload.py` `api_upload()` |

In both modes, every response gets a strict Content-Security-Policy (`default-src 'self'`, no inline code), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff` (`app.py` `SECURITY_HEADERS`).

## Deployment

In hosted mode on Google Cloud (`us-west1`, sized to stay inside the monthly free tier; see [docs/plan/P4-go-live-on-gcp.md](plan/P4-go-live-on-gcp.md)), one container image in Artifact Registry runs two Cloud Run services under separate service accounts:

- **`ph-statement-import` (`statement_etl.web.app:app`):** Public web service (`min-instances=0`, `max-instances=1`) for YNAB OAuth login and in-memory statement uploads. Its runtime service account can read only one Secret Manager JSON secret, `app-config` (`FLASK_SECRET_KEY` and `YNAB_CLIENT_SECRET`), passed as `APP_CONFIG`.
- **`budget-guard` (`statement_etl.ops.budget_guard:app`):** Private kill-switch service invoked by a Pub/Sub push subscription on `budget-alerts`. If a Cloud Billing Budget notification reports `costAmount > budgetAmount`, it unlinks billing from the project via the Cloud Billing API (`BUDGET_GUARD_DRY_RUN=1` logs `"would disable billing"` without unlinking).

## Extending the app

- **Adding an e-wallet or bank** means writing a parser in `statement_etl/parsers/` and registering a `Source` in `statement_etl/sources.py`. No YNAB or HTML code changes are needed. See [Adding an e-wallet or bank](adding-an-e-wallet.md).
- **Adding another budgeting app** (e.g. Actual Budget, Lunch Money, Firefly III, CSV export) means adding a new destination package next to `statement_etl/ynab_api/` that consumes the same neutral transactions. Until a second destination exists, there is deliberately no destination registry.

## Module ownership

| Module | What it owns |
|---|---|
| `statement_etl/sources.py` | Registry of supported e-wallets and banks (`SOURCES`): their column metadata and parser functions, including `to_transactions` |
| `statement_etl/parsers/common.py` | Shared PDF helpers: `open_pdf()`, `group_lines()`, `to_decimal()` |
| `statement_etl/parsers/gcash.py` | GCash Transaction History PDF parser, totals verification, balance-correction rows, and `to_transactions()` |
| `statement_etl/parsers/gotyme.py` | GoTyme Everyday Deposit Account PDF parser, summary/totals/running-balance checks, and `to_transactions()` |
| `statement_etl/transaction.py` | The neutral `Transaction` dataclass |
| `statement_etl/convert.py` | `rows_to_transactions(rows, source)`: picks the source and calls its converter; CLI that prints the YNAB JSON (`python -m statement_etl.convert`) |
| `statement_etl/pipeline.py` | Pipeline steps (`parse_statement`, `starting_balance_check`, and `import_statement`, which runs `new_account_starting_balance`, `upload_transactions` and `ending_balance_check`) and the CLI |
| `statement_etl/ynab_api/format.py` | `to_ynab()`: `Transaction` → YNAB transaction dict; milliunits and deterministic `ETLPH:` `import_id`s |
| `statement_etl/ynab_api/client.py` | YNAB SDK client setup, duplicate `import_id` lookup, batch transaction upload, and ending balance check |
| `statement_etl/ynab_api/accounts.py` | Fetching YNAB user info, plans (budgets), and accounts |
| `statement_etl/ynab_api/categories.py` | The categories a transaction can be given, the plan's uncategorized transactions (no transfers or splits), the ones an import just created with no category (`uncategorized_created()`), and setting categories in one call |
| `statement_etl/ynab_api/balance.py` | Comparing statement balances with YNAB account balances and setting `Starting Balance` on newly created YNAB accounts |
| `statement_etl/categorize.py` | `categorize_uncategorized()`, `categorize_transactions()` and `suggest_categories()`: the Gemini prompt, checking its picks (`MIN_CONFIDENCE`), saving them; CLI (`python -m statement_etl.categorize`) |
| `statement_etl/gemini.py` | `generate_json()`: one Gemini `generateContent` call with a JSON response schema, via `urllib`; errors never contain the API key |
| `statement_etl/autoimport/store.py` | Encrypted Firestore storage (`MultiFernet` via `ENCRYPTION_KEYS`) for opted-in users' refresh tokens, mappings, password inputs, and `runs` subcollection |
| `statement_etl/autoimport/gmail.py` | Session-independent Google OAuth and Gmail REST client (`urllib`): consent URL, token exchange/refresh/revoke, paginated search, and in-memory PDF attachment download |
| `statement_etl/autoimport/runner.py` | `poll_all()` background importer across enabled users and sources, `process_push()` for one user's Gmail push, `start_watch()`, plus `test_password()` for testing password inputs on the newest email without importing |
| `statement_etl/web/app.py` | Creates the Flask `app`, registers the blueprints (`auto_import` only when `HOSTED` and `GOOGLE_CLIENT_ID` are set), sets the security headers and hosted mode's cookie, proxy and idle rules, and `main()` (`python -m statement_etl.web`) |
| `statement_etl/web/upload.py` | Blueprint `upload`: the page, `/api/status`, `/api/parse`, plans and accounts, the starting balance check, and `/api/upload` |
| `statement_etl/web/ynab_auth.py` | Blueprint `ynab_auth`: `/oauth/*` and `/api/token`; `get_token()` and `ynab_call()`, which every YNAB call from the page goes through |
| `statement_etl/web/auto_import.py` | Blueprint `auto_import`: `/auto-import`, `/google/*` sign-in + invite check, `/api/auto-import*` save/test/delete (with single-use YNAB refresh token rotation), and the OIDC-verified task routes `/tasks/poll-gmail` and `/tasks/gmail-push` |
| `statement_etl/web/ynab_oauth.py` | YNAB OAuth token exchange and refresh, with no Flask session (so a background job can use it) |
| `statement_etl/web/state.py` | `STORE`, each browser's server-side state (tokens and parsed rows, in memory only), and its idle expiry in hosted mode |
| `statement_etl/web/limits.py` | The Flask-Limiter `limiter` and the rate limits (hosted mode only) |
| `statement_etl/web/config.py` | `PORT`, the OAuth `REDIRECT_URI` and `HOSTED` (hosted mode); loads `.env` and, in hosted mode, `APP_CONFIG` |
| `statement_etl/web/token_store.py` | Saving and deleting the YNAB personal access token in the OS secure store via `keyring` |
| `statement_etl/web/templates/index.html`, `auto_import.html` | The manual 4-step import page and the hosted Gmail auto-import setup / run-log page |
| `statement_etl/web/templates/privacy.html`, `terms.html`, `footer.html` | The privacy policy (also the RA 10173 privacy notice and Google Limited Use disclosure) and terms, served by `app.py`; the footer every page includes |
| `statement_etl/web/static/app.css`, `app.js`, `auto_import.js` | Shared styles and page scripts (no inline code, so a strict Content-Security-Policy works) |

## Design and roadmap

- **Design diagrams and wireframes:** [docs/design/diagrams.html](design/diagrams.html), [docs/design/wireframes.html](design/wireframes.html), and [docs/design/gcp-architecture.drawio](design/gcp-architecture.drawio)
- **Implementation plan (P0–P7):** [docs/plan/README.md](plan/README.md)
