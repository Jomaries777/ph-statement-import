# ph-statement-import

Import Philippine e-wallet and bank statement PDFs into your budgeting app. It currently supports [YNAB](https://www.ynab.com/). For statements, it currently supports **GCash**. Want another e-wallet or bank? See [Adding an e-wallet or bank](docs/adding-an-e-wallet.md).

It parses the password-protected statement PDF, checks the running balance, and compares the statement's starting balance with your YNAB account. It then creates the transactions in YNAB, unapproved, so you can review them. Re-uploading the same statement doesn't create duplicates.

Everything runs locally on your machine. The PDF is never written to disk, and your data only goes to YNAB's API.

## Getting started

New to this? **Follow [Getting started](docs/getting-started.md)**. It walks through installing Python, downloading the code and importing your first statement, for macOS, Windows and Linux, with troubleshooting.

In short, with Python 3.13 installed:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m statement_etl.web
```

Then create a YNAB personal access token (YNAB → **Account Settings** → **Developer Settings** → **New Token**) and paste it on the page.

A `.env` file is optional for the web app. Copy `.env.example` to `.env` to change the port (`APP_PORT`) or for the settings the command line needs.

## Web app

```bash
python -m statement_etl.web
```

Open http://localhost:5050. Pick the source (e.g. GCash), upload the statement PDF and type its password. Paste your YNAB token in step 2, then pick the YNAB plan and account, check the balances, and upload.

The app binds to `127.0.0.1` only, so nothing is reachable from your network.

### Saving your YNAB token

Tick **Remember this token on this computer** when you connect, and the app saves the token in your operating system's secure store. After that, step 2 connects by itself every time you start the app.

| OS | Where it's saved | Where to see or delete it |
|---|---|---|
| macOS | Keychain | **Keychain Access** app, search `statement-etl-ph` |
| Windows | Credential Manager | **Control Panel → Credential Manager → Windows Credentials** |
| Linux | Secret Service (GNOME Keyring / KWallet) | **Passwords and Keys** (Seahorse) or **KWallet Manager** |

- **macOS:** the first time the app reads the token, macOS may ask whether `python` can use the `statement-etl-ph` keychain item. Click **Always Allow**. It can ask once more after you recreate `.venv` or upgrade Python.
- **Linux:** if your keyring is locked, the desktop asks for its password, usually your login password. On a machine without a desktop (a server, WSL) there's no secure store. The checkbox is hidden, and you enter the token each session.
- **Disconnect** ends this session only; the saved token stays. **Forget saved token** removes it from the computer.
- If you revoke the token in YNAB, the app removes the saved copy and asks for a new one.
- The token is saved per computer and per login. It never goes into this folder, `.env`, the browser or git.

## Command line

```bash
python -m statement_etl.ynab_api.accounts                                # find your plan and account ids
python -m statement_etl.pipeline statement.pdf --dry-run                 # parse only, nothing sent
python -m statement_etl.pipeline statement.pdf                           # parse and import
python -m statement_etl.pipeline statement.pdf --source gcash --dry-run  # pick the e-wallet / bank
```

`--source` defaults to `gcash`. The CLI reads the PDF password from that source's variable (`GCASH_PDF_PASSWORD` for GCash), and `YNAB_PLAN_ID` and `YNAB_ACCOUNT_ID`, from `.env`. You can also pass the plan and account as `--plan-id` / `--account-id`.

## Optional: OAuth instead of a token

If you host the app for other people, you can use YNAB OAuth instead of a personal access token:

1. In YNAB Developer Settings, create an OAuth application with the redirect URI `http://localhost:5050/oauth/callback`.
2. Set `YNAB_CLIENT_ID` and `YNAB_CLIENT_SECRET` in `.env`.

The page then also shows a **Connect with YNAB login** button next to the token field.

## Project layout

```
statement_etl/
├── sources.py      # the supported e-wallets / banks and their columns
├── parsers/        # one module per statement source (gcash.py) + shared helpers (common.py)
├── transform.py    # parsed rows -> YNAB transactions
├── pipeline.py     # command-line pipeline
├── ynab_api/       # YNAB calls: client.py (upload), balance.py, accounts.py
└── web/            # Flask app, token store, page template
tests/
├── test_<module>.py  # one unit test file per module above, e.g. test_parsers_gcash.py
├── fakes.py          # made-up PDF words, GCash rows and YNAB objects shared by the tests
└── e2e/              # browser tests: the web page against a fake YNAB
```

Run the commands from the project folder.

## Contributing

To add an e-wallet or bank, follow [Adding an e-wallet or bank](docs/adding-an-e-wallet.md): a parser, a transformer and one entry in `sources.py`, with no web page changes. The same guide covers changing the web page.

Run the tests before opening a pull request:

```bash
python -m unittest discover tests
```

The tests never reach YNAB or your OS secure store, and they don't need a real statement: the PDF, YNAB and the keychain are faked, and any unmocked YNAB call fails the test.

The browser tests in `tests/e2e/` are skipped unless Playwright is installed. To run them too:

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
REQUIRE_E2E=1 python -m unittest discover tests
```

GitHub Actions runs every test, browser tests included, on each push and pull request (`.github/workflows/tests.yml`).

Never commit a real statement, or names and numbers from one. Keep your PDFs in `statements/`, which is gitignored.

## Security

- Never commit `.env`; it's in `.gitignore`, and so are `*.pdf` files.
- A personal access token gives full access to your YNAB account. If it leaks, revoke it in YNAB Developer Settings and create a new one.

## License

Copyright (C) 2026 John Martin Magayanes

ph-statement-import is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. See [LICENSE](LICENSE).
