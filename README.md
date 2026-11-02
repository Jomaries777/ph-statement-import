# ph-statement-import

Import Philippine e-wallet and bank statement PDFs into your budgeting app. It currently supports [YNAB](https://www.ynab.com/). For statements, it currently supports **GCash** and **GoTyme** (Everyday Deposit Account, and GoalSave savings: one PDF holds up to five GoalSave accounts, each imported into its own YNAB account; after parsing, pick the GoalSave No. to upload). Want another e-wallet or bank? See [Adding an e-wallet or bank](docs/adding-an-e-wallet.md).

It parses the password-protected statement PDF, checks the running balance, and compares the statement's starting balance with your YNAB account. It then creates the transactions in YNAB, unapproved, so you can review them. Re-uploading the same statement doesn't create duplicates.

Everything runs locally on your machine. The PDF is never written to disk, and your data only goes to YNAB's API (unless you choose to [categorize with Gemini](#optional-categorize-with-gemini)).

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

### Transfer rules

Money you move between your own wallets (say GCash to GoTyme) shows on both statements. On **Transfer rules** (top of the page) each rule is one direction between two of your YNAB accounts, with the text each one's statement shows for the move: from Bank #1 `GCash - 1161`, which shows `Transfer to 0917123`, to Bank #2 `GoTyme`, which shows `InstaPay from GCash`. Money going the other way shows different text (GCash's `Received GCash from ...`), so it gets its own rule with the banks swapped. Capital letters and extra spaces don't matter. Locally they're saved in `~/.config/ph-statement-import/settings.json` (set `LOCAL_SETTINGS_PATH` to use another file); hosted, they're saved with your Google account.

When you upload (or auto-import) a statement, the rows a rule matches are marked "→ GoTyme" and imported as YNAB transfers, so YNAB adds the other side to the GoTyme account. Import the other statement later, in either order, and its row is found already in YNAB and not added again. Bank #2's text is optional; without it only Bank #1's statements are matched.

GoTyme GoalSave and GoTyme Everyday need no rule: a GoalSave "Transfer to/from Deposit account" row is always a transfer with your Everyday account (pick it in step ③, under **Transfers to/from Deposit account go to**; auto-import uses the one you mapped). The Everyday "Transfer to/from GoalSave" rows don't say which GoalSave account, so import the GoalSave statement first: each Everyday row then finds the transfer its GoalSave account made. Auto-import does GoalSave first by itself.

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

## Optional: categorize with Gemini

Imported transactions arrive in YNAB without a category unless YNAB already knows the payee. After an import, this script asks Google's Gemini to pick one of **your own** YNAB categories for each uncategorized transaction:

```bash
python -m statement_etl.categorize --dry-run                       # show Gemini's picks, change nothing
python -m statement_etl.categorize                                 # set them in YNAB
python -m statement_etl.categorize --account-id ID --since 2026-10-01  # one account, recent ones only
```

