# Getting started: run the app on your computer

This guide takes you from downloading the code to your first statement in YNAB. You don't need to know Python. Follow the steps for your operating system, and copy each command as it is.

The app runs only on your computer. It opens in your web browser, but the page is served from your own machine (`http://localhost:5050`), not the internet. Your statement PDF is never saved to disk, and your data goes only to YNAB.

**Time needed:** about 15 minutes the first time, then about 1 minute each time after.

## What you need

- **A YNAB account.**
- **Your statement PDF** and its password. For GCash, request a transaction history in the GCash app; it arrives by email.
- **Python 3.13.** Check whether you have it:

  | OS | Check | If it's missing |
  |---|---|---|
  | macOS | `python3 --version` in **Terminal** | Install from [python.org/downloads](https://www.python.org/downloads/), or `brew install python@3.13` |
  | Windows | `py --version` in **PowerShell** | Install from [python.org/downloads](https://www.python.org/downloads/). On the first screen, tick **Add python.exe to PATH**. |
  | Linux | `python3 --version` | Install `python3` and `python3-venv` with your package manager |

  It should print `Python 3.13` or newer. Other 3.x versions may work, but 3.13 is the one the tests run on.

## Step 1: Download the code

Choose one:

- **With git:**
  ```bash
  git clone https://github.com/Jomaries777/ph-statement-import.git
  ```
  ```bash
  cd ph-statement-import
  ```
- **Without git:** on the GitHub page, click **Code → Download ZIP** and unzip it. Then open Terminal (or PowerShell) in the unzipped folder, `ph-statement-import-main`. For example, type `cd ` (with a space) and drag the folder onto the window.

Run every command below from this folder.

## Step 2: Install (once)

This creates a private Python environment in a `.venv` folder inside the project, so nothing is installed system-wide.

**macOS and Linux:**
```bash
python3 -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
py -3.13 -m venv .venv
```
```powershell
.venv\Scripts\activate
```
```powershell
pip install -r requirements.txt
```

When the environment is active, your prompt starts with `(.venv)`.

## Step 3: Create a YNAB token (once)

1. In YNAB, open **Account Settings → Developer Settings**.
2. Click **New Token** and copy the token.
3. YNAB shows it only once. Keep it somewhere safe, such as your password manager, until step 5.

The token gives full access to your YNAB account. Never share it or post it anywhere. If it leaks, revoke it in the same screen and create a new one.

## Step 4: Start the app

**macOS and Linux:**
```bash
python -m statement_etl.web
```

**Windows:**
```powershell
python -m statement_etl.web
```

The terminal shows `Open http://localhost:5050`. **Leave this window open** while you use the app; closing it stops the app.

You'll also see a red `WARNING: This is a development server`. That's expected: it's meant for hosting the app for other people, and running it on your own computer is fine.

## Step 5: Import a statement

Open **http://localhost:5050** in your browser. The page has four steps:

1. **Statement:** pick the source (e.g. GCash), choose the PDF, type its password, and click **Parse**. You'll see a preview of the transactions and a check that the statement adds up.
2. **YNAB:** paste your token from step 3 and click **Connect**.
   - Leave **Remember this token on this computer** ticked to save it in your computer's secure store (macOS Keychain, Windows Credential Manager or Linux Secret Service). Next time, this step connects by itself.
   - On macOS, Keychain may ask whether `python` can use the item. Click **Always Allow**.
3. **Plan & Account:** pick your YNAB plan and the account for this wallet. The page compares the statement's starting balance with YNAB's.
4. **Upload:** click **Upload to YNAB**. The page lists the transactions it created and checks the ending balance.

The transactions arrive in YNAB **unapproved**, so you can review and categorize them there. Uploading the same statement again doesn't create duplicates.

## Step 6: Stop the app

Go back to the terminal window and press **Ctrl+C**.

## Next time

You only need to start the app again. Open a terminal in the project folder, then:

**macOS and Linux:**
```bash
source .venv/bin/activate
```
```bash
python -m statement_etl.web
```

**Windows:**
```powershell
.venv\Scripts\activate
```
```powershell
python -m statement_etl.web
```

Then open http://localhost:5050.

## Updating to a new version

- **With git:** run `git pull`, then `pip install -r requirements.txt` with the environment active.
- **From a ZIP:** download and unzip the new version, then repeat step 2 in the new folder. If you made a `.env` file, copy it over.

A token you saved with **Remember this token** stays on your computer, so you won't need to enter it again.

## Optional settings (`.env`)

The web app works without any settings file. Create one only if you want to:
- **use a different port** than 5050: set `APP_PORT`
- **use the command line instead of the page:** see [Command line](../README.md#command-line) in the README

To create it:
```bash
cp .env.example .env
```
This works in PowerShell too.

Open `.env` in any text editor and fill in what you need; each line is explained in the file. Leave the rest empty. `FLASK_SECRET_KEY` isn't needed on your own computer; the app makes a new one each run.

Never share or commit `.env`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `python3: command not found`, or `py` isn't recognized | Python isn't installed or isn't on PATH. Install it (see [What you need](#what-you-need)). On Windows, rerun the installer and tick **Add python.exe to PATH**. |
| `ModuleNotFoundError: No module named 'flask'` | The environment isn't active. Run the activate command from [Next time](#next-time), then try again. |
| Windows: "running scripts is disabled on this system" when activating | Skip activating, and run the app with the environment's Python directly: `.venv\Scripts\python -m statement_etl.web` |
| macOS: the terminal stops before printing `Open http://localhost:5050` | The app is reading your saved token, and macOS is asking for permission in a Keychain dialog, which may be behind other windows. Click **Always Allow**. This happens again after you update or recreate `.venv`. |
| `Address already in use`, or the page doesn't load | Another program uses port 5050. Set `APP_PORT=5051` in `.env` (see above) and open http://localhost:5051. |
| "Wrong password" after **Parse** | Check the PDF password. For GCash it's your registered last name in lowercase plus the last 4 digits of your mobile number, e.g. `delacruz1234`. |
| The starting or ending balance doesn't match YNAB | It's a warning, not an error, and the upload is still allowed. Usually the YNAB account has transactions from before the statement period, or some transactions were entered by hand. Compare them in YNAB. |
| YNAB says the token is invalid | It may have been revoked. Create a new one (step 3). If you ticked **Remember**, the app removes the old one and asks again. |

Still stuck? [Open an issue](https://github.com/Jomaries777/ph-statement-import/issues). Remove names, account numbers and amounts first, and never attach a statement.
