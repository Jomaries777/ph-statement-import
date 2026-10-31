# Architecture

`ph-statement-import` parses password-protected Philippine e-wallet and bank statement PDFs, checks their balances, and imports the transactions into a budgeting app (currently YNAB).

The Python package is named `statement_etl` (from the project's original name, `statement-etl-ph`). Keeping `statement_etl` avoids churning every import and test across the codebase. Similarly, the OS keychain service name (`statement-etl-ph` in `statement_etl/web/token_store.py`) and the `ETLPH:` `import_id` prefix are kept unchanged so existing users' saved tokens and imported YNAB transactions continue to match.

## Three-part pipeline

The data flow is split into three parts so statement sources and budgeting-app destinations stay independent:

```
Source (parsers)           →  Transaction (neutral)         →  YNAB (destination)
statement_etl/parsers/*.py    statement_etl/transform.py       statement_etl/ynab_api/
statement_etl/sources.py      (statement_etl/transaction.py    (format, upload, accounts,
                               after P1)                        balance checks)
```

1. **Source (`statement_etl/parsers/` and `statement_etl/sources.py`):** Opens the encrypted PDF and extracts rows using the statement's own column names, verifies statement totals and running balances, and converts rows into neutral transactions.
2. **Transaction (`statement_etl/transform.py`, moving to `statement_etl/transaction.py` in P1):** Represents each transaction in a tracker-neutral format (`Decimal` pesos, positive for inflow, negative for outflow, with source key and stable transaction reference).
3. **YNAB (`statement_etl/ynab_api/`):** Formats transactions for YNAB's API (milliunits and deterministic `ETLPH:` `import_id`s), lists plans and accounts, checks starting and ending balances against YNAB, and uploads transactions in one batch.

Entry points sit on top of the pipeline:
- **Pipeline (`statement_etl/pipeline.py`):** Orchestrates parse → starting balance check → upload → ending balance check, and provides the CLI (`python -m statement_etl.pipeline`).
- **Web app (`statement_etl/web/`):** Local Flask web UI (`python -m statement_etl.web`) that calls the pipeline functions and stores personal access tokens in the OS keyring (`token_store.py`).
- **Auto-import runner (planned in P5):** Background polling for emailed statements using the same pipeline entry point.

## Extending the app

- **Adding an e-wallet or bank** means writing a parser in `statement_etl/parsers/` and registering a `Source` in `statement_etl/sources.py`. No YNAB or HTML code changes are needed. See [Adding an e-wallet or bank](adding-an-e-wallet.md).
- **Adding another budgeting app** (e.g. Actual Budget, Lunch Money, Firefly III, CSV export) means adding a new destination package next to `statement_etl/ynab_api/` that consumes the same neutral transactions. Until a second destination exists, there is deliberately no destination registry.

## Module ownership

| Module | What it owns |
|---|---|
| `statement_etl/sources.py` | Registry of supported e-wallets and banks (`SOURCES`) and their column metadata |
| `statement_etl/parsers/common.py` | Shared PDF helpers: `open_pdf()`, `group_lines()`, `to_decimal()` |
| `statement_etl/parsers/gcash.py` | GCash Transaction History PDF parser, totals verification, and balance-correction rows |
| `statement_etl/transform.py` | Converting parsed statement rows into YNAB transaction dicts and deterministic `ETLPH:` `import_id`s (split into `transaction.py` and `ynab_api/format.py` in P1) |
| `statement_etl/pipeline.py` | Pipeline steps (`parse_statement`, `starting_balance_check`, `new_account_starting_balance`, `upload_transactions`, `ending_balance_check`) and the CLI |
| `statement_etl/ynab_api/client.py` | YNAB SDK client setup, duplicate `import_id` lookup, batch transaction upload, and ending balance check |
| `statement_etl/ynab_api/accounts.py` | Fetching YNAB user info, plans (budgets), and accounts |
| `statement_etl/ynab_api/balance.py` | Comparing statement balances with YNAB account balances and setting `Starting Balance` on newly created YNAB accounts |
| `statement_etl/web/app.py` | Flask routes, session state, OAuth flow, and JSON endpoints for the web UI |
| `statement_etl/web/token_store.py` | Saving and deleting the YNAB personal access token in the OS secure store via `keyring` |
| `statement_etl/web/templates/index.html` | Single-file HTML/CSS/JS front-end for the 4-step import workflow |

## Design and roadmap

- **Design diagrams and wireframes:** [docs/design/diagrams.html](design/diagrams.html), [docs/design/wireframes.html](design/wireframes.html), and [docs/design/gcp-architecture.drawio](design/gcp-architecture.drawio)
- **Implementation plan (P0–P7):** [docs/plan/README.md](plan/README.md)
