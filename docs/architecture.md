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
2. **Transaction (`statement_etl/transaction.py`):** Represents each transaction in a tracker-neutral format (`Decimal` pesos, positive for inflow, negative for outflow, with source key and stable transaction reference, and a `flagged` marker for balance-correction rows). It holds only the data shape, no logic.
3. **YNAB (`statement_etl/ynab_api/`):** Formats transactions for YNAB's API (`format.to_ynab()`: milliunits and deterministic `ETLPH:` `import_id`s), lists plans and accounts, checks starting and ending balances against YNAB, and uploads transactions in one batch.

Entry points sit on top of the pipeline:
- **Pipeline (`statement_etl/pipeline.py`):** Three steps every caller uses: `parse_statement()`, `starting_balance_check()` (shown before the upload), and `import_statement()`, the one place that imports: it sets a new account's Starting Balance, uploads, and checks the ending balance, returning an `ImportResult`. Also the CLI (`python -m statement_etl.pipeline`).
- **Web app (`statement_etl/web/`):** Local Flask web UI (`python -m statement_etl.web`), split into two blueprints: `upload.py` (the page and the import steps) and `ynab_auth.py` (connecting to YNAB). It calls the same three pipeline steps as the CLI and stores personal access tokens in the OS keyring (`token_store.py`).
- **Auto-import runner (planned in P5):** Background polling for emailed statements, calling the same `parse_statement()` and `import_statement()`.

## Extending the app

- **Adding an e-wallet or bank** means writing a parser in `statement_etl/parsers/` and registering a `Source` in `statement_etl/sources.py`. No YNAB or HTML code changes are needed. See [Adding an e-wallet or bank](adding-an-e-wallet.md).
- **Adding another budgeting app** (e.g. Actual Budget, Lunch Money, Firefly III, CSV export) means adding a new destination package next to `statement_etl/ynab_api/` that consumes the same neutral transactions. Until a second destination exists, there is deliberately no destination registry.

## Module ownership

| Module | What it owns |
|---|---|
| `statement_etl/sources.py` | Registry of supported e-wallets and banks (`SOURCES`): their column metadata and parser functions, including `to_transactions` |
| `statement_etl/parsers/common.py` | Shared PDF helpers: `open_pdf()`, `group_lines()`, `to_decimal()` |
| `statement_etl/parsers/gcash.py` | GCash Transaction History PDF parser, totals verification, balance-correction rows, and `to_transactions()` |
| `statement_etl/transaction.py` | The neutral `Transaction` dataclass |
| `statement_etl/convert.py` | `rows_to_transactions(rows, source)`: picks the source and calls its converter; CLI that prints the YNAB JSON (`python -m statement_etl.convert`) |
| `statement_etl/pipeline.py` | Pipeline steps (`parse_statement`, `starting_balance_check`, and `import_statement`, which runs `new_account_starting_balance`, `upload_transactions` and `ending_balance_check`) and the CLI |
| `statement_etl/ynab_api/format.py` | `to_ynab()`: `Transaction` → YNAB transaction dict; milliunits and deterministic `ETLPH:` `import_id`s |
| `statement_etl/ynab_api/client.py` | YNAB SDK client setup, duplicate `import_id` lookup, batch transaction upload, and ending balance check |
| `statement_etl/ynab_api/accounts.py` | Fetching YNAB user info, plans (budgets), and accounts |
| `statement_etl/ynab_api/balance.py` | Comparing statement balances with YNAB account balances and setting `Starting Balance` on newly created YNAB accounts |
| `statement_etl/web/app.py` | Creates the Flask `app`, registers the blueprints, and `main()` (`python -m statement_etl.web`) |
| `statement_etl/web/upload.py` | Blueprint `upload`: the page, `/api/status`, `/api/parse`, plans and accounts, the starting balance check, and `/api/upload` |
| `statement_etl/web/ynab_auth.py` | Blueprint `ynab_auth`: `/oauth/*` and `/api/token`; `get_token()` and `ynab_call()`, which every YNAB call from the page goes through |
| `statement_etl/web/ynab_oauth.py` | YNAB OAuth token exchange and refresh, with no Flask session (so a background job can use it) |
| `statement_etl/web/state.py` | `STORE`, each browser's server-side state (tokens and parsed rows, in memory only) |
| `statement_etl/web/config.py` | `PORT` and the OAuth `REDIRECT_URI`; loads `.env` |
| `statement_etl/web/token_store.py` | Saving and deleting the YNAB personal access token in the OS secure store via `keyring` |
| `statement_etl/web/templates/index.html` | Single-file HTML/CSS/JS front-end for the 4-step import workflow |

## Design and roadmap

- **Design diagrams and wireframes:** [docs/design/diagrams.html](design/diagrams.html), [docs/design/wireframes.html](design/wireframes.html), and [docs/design/gcp-architecture.drawio](design/gcp-architecture.drawio)
- **Implementation plan (P0–P7):** [docs/plan/README.md](plan/README.md)
