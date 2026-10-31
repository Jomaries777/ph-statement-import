# P5. Gmail auto-import (GCash first)

Status: Not started · Estimate: about 4–5 days, 5 PRs · Needs: P4

## Goal

Invited users can turn on **auto-import** at `/auto-import`:
1. Connect Gmail (read-only).
2. Connect YNAB.
3. Pick one YNAB plan.
4. Map each supported e-wallet to its own YNAB account.
5. Enter the PDF password inputs once.

Every 15 minutes, Cloud Scheduler asks the app to check Gmail. A new GCash statement email is parsed and imported to the mapped account, **unapproved**, using the same `import_statement()` as the manual upload. The page shows the last 20 runs. **Turn off and delete** removes everything and revokes Gmail access.

This phase ships GCash only. GoTyme is added in P6, as one more `Source` with email settings; the mapping table picks it up automatically. Wireframes: `docs/design/wireframes.html` (W2 set up, W3 on).

## Why it's built this way

- **Polling, not Gmail push.** A Scheduler job every 15 minutes is simpler than Gmail `watch` plus Pub/Sub, which needs renewing every 7 days. It's well within the free tier at this scale.
- **Stored secrets, opt-in only.** A background run has no browser session, so it needs the Gmail refresh token, the YNAB refresh token, the mapping and the PDF password inputs. They're stored only for users who turn this on.
  - Tokens and password inputs are **encrypted in the app** with `MultiFernet`, using keys from the `app-config` secret, then stored in Firestore.
  - No Cloud KMS: it isn't free for a personal project.
- **The password is built from inputs, not read from the email.** The GCash email gives only the rule (last name in lowercase + last 4 digits of the mobile number) and masks the digits.
  - The user enters them once. The last name is prefilled from Google's `family_name`, and can be edited.
  - Trying all 10,000 combinations was rejected: it defeats GCash's protection.
- **Google limits:** `gmail.readonly` is a *restricted* scope. Publish the Google OAuth app **"In production", unverified**: up to 100 users, a one-time "unverified app" warning, no review fee.
  - Don't leave it in "Testing": refresh tokens there expire after 7 days, which would break background imports.
  - `ALLOWED_GOOGLE_EMAILS` limits who can turn the feature on.

## Before you start

- P4 live: the app runs on Cloud Run, the YNAB OAuth app exists, and the budget and kill switch are set up.
- A real GCash statement email in your Gmail. Request one in the GCash app (Transaction History → email). These emails only arrive when requested.
- Placeholders: `<PROJECT_ID>`, `<PROJECT_NUMBER>`, `<PUBLIC_URL>`, `<you@example.com>`.

## Cost check

| Service | Free per month (Firestore: per day) | Use | How it stays free |
|---|---|---|---|
| Cloud Scheduler | 3 jobs **per billing account** | 1 job | Check other projects on the same billing account: `gcloud scheduler jobs list --project=<other>`. gcash-ynab-etl already has one. |
| Cloud Run | 180,000 vCPU-s | ~2,900 polls × ~5 s ≈ 15,000 vCPU-s | min instances 0. If usage grows, poll every 30–60 minutes. |
| Firestore | 50k reads, 20k writes **per day**; 1 GiB; only the `(default)` database | A few reads per user per poll | Query only `enabled == true` users; check runs by document ID (`get`), never by listing |
| Secret Manager | 10,000 accesses | One per cold start (~3,000) | Still **one** secret: add the keys to `app-config`, don't create new secrets |

## Steps

### A. Google Cloud setup (console or CLI)

- [ ] Firestore, Native mode, the **`(default)`** database in `us-west1`:
  ```bash
  gcloud services enable firestore.googleapis.com cloudscheduler.googleapis.com gmail.googleapis.com
  gcloud firestore databases create --location=us-west1 --type=firestore-native
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member=serviceAccount:app-runtime@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/datastore.user
  ```
  Leave Firestore security rules **locked** (deny all client access). Only the server's service account reads and writes. Don't turn on backups or point-in-time recovery, which aren't free.
- [ ] Google OAuth consent screen (APIs & Services → OAuth consent screen):
  - User type **External**.
  - App name without "YNAB" (or "… for YNAB").
  - Support email.
  - Privacy policy `<PUBLIC_URL>/privacy`.
  - Scopes `openid`, `email`, `profile`, `https://www.googleapis.com/auth/gmail.readonly`.
  - **Publish to "In production"**, without submitting for verification.