It needs `BEARER_TOKEN`, `YNAB_PLAN_ID` and `GEMINI_API_KEY` (create one in [Google AI Studio](https://aistudio.google.com/apikey)) in `.env`. `GEMINI_MODEL` is optional. Gemini says how sure it is of each pick; one it's less than 80% sure of (`MIN_CONFIDENCE` in `categorize.py`) is left uncategorized, and so is one it says it's unsure of. The table shows each pick's confidence. If Gemini is busy, the script waits and tries again (up to 4 times, about 2 minutes in all) before giving up. Only the category changes: an unapproved transaction stays unapproved, so you still review each one in YNAB. Transfers and split transactions are skipped.

**What goes to Google:** your category and group names, and each uncategorized transaction's date, account name, payee, memo and amount. Not your YNAB token or balances. On Gemini's free tier, Google may use what you send to improve its products ([Gemini API terms](https://ai.google.dev/gemini-api/terms)).

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

### Docker

The `Dockerfile` runs hosted mode with gunicorn: one worker, because the server-side state lives in that process's memory, and 8 threads.

```bash
docker build -t ph-statement-import .
docker image ls ph-statement-import   # check the size; aim for under 250 MB
docker run --rm -p 8080:8080 \
  -e PUBLIC_URL=http://localhost:8080 \
  -e APP_CONFIG='{"FLASK_SECRET_KEY":"local-test","YNAB_CLIENT_SECRET":"x"}' \
  -e YNAB_CLIENT_ID=x ph-statement-import
```

Open http://localhost:8080: only **Connect with YNAB login** shows. Check the security headers with `curl -sI http://localhost:8080/`.

Over plain `http://localhost` the browser won't send the session cookie back, because in hosted mode it's HTTPS-only. That's expected: Cloud Run serves HTTPS. Deploying to Cloud Run is in [docs/plan/P4-go-live-on-gcp.md](docs/plan/P4-go-live-on-gcp.md).

| Variable | What it is |
|---|---|
| `PUBLIC_URL` | The address people open, e.g. `https://statements.example.com`. Turns on hosted mode. The OAuth redirect URI is `PUBLIC_URL` + `/oauth/callback`; register exactly that in your YNAB OAuth application. |
| `YNAB_CLIENT_ID` | From your YNAB OAuth application. Not a secret, so a plain variable. |
| `GOOGLE_CLIENT_ID` | Optional. From your Google Cloud OAuth 2.0 Web client (redirect URI `PUBLIC_URL` + `/google/callback`). Turns on `/auto-import` when set. |
| `APP_CONFIG` | The secrets as one JSON object: `{"FLASK_SECRET_KEY": "...", "YNAB_CLIENT_SECRET": "..."}` (plus `GOOGLE_CLIENT_SECRET`, `ENCRYPTION_KEYS` and `ALLOWED_GOOGLE_EMAILS` when `GOOGLE_CLIENT_ID` is set, and optionally `GEMINI_API_KEY`). Cloud Run passes it from Secret Manager. Each key can also be set as its own variable instead. |

With `GEMINI_API_KEY` in `APP_CONFIG`, the upload page shows **Categorize new transactions with Gemini** under the Upload button, and the auto-import page the same checkbox for every import. Each person turns it on for themselves (the upload page remembers it in the browser). It categorizes only the transactions that import created, and only with picks Gemini is at least 80% sure of; the rest stay uncategorized. A Gemini problem never undoes the import: the page or the run log says it wasn't categorized. Without the key the checkboxes don't show, and the server logs "Gemini categorizing is off" when it starts. A key added to `app-config` later is read only by a new revision, so redeploy after adding it. Locally, run `python -m statement_etl.categorize` instead.

`FLASK_SECRET_KEY`, `YNAB_CLIENT_ID` and `YNAB_CLIENT_SECRET` are required in hosted mode (`GOOGLE_CLIENT_SECRET`, `ENCRYPTION_KEYS` and `ALLOWED_GOOGLE_EMAILS` too when `GOOGLE_CLIENT_ID` is set). If one is missing, the app stops at startup with a message that names the missing key (never a value). Without a fixed `FLASK_SECRET_KEY`, every restart would sign everyone out.

### Auto-import (hosted)

In hosted mode with `GOOGLE_CLIENT_ID` set, invited users can open `/auto-import` (also linked from the main page header) to import emailed statements automatically:

1. **Connect Gmail** (`gmail.readonly`, read-only) — only Google accounts listed in `ALLOWED_GOOGLE_EMAILS` (in `APP_CONFIG`) can turn it on.
2. **Connect YNAB** via YNAB OAuth login.
3. **Choose one YNAB plan**, map each supported e-wallet (currently **GCash**, **GoTyme** and **GoTyme GoalSave**) to its own YNAB account, enter its PDF password inputs once (GCash: last name, prefilled from Google, and the last 4 digits of your mobile; GoTyme: date of birth), and click **Test** to check the newest statement email without importing. More than one GCash account? Click **+ Add another GCash account** and enter the first 7 digits of each account's mobile number: the statement email says which number it's for, so each one goes to its own YNAB account. GoalSave: add a row for each GoalSave account (up to 5) with the last 5 digits of its GoalSave No.; the date of birth is asked once, on the first row. The savings PDF in the GoTyme email is read once, and each account goes to its own YNAB account. **Test** lists the GoalSave No.s it found.
4. **Turn on** — with `GMAIL_PUSH_TOPIC` set, Gmail tells the app through Pub/Sub when your inbox changes (`POST /tasks/gmail-push`), so a statement is imported within seconds. Cloud Scheduler also calls `POST /tasks/poll-gmail` every 15 minutes, which imports anything a push missed and renews the Gmail watch. Both routes verify a Google OIDC token first. New matching statement PDFs are read in memory, parsed, and imported **unapproved** via `import_statement()`.
5. **Turn off and delete my data** removes the user's Firestore settings document and run log, revokes the Google token, and signs out of Google.

Refresh tokens and password inputs are encrypted in the app with `MultiFernet` (`ENCRYPTION_KEYS` in `APP_CONFIG`) before they reach Firestore. Setup details: [docs/plan/P5-gmail-auto-import-gcash.md](docs/plan/P5-gmail-auto-import-gcash.md).

## Deploy

Every merge to `main` deploys to Google Cloud Run through [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), once the owner approves it in the GitHub `production` environment. The workflow runs the unit tests, builds the Docker image, pushes it to Artifact Registry and deploys it. It logs in to Google Cloud with Workload Identity Federation, so no keys are stored in GitHub.

The one-time Google Cloud setup is in [docs/plan/P4-go-live-on-gcp.md](docs/plan/P4-go-live-on-gcp.md). The workflow reads these variables from the `production` environment (variables, not secrets; none is sensitive):

| Variable | What it is |
|---|---|
| `GCP_PROJECT_ID` | The Google Cloud project |
| `GCP_WIF_PROVIDER` | The Workload Identity Federation provider GitHub logs in through |
| `GCP_DEPLOYER` | The service account that deploys |
| `PUBLIC_URL` | The app's Cloud Run URL (see [Hosted mode](#hosted-mode)) |
| `YNAB_CLIENT_ID` | From the YNAB OAuth application |
| `GOOGLE_CLIENT_ID` | Optional; from the Google OAuth 2.0 Web client (empty keeps `/auto-import` off; `ALLOWED_GOOGLE_EMAILS` goes in `app-config`, not GitHub variables) |

## Project layout

```
statement_etl/
├── sources.py      # the supported e-wallets / banks and their columns
├── parsers/        # one module per statement source (gcash.py, gotyme.py) + shared helpers (common.py)
├── transaction.py  # the neutral Transaction, in no budgeting app's format
├── convert.py      # parsed rows -> Transactions, using the source's converter
├── pipeline.py     # command-line pipeline
├── transfers.py    # transfer rules: which rows are money moved between two of your accounts
├── local_settings.py # local mode's settings file (transfer rules)
├── categorize.py   # optional: categorize uncategorized YNAB transactions with Gemini
├── gemini.py       # Gemini REST client (urllib)
├── ynab_api/       # YNAB: format.py (Transaction -> YNAB), client.py (upload), balance.py, accounts.py, categories.py
├── autoimport/     # hosted Gmail auto-import: store.py (encrypted Firestore), gmail.py, runner.py
└── web/            # Flask app, token store, page templates (templates/) and CSS / JS (static/)
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
