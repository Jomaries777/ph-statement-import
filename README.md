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

You can also connect with your YNAB login instead of a personal access token:

1. In YNAB Developer Settings, create an OAuth application with the redirect URI `http://localhost:5050/oauth/callback`.
2. Set `YNAB_CLIENT_ID` and `YNAB_CLIENT_SECRET` in `.env`.

The page then also shows a **Connect with YNAB login** button next to the token field.

## Hosted mode

To run the app on a server for other people, set `PUBLIC_URL`. Leave it empty on your own computer: the app then works as described above.

In hosted mode:
- **YNAB login (OAuth) only.** The token field is gone and `/api/token` is refused, because YNAB's API terms don't allow asking other people for their personal access tokens.
- **No OS secure store.** A token saved on the server would connect every visitor, so the app never reads or writes it.
- **HTTPS-only cookies** (`Secure`, `HttpOnly`, `SameSite=Lax`), and the app trusts the one proxy hop Cloud Run adds for the client's IP and HTTPS.
- **Rate limits** per client IP: 10 statement parses and 20 YNAB login requests per minute.
- **Nothing is kept for long.** The parsed statement is dropped after the upload, and everything about a browser (its YNAB connection too) after 30 minutes without a request.

Both modes send security headers on every response, including a strict Content-Security-Policy. Both also serve `/privacy` and `/terms`, linked from the footer of every page. The privacy policy is the URL YNAB's OAuth app review asks for; set its contact email (`CONTACT_EMAIL` in `statement_etl/web/app.py`) before going live.

| Variable | What it is |
|---|---|
| `PUBLIC_URL` | The address people open, e.g. `https://statements.example.com`. Turns on hosted mode. The OAuth redirect URI is `PUBLIC_URL` + `/oauth/callback`; register exactly that in your YNAB OAuth application. |
| `YNAB_CLIENT_ID` | From your YNAB OAuth application. Not a secret, so a plain variable. |
| `APP_CONFIG` | The secrets as one JSON object: `{"FLASK_SECRET_KEY": "...", "YNAB_CLIENT_SECRET": "..."}`. Cloud Run passes it from Secret Manager. Each key can also be set as its own variable instead. |

`FLASK_SECRET_KEY`, `YNAB_CLIENT_ID` and `YNAB_CLIENT_SECRET` are required in hosted mode. If one is missing, the app stops at startup with a message that names the missing key (never a value). Without a fixed `FLASK_SECRET_KEY`, every restart would sign everyone out.

## Project layout

```
statement_etl/
├── sources.py      # the supported e-wallets / banks and their columns
├── parsers/        # one module per statement source (gcash.py) + shared helpers (common.py)
├── transaction.py  # the neutral Transaction, in no budgeting app's format
├── convert.py      # parsed rows -> Transactions, using the source's converter
├── pipeline.py     # command-line pipeline
├── ynab_api/       # YNAB: format.py (Transaction -> YNAB), client.py (upload), balance.py, accounts.py
└── web/            # Flask app, token store, page template (templates/) and its CSS / JS (static/)
tests/
├── test_<module>.py  # one unit test file per module above, e.g. test_parsers_gcash.py
├── fakes.py          # made-up PDF words, GCash rows and YNAB objects shared by the tests
└── e2e/              # browser tests: the web page against a fake YNAB
```

Run the commands from the project folder.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, test and linter commands, code standards, and pull request guidelines. To add an e-wallet or bank, follow [Adding an e-wallet or bank](docs/adding-an-e-wallet.md). See [docs/architecture.md](docs/architecture.md) for how the modules fit together and [docs/plan/](docs/plan/README.md) for the implementation roadmap.

Never commit a real statement, or names and numbers from one. Keep your PDFs in `statements/`, which is gitignored.

## Security

- Never commit `.env`; it's in `.gitignore`, and so are `*.pdf` files.
- A personal access token gives full access to your YNAB account. If it leaks, revoke it in YNAB Developer Settings and create a new one.

## License

Copyright (C) 2026 John Martin Magayanes

ph-statement-import is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. See [LICENSE](LICENSE).