- [ ] OAuth client (Credentials → Create → OAuth client ID → Web application). Authorized redirect URI: `<PUBLIC_URL>/google/callback`.
- [ ] Generate an encryption key, and add `GOOGLE_CLIENT_SECRET`, `ENCRYPTION_KEYS` and `ALLOWED_GOOGLE_EMAILS` to `app-config` (the same temp-file and destroy-old-version steps as in P4 step D):
  ```bash
  python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  ```
  - `ENCRYPTION_KEYS` is a comma-separated list, newest first. To rotate later, put the new key first and keep the old one until all data has been rewritten.
  - `ALLOWED_GOOGLE_EMAILS` is the invite list, comma-separated (you and family). It goes in the secret, not in a GitHub variable: this repo's Actions logs are public, and the deploy step would print it.
  - With `GOOGLE_CLIENT_ID` set, the app won't start without all three.
  - To change one later, add a new `app-config` version, then start a new revision. The running app reads the secret only when it starts:
    ```bash
    gcloud run services update ph-statement-import --region=us-west1 --update-labels=app-config-version=<N>
    ```
- [ ] GitHub `production` environment variable `GOOGLE_CLIENT_ID`, after the secret has the three keys above. `deploy.yml` passes it to the app (PR 5).
- [ ] Scheduler service account. The job and its permission are created in step C, after the route exists.
  ```bash
  gcloud iam service-accounts create scheduler-invoker
  ```

### B. PRs

#### PR 1: `feat/source-email-settings`, "feat: add email and password settings to Source"

- [ ] `statement_etl/sources.py`: add optional fields, documented in the `Source` docstring:
  - `email_query: str | None = None`: the Gmail search for this source's statement emails. Sources without it don't appear on `/auto-import`.
  - `attachment_pattern: str | None = None`: a regex for the PDF attachment's filename. An email may carry several PDFs.
  - `password_inputs: tuple = ()`: names of the inputs the user enters once, e.g. `("last_name", "mobile_last4")`.
  - `build_password: Callable | None = None`: `dict of inputs -> PDF password`.
- [ ] GCash values:
  - `email_query='from:noreply-transhistory@gcash.com subject:"Your GCash Transaction History" has:attachment filename:pdf'`
  - `attachment_pattern=r"\.pdf$"`
  - `password_inputs=("last_name", "mobile_last4")`
  - `build_password=gcash.build_password`: `last_name.lower().replace(" ", "") + mobile_last4`, with a docstring example `"Dela Cruz", "1234" -> "delacruz1234"`.
- [ ] `docs/adding-an-e-wallet.md`: a new section, "Auto-import from Gmail (optional)", describing the four fields.
- [ ] Tests:
  - `build_password("Dela Cruz", "1234") == "delacruz1234"`
  - every source with `email_query` also has `attachment_pattern`
  - every source with `password_inputs` also has `build_password`

#### PR 2: `feat/autoimport-store`, "feat: store auto-import settings encrypted in Firestore"

- [ ] `requirements.txt`: add pinned `google-cloud-firestore`, `google-auth` and `cryptography`.
- [ ] `statement_etl/autoimport/__init__.py` and `statement_etl/autoimport/store.py`. Module docstring: what's stored, where, and why it's encrypted.
  - `encrypt(text) -> str` and `decrypt(token) -> str`, using `MultiFernet` built from `ENCRYPTION_KEYS`.
  - User settings: `get_user(sub)`, `save_user(sub, **fields)`, `list_enabled_users()`, `delete_user(sub)` (which also deletes its `runs`), `set_last_checked(sub, when)`, `save_ynab_tokens(sub, tokens)`.
  - Runs: `run_exists(sub, msg_id, source_key)`, which does a document `get` on `runs/{msg_id}:{source_key}`; `write_run(sub, msg_id, source_key, **result)`; `list_runs(sub, limit=20)`.
  - The record (one per Google account):
    ```
    auto_import_users/{google_sub}
      email, enabled, last_checked
      gmail_refresh_token   (encrypted)
      ynab_refresh_token    (encrypted)
      plan_id, plan_name
      mappings: {gcash: {account_id, account_name, enabled}}
      password_inputs: {gcash: <encrypted JSON {last_name, mobile_last4}>}
      runs/{gmail_msg_id}:{source}: {source, account_id, status, created, skipped, message, received_at, ran_at}
    ```
- [ ] `tests/fakes.py`: an in-memory fake store with the same functions. Extend the safety net in `tests/__init__.py` so any **unmocked** Firestore call fails the test run.
- [ ] Tests:
  - encrypt/decrypt round trip
  - data encrypted with an old key still decrypts after a new key is added first
  - a wrong key raises a clear error
  - Firestore (faked at the client level) never receives a plaintext token or password input
  - `delete_user` removes the runs too
  - `app-config` parsing: a missing key fails with the key's name only

#### PR 3: `feat/autoimport-gmail`, "feat: Gmail client for statement emails"

