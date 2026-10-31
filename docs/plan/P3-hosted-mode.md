# P3. Hosted mode (safe to run for other people)

Status: Not started · Estimate: about 2–3 days, 5 PRs · Needs: P1 · Can run at the same time as P2

## Goal

One switch, `PUBLIC_URL`, turns the app into a version that's safe to host for other people. When `PUBLIC_URL` is empty, **local mode behaves exactly as today**.

In hosted mode:
- YNAB OAuth only: no personal access token field, no OS keychain.
- Secure cookies, security headers and rate limits.
- Parsed statements are dropped after upload or after 30 minutes idle.
- `/privacy` and `/terms` pages.
- A Docker image that runs it with gunicorn.

**Why the keychain must be off:** in local mode `get_token()` falls back to a token saved in the OS keychain. On a server, that one saved token would load into **every visitor's** session. Hosted mode must never call `token_store`.

**Why no personal access tokens:** YNAB's API terms forbid asking other people for theirs. OAuth is the only option for apps other people use.

## Before you start

- P1 merged: the web app is split into `web/app.py`, `web/state.py`, `web/ynab_auth.py`, `web/ynab_oauth.py` and `web/upload.py`.
- Docker Desktop (or another Docker engine) for PR 4.
- A local YNAB OAuth app is optional. You can test hosted mode's OAuth redirect with a fake one, as the existing e2e tests do (`tests/e2e/fake_ynab.py`).

## Steps

### PR 1: `refactor/static-assets`, "refactor: move the page's inline CSS and JS into static files"

A strict Content-Security-Policy (`default-src 'self'`) blocks inline `<script>` and `<style>`. Moving them out also makes the 27 KB template easier to read.

- [ ] Move the `<style>` block in `templates/index.html` to `statement_etl/web/static/app.css`, and the `<script>` block to `static/app.js`. Link both with `/static/...`.
- [ ] Replace the inline `style="margin-top:12px"` attributes with a CSS class.
- [ ] If the script reads values the template fills in (`{{ … }}`), pass them in a `<script type="application/json" id="page-config">` block (not executed, so CSP allows it) or as `data-` attributes, and read them in `app.js`.
- [ ] No behavior change: the e2e tests (`REQUIRE_E2E=1`) must pass unchanged.

### PR 2: `feat/hosted-mode`, "feat: add hosted mode with OAuth only"

- [ ] Add a small config section (in `web/state.py`, or `web/config.py` if that reads better):
  ```python
  PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
  HOSTED = bool(PUBLIC_URL)  # hosted mode: OAuth only, no keychain, strict cookies
  ```
  Document both in the module docstring, `.env.example` and the README.
- [ ] **Read the combined secret.** In hosted mode, Cloud Run passes one env var, `APP_CONFIG`, holding JSON (`{"FLASK_SECRET_KEY": "...", "YNAB_CLIENT_SECRET": "..."}`). At startup, load each key into the config, then read config as usual.
  - A missing key fails at startup, with a message naming **only the key**, never any values.
  - Locally, `.env` works as today.
- [ ] `FLASK_SECRET_KEY` is **required** in hosted mode: exit at startup with a clear message. Otherwise every restart would sign everyone out. Local mode keeps the random-key fallback and its note.
- [ ] `web/ynab_auth.py`:
  - `REDIRECT_URI = f"{PUBLIC_URL}/oauth/callback"` when `HOSTED`; otherwise the current localhost one.
  - `get_token()`, `oauth_logout()` and `api_token()`: call `token_store.load_token/save_token/delete_token` **only when not `HOSTED`**.
  - `/api/token` returns **403** with `{"error": "Personal access tokens are only supported in the local version. Use Connect with YNAB login."}` when `HOSTED`.
- [ ] `web/upload.py` `/api/status`: add `"token_allowed": not HOSTED`. `remember_available` is always `false` when `HOSTED`.
- [ ] `static/app.js` and the template: when `token_allowed` is false, hide the token button, token input, token note and the "remember" row, so only **Connect with YNAB login** shows.
- [ ] `web/app.py` `main()`: skip the keychain message when `HOSTED`.

### PR 3: `feat/hosted-security`, "feat: secure cookies, headers, rate limits and idle expiry in hosted mode"

- [ ] `requirements.txt`: add `Flask-Limiter==X.Y.Z` (pinned).
- [ ] `web/app.py`, when `HOSTED`:
  ```python
  app.config.update(
      SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax"
  )
  app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # Cloud Run terminates HTTPS
  ```
- [ ] Add an `after_request` hook, in both modes, that sets security headers:
  - `Content-Security-Policy: default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://app.ynab.com`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `X-Content-Type-Options: nosniff`

  Check the page still works in the browser with no CSP errors in the console.
- [ ] Rate limits with Flask-Limiter (key = client IP via ProxyFix; storage `memory://`, which is fine with one instance):
  - `/api/parse`: 10 per minute
  - `/oauth/*`: 20 per minute

  A blocked request returns 429 with a JSON error **before** any parsing work. That's cheaper, which matters for the $0 bill.
- [ ] `web/state.py` idle expiry:
  - `_state()` stamps `last_seen`.
  - On each request, drop `STORE` entries idle for more than 30 minutes (constant `IDLE_SECONDS = 30 * 60`).
  - After a successful `/api/upload`, clear `rows`, `transactions` and `parsed` from the state.
- [ ] Logging: never log request bodies, PDF bytes, passwords or tokens. The code doesn't today; add a test that keeps it that way.

### PR 4: `feat/privacy-pages`, "feat: add privacy and terms pages"

YNAB's review checks the privacy policy for: why data is used; how it's handled, stored, secured and retained; that it isn't shared with third parties without users knowing; how to request deletion; and a "Last updated" date. The policy also serves as the privacy notice under the Philippines Data Privacy Act (RA 10173).