- [ ] `statement_etl/autoimport/gmail.py`. Use plain HTTPS via `urllib`, the same way as `web/ynab_oauth.py`, so there's no heavy Google client library.
  - `authorize_url(state)`: scopes `openid email profile gmail.readonly`, `access_type=offline`, `prompt=consent`
  - `exchange_code(code) -> tokens` (including `id_token` for `sub`, `email` and `family_name`)
  - `refresh_access_token(refresh_token)`
  - `list_messages(access_token, query, after)`: adds `after:<epoch seconds>` to the query and follows `nextPageToken`
  - `get_pdf_attachment(access_token, msg_id, pattern) -> (filename, bytes) | None`: the **first** attachment whose filename matches `pattern`, downloaded in memory only
  - `revoke(token)`: `POST https://oauth2.googleapis.com/revoke`
- [ ] Extend the safety net: any unmocked call to `googleapis.com` fails the tests.
- [ ] Tests with a fake HTTP layer:
  - a query gets `after:` added
  - pagination works
  - attachment selection picks only the matching filename
  - no PDF → `None`
  - token refresh errors are surfaced

#### PR 4: `feat/autoimport-runner`, "feat: import new statement emails for each enabled user"

- [ ] `statement_etl/autoimport/runner.py`. Module docstring with the flow. Numbered steps in each function.
  - `poll_all()`: for each `list_enabled_users()`, call `process_user()`. **Never raises**: one bad user doesn't stop the others. Returns counts for the log line.
  - `process_user(user)`:
    1. decrypt the tokens
    2. refresh the Gmail token
    3. refresh the YNAB token with `web/ynab_oauth.refresh()`, and **save the rotated YNAB refresh token** with `store.save_ynab_tokens()`
    4. for each mapping with `enabled`, call `process_source()`
    5. `set_last_checked()`
  - `process_source(user, source_key, mapping)`: `list_messages(Source.email_query, after=last_checked)`, skipping messages where `run_exists(...)` is true, then `process_message()`.
  - `process_message(user, msg_id, source, mapping)`:
    1. `get_pdf_attachment(msg_id, source.attachment_pattern)`
    2. `pdf_password(user, source)`
    3. `pipeline.parse_statement()`
    4. `pipeline.import_statement()`
    5. `store.write_run()` with a status:
       - `ok`
       - `wrong password` (a `StatementError` from `open_pdf`)
       - `parse error`
       - `balance mismatch` (`ending_check` not matching)
       - `YNAB error` (an `ApiException`)
       - `no attachment`
  - `pdf_password(user, source)`: `source.build_password(json.loads(decrypt(user.password_inputs[source.key])))`
  - `test_password(user, source_key)`: run steps 1–3 on the **newest** matching email without importing, for the page's Test button.
  - **Log one line per run**, with the user's hashed sub, source and status. Never the email address, password or rows.
- [ ] Tests (`tests/test_autoimport.py`), with fake Gmail (PDF bytes from `tests/e2e/make_pdf.py`), fake store and fake YNAB:
  - a new email is imported once; the second poll skips it
  - two mapped sources (GCash plus a fake second source) each go to their own account; a "Don't import" source isn't searched
  - a wrong password → a `wrong password` run, nothing uploaded
  - one user's YNAB error doesn't stop the next user
  - the rotated YNAB refresh token is saved
  - no plaintext password or token appears in the log output

#### PR 5: `feat/autoimport-page`, "feat: auto-import settings page, Google sign-in and scheduler task"

- [x] `statement_etl/web/auto_import.py` (Blueprint), registered in `web/app.py` **only when `HOSTED` and `GOOGLE_CLIENT_ID` are set**:
  - `GET /auto-import`: the page (W2 when off, W3 when on), with the last 20 runs.
  - `GET /google/login` → Google; `GET /google/callback`:
    - checks the OAuth `state`
    - exchanges the code
    - **rejects emails not in `ALLOWED_GOOGLE_EMAILS`** with a friendly page
    - saves the user with `family_name` for the last-name prefill
  - `POST /api/auto-import`: save the plan, mappings and password inputs.
    - Reject two sources mapped to the **same account**, because their balance checks would conflict.
    - Reject missing password inputs for an enabled mapping.
    - Store the current session's YNAB refresh token, encrypted.
  - `POST /api/auto-import/test`: `runner.test_password()` for one source; returns OK, or the reason it failed.
  - `POST /api/auto-import/delete`: `store.delete_user()`, then `gmail.revoke()`, then sign out.
  - `POST /tasks/poll-gmail`:
    - Verify the `Authorization: Bearer` OIDC token with `google.oauth2.id_token.verify_oauth2_token`. The audience is `<PUBLIC_URL>/tasks/poll-gmail`, and the email must be `scheduler-invoker@<PROJECT_ID>.iam.gserviceaccount.com`.
    - Anything else → **403 before any work**.
    - Then `runner.poll_all()`.
  - Add the `/google/*` and `/api/auto-import*` routes to the rate limits.
- [x] `templates/auto_import.html` and `static/auto_import.js`, following wireframes W2 and W3:
  - the steps: ① Gmail, ② YNAB, ③ YNAB plan, ④ "Map e-wallets to YNAB accounts"
  - one row per source with `email_query`: an account dropdown, "Don't import", that source's password inputs, and a Test button
  - an info box: emails arrive when you request a statement in the e-wallet app; settings are stored encrypted until you turn this off
  - the run log; "Turn off and delete"
- [x] A link to Auto-import in the main page header (hosted mode only).
- [x] Privacy page additions:
  - The Google API Services User Data Policy **Limited Use** statement.
  - What auto-import stores (encrypted tokens, mapping, password inputs) and for how long (until you turn it off).
  - That only emails matching the statement search are read, and no email content beyond the run log is stored.
  - How "Turn off and delete" works.
- [x] `deploy.yml`: add `GOOGLE_CLIENT_ID` to `--set-env-vars`. `ALLOWED_GOOGLE_EMAILS` is read from `app-config` instead (step A).
- [x] Tests:
  - `/tasks/poll-gmail` without a valid OIDC token → 403, and the runner isn't called
  - a non-allowlisted Google email can't turn the feature on
  - saving two sources with the same account → 400
  - delete removes the doc and calls revoke
  - the routes don't exist in local mode

### C. Scheduler job (after PR 5 is deployed)

- [ ] Let the scheduler call the service, then create the job:
  ```bash
  gcloud run services add-iam-policy-binding ph-statement-import --region=us-west1 \
    --member=serviceAccount:scheduler-invoker@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/run.invoker
  gcloud scheduler jobs create http poll-gmail --location=us-west1 \
    --schedule="*/15 * * * *" --time-zone="Asia/Manila" \
    --http-method=POST --uri="<PUBLIC_URL>/tasks/poll-gmail" \
    --oidc-service-account-email=scheduler-invoker@<PROJECT_ID>.iam.gserviceaccount.com \
    --oidc-token-audience="<PUBLIC_URL>/tasks/poll-gmail" \
    --attempt-deadline=60s
  ```
  The service allows unauthenticated access for the web page, so the route's own OIDC check is what protects it. The `run.invoker` binding keeps it working if you ever make the service private.

### D. Try it end to end

- [ ] On `<PUBLIC_URL>/auto-import`:
  1. Connect Gmail. Expect the "unverified app" warning once; continue.
  2. Connect YNAB.
  3. Pick a plan, map GCash to a **test** account, and enter the last 4 digits (the last name is prefilled).
  4. Test: should be OK.
  5. Turn on.
- [ ] Request a GCash statement email, or use an existing one newer than `last_checked`. Then run the job by hand:
  ```bash
  gcloud scheduler jobs run poll-gmail --location=us-west1
  ```
  The transactions should appear unapproved in YNAB, and the run should show on the page with status OK.
- [ ] Run it again: no duplicates, and the run log doesn't repeat.
- [ ] **Turn off and delete**:
  - the Firestore doc and its runs are gone (`gcloud firestore` console)
  - Google Account → Security → Third-party access no longer lists the app

## Tests

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- The end-to-end check (step D) passes with a real GCash email, including no duplicates and a clean delete.
- Scheduler runs every 15 minutes with status OK in its log, and the app logs one line per run with no personal data.
- After a week, Billing → Reports still shows **$0.00**. Firestore usage (console → Usage) is far below 50k reads a day.

## Docs to update

- README: an "Auto-import (hosted)" section.
- `docs/architecture.md`: the auto-import package and the poll flow.
- `docs/adding-an-e-wallet.md`: the auto-import fields.
- `docs/design/diagrams.html` U2, if anything changed.
- The privacy page.

## Risks and rollback

| Risk | What to do |
|---|---|
| Google refresh tokens stop working after 7 days | The consent screen is still in "Testing". Publish it "In production", then have users reconnect Gmail. |
| Wrong last name format (e.g. a space or a married name) | The Test button shows "wrong password" before saving. Edit the last name. |
| A user's YNAB refresh token is rotated but not saved | The next poll fails with a YNAB auth error. The runner always saves the new token right after refreshing; there's a test for it. |
| Too many Firestore reads | Check that `run_exists` uses a document `get`, not a query; poll less often. |
| Something goes wrong for everyone | Pause the job (`gcloud scheduler jobs pause poll-gmail --location=us-west1`), then fix and resume. Users' settings are kept. |
| A user wants their data gone | "Turn off and delete", or delete their `auto_import_users/{sub}` document by hand and revoke the token. |