- [ ] Routes `/privacy` and `/terms` in `web/app.py`, with templates `templates/privacy.html` and `templates/terms.html` that use the same CSS as the main page.
- [ ] `privacy.html` covers, in plain language:
  - **The PDF and its password:** held in memory for your session only, never written to disk, gone after upload or 30 minutes idle.
  - **YNAB connection:** OAuth tokens are kept in memory only, and dropped when you disconnect or the session expires. The app never asks for your YNAB password or a personal access token.
  - **What's sent where:** transactions go only to YNAB's API. There are no analytics, ads or other third parties. Google Cloud hosts the app as a processor.
  - **Where it runs:** Google Cloud `us-west1` (United States). Nothing from a manual upload is stored there.
  - **Your rights:** how to ask about, or delete, your data, with a contact email (a placeholder until P4).
  - **Last updated:** a date.
  - **The PDF password unlocks the file only.** It isn't a bank or e-wallet login, and it isn't stored.

  P5 adds the auto-import sections.
- [ ] `terms.html`: provided as is; transactions are created unapproved for you to review; YNAB is a trademark of YNAB, and this app isn't affiliated with or endorsed by YNAB.
- [ ] Footer on every page: "Your PDF is processed in memory and never stored." · Privacy · Terms · Source code (link to the repo).
- [ ] Also link Privacy next to the "Connect with YNAB login" button.

### PR 5: `build/docker`, "build: add Dockerfile and gunicorn for Cloud Run"

- [ ] `requirements.txt`: add `gunicorn==X.Y.Z` (pinned).
- [ ] `Dockerfile`. Keep it slim, because Artifact Registry's free tier is 0.5 GB and P4 keeps 2 images:
  ```dockerfile
  FROM python:3.13-slim
  ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt
  COPY statement_etl ./statement_etl
  RUN useradd --uid 10001 app
  USER app
  # One worker: STORE lives in process memory. Threads handle concurrent requests.
  CMD exec gunicorn -w 1 --threads 8 --timeout 60 -b 0.0.0.0:${PORT:-8080} statement_etl.web.app:app
  ```
- [ ] `.dockerignore`: `.env`, `*.swp`, `.venv/`, `statements/`, `*.pdf`, `tests/`, `docs/`, `.git/`, `.github/`, `.DS_Store`, `test-results/`, `__pycache__/`.
- [ ] Build and run locally:
  ```bash
  docker build -t ph-statement-import .
  docker image ls ph-statement-import   # check the size; aim for under 250 MB
  docker run --rm -p 8080:8080 \
    -e PUBLIC_URL=http://localhost:8080 \
    -e APP_CONFIG='{"FLASK_SECRET_KEY":"local-test","YNAB_CLIENT_SECRET":"x"}' \
    -e YNAB_CLIENT_ID=x ph-statement-import
  ```
  Open http://localhost:8080: only "Connect with YNAB login" shows, with no token field.

  The Secure cookie won't be sent over plain `http://localhost`. That's expected: in hosted mode the cookie is HTTPS-only, and Cloud Run serves HTTPS.

## Tests

New tests:
- A **hosted-mode class** in `tests/test_web_app.py`. Patch the env with `PUBLIC_URL=https://example.test` and the `APP_CONFIG` JSON, then reload the web modules. It checks:
  - `/api/token` → 403
  - `token_store.load_token` is never called (patch it to fail if it is)
  - `/api/status` has `remember_available: false` and `token_allowed: false`
  - the OAuth redirect URI starts with `https://example.test/oauth/callback`
  - the `Set-Cookie` header has `Secure` and `HttpOnly`
  - the security headers are present
  - `STORE` entries older than `IDLE_SECONDS` are dropped (patch `time.time`)
  - `rows` and `transactions` are cleared after a successful upload
  - missing `FLASK_SECRET_KEY` in hosted mode fails at startup, and the message has no values
  - `/api/parse` returns 429 after the limit
- `/privacy` and `/terms` return 200 in both modes, and the footer links to them
- A logging test: run parse and upload with `assertLogs`, and check that no password, token or row text appears.
- An e2e test in `tests/e2e/test_web_flow.py`: in hosted mode the token form is hidden and only the OAuth button shows.
- A **local-mode regression check**: all existing tests pass unchanged.

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- All tests pass in both modes; local mode is unchanged.
- `docker run` (above) shows the OAuth-only page; the browser console shows no CSP errors.
- `curl -sI http://localhost:8080/` shows the CSP, `X-Frame-Options`, `Referrer-Policy` and `nosniff` headers.
- The image is under about 250 MB.

## Docs to update

- README:
  - a "Hosted mode" section listing the env vars `PUBLIC_URL`, `APP_CONFIG` and `YNAB_CLIENT_ID`
  - the Docker commands
- `.env.example`: `PUBLIC_URL=`, with a comment.
- `docs/architecture.md`: hosted vs local mode, and where each rule lives.

## Risks and rollback

| Risk | What to do |
|---|---|
| CSP blocks something on the page | Check the browser console. Move the offending inline code to `app.js` or `app.css`; don't add `'unsafe-inline'`. |
| ProxyFix trusts a spoofed `X-Forwarded-For` | `x_for=1` trusts only the one proxy hop Cloud Run adds. Don't raise it. |
| Hosted mode is accidentally on locally | It's on only when `PUBLIC_URL` is set. Leave it empty in `.env` for local use. |
| The rate limit locks out a real user | The limits are per minute and reset quickly. Raise them only if a real flow hits them. |

Each PR reverts on its own. PR 1 is a pure refactor and can stay even if the hosted-mode PRs are reverted. The privacy pages (PR 4) are harmless in local mode, so they can stay too.
