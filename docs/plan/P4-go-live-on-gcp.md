# P4. Go live on Google Cloud, at $0

Status: Not started · Estimate: about 1–2 days, plus waiting on family · Needs: P3

## Goal

The manual upload runs on Cloud Run at `https://ph-statement-import-<PROJECT_NUMBER>.us-west1.run.app`. Every merge to `main` deploys automatically. You first, then one family member, then the rest of the family (YNAB Restricted Mode allows up to 25 users) can use it with **Connect with YNAB login**.

**The monthly bill must stay at $0.** Every service below either is free or stays well inside Google's free tier. A budget alert and a billing kill switch guard against surprises.

## Before you start

- P3 merged: hosted mode, privacy pages, Dockerfile.
- The `gcloud` CLI, logged in (`gcloud auth login`).
- A Google Cloud **billing account**. Cloud Run needs billing turned on even at $0 usage. Don't rely on the $300 trial credit, which expires after 90 days.
- A YNAB account to own the OAuth app.
- Placeholders used below: `<PROJECT_ID>`, `<PROJECT_NUMBER>`, `<BILLING_ACCOUNT_ID>`, `<you>/ph-statement-import` (the GitHub repo) and `<CONTACT_EMAIL>`.
- Use a **new** project, not gcash-ynab-etl's. A project's Firestore `(default)` database location is permanent, and the free tier covers only that database (P5 needs it in `us-west1`).

## Cost check

Google's Free Tier figures as of Oct 2026. Re-check them on https://docs.cloud.google.com/free/docs/free-cloud-features before starting, since Google can change them with 30 days' notice.

| Service | Free per month | This app's expected use | Setting that keeps it free |
|---|---|---|---|
| Cloud Run | 2M requests; 180,000 vCPU-s; 360,000 GiB-s; **1 GB outbound from North America** | A few hundred requests a month in P4 (~3,000 once P5 polls); well under 1 GB outbound | `us-west1`, min instances 0, max 1, CPU only during requests, 60 s timeout |
| Artifact Registry | 0.5 GB storage | 2 images × ~200 MB | Cleanup policy keeps the 2 newest; Container Scanning **off** |
| Secret Manager | 6 active versions; 10,000 accesses | 1 version; one access per cold start | **One** JSON secret, `app-config`; destroy old versions |
| Cloud Logging | 50 GiB; free at default retention | MBs | Default 30-day retention; no sinks |
| Pub/Sub (budget messages) | 10 GiB | Bytes | — |
| Workload Identity Federation, IAM, budgets | Free | — | — |
| Builds | Not used | — | Images are built in **GitHub Actions** (free for public repos), so Cloud Build stays off |

**Never turn on:** Cloud KMS keys, Cloud Build, Container Scanning, Memorystore, Cloud SQL, load balancers or Cloud Armor, min instances ≥ 1, always-allocated CPU, a second Firestore database, log sinks, or a paid custom domain.

## Steps

### A. Project, budget and APIs (console or CLI)

- [ ] Create the project and link billing:
  ```bash
  gcloud projects create <PROJECT_ID> --name="ph-statement-import"
  gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
  gcloud config set project <PROJECT_ID>
  gcloud projects describe <PROJECT_ID> --format='value(projectNumber)'   # → <PROJECT_NUMBER>
  ```
- [ ] **Budget first, before anything can cost money.** Use the billing account's currency: $1, or ₱50 if the account bills in PHP.
  ```bash
  gcloud services enable pubsub.googleapis.com billingbudgets.googleapis.com
  gcloud pubsub topics create budget-alerts
  gcloud billing budgets create --billing-account=<BILLING_ACCOUNT_ID> \
    --display-name="ph-statement-import" --budget-amount=1USD \
    --filter-projects=projects/<PROJECT_ID> \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
    --notifications-rule-pubsub-topic=projects/<PROJECT_ID>/topics/budget-alerts
  ```
  Budget alert emails go to billing admins by default. The kill switch that reads this topic comes in step E.
- [ ] Enable only what P4 needs:
  ```bash
  gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
  gcloud services list --enabled | grep -Ei 'cloudbuild|containerscanning|cloudkms' \
    && echo "Disable these" || echo "OK: none enabled"
  ```
- [ ] Artifact Registry repo with a cleanup policy:
  ```bash
  gcloud artifacts repositories create app --repository-format=docker --location=us-west1
  cat > /tmp/cleanup.json <<'EOF'
  [
    {"name": "keep-2-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
    {"name": "delete-older",  "action": {"type": "Delete"}, "condition": {"tagState": "any"}}
  ]
  EOF
  gcloud artifacts repositories set-cleanup-policies app --location=us-west1 --policy=/tmp/cleanup.json
  ```
- [ ] The **one** secret, `app-config`. Write it from a temporary file, so the values don't end up in your shell history. The real `YNAB_CLIENT_SECRET` comes in step D.
  ```bash
  umask 077
  python3 -c 'import json,secrets; print(json.dumps({"FLASK_SECRET_KEY": secrets.token_hex(32), "YNAB_CLIENT_SECRET": "set-in-step-D"}))' > /tmp/app-config.json
  gcloud secrets create app-config --replication-policy=user-managed --locations=us-west1 --data-file=/tmp/app-config.json
  rm /tmp/app-config.json
  ```
- [ ] Runtime service account. It can read `app-config` and nothing else:
  ```bash
  gcloud iam service-accounts create app-runtime --display-name="ph-statement-import runtime"
  gcloud secrets add-iam-policy-binding app-config \
    --member=serviceAccount:app-runtime@<PROJECT_ID>.iam.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor
  ```
- [ ] Workload Identity Federation, so GitHub Actions on **`main` of your repo only** can deploy, with no JSON keys:
  ```bash
  gcloud iam workload-identity-pools create github --location=global
  gcloud iam workload-identity-pools providers create-oidc github --location=global \
    --workload-identity-pool=github --issuer-uri=https://token.actions.githubusercontent.com \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='<you>/ph-statement-import' && assertion.ref=='refs/heads/main'"
  gcloud iam service-accounts create deployer --display-name="GitHub deployer"
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member=serviceAccount:deployer@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/run.developer
  gcloud artifacts repositories add-iam-policy-binding app --location=us-west1 \
    --member=serviceAccount:deployer@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/artifactregistry.writer
  gcloud iam service-accounts add-iam-policy-binding app-runtime@<PROJECT_ID>.iam.gserviceaccount.com \
    --member=serviceAccount:deployer@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/iam.serviceAccountUser
  gcloud iam service-accounts add-iam-policy-binding deployer@<PROJECT_ID>.iam.gserviceaccount.com \
    --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/attribute.repository/<you>/ph-statement-import"
  ```
  Don't give the deployer any Secret Manager or owner roles.
- [ ] In GitHub → Settings → Environments, create a `production` environment with **you as a required reviewer**, limited to the `main` branch. Add these environment **variables** (not secrets; none of them is sensitive):
  - `GCP_PROJECT_ID`
  - `GCP_WIF_PROVIDER` = `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/github`
  - `GCP_DEPLOYER` = `deployer@<PROJECT_ID>.iam.gserviceaccount.com`
  - `PUBLIC_URL` = `https://ph-statement-import-<PROJECT_NUMBER>.us-west1.run.app`
  - `YNAB_CLIENT_ID` (filled in step D)

### B. PR 1: `ci/deploy`, "ci: build the image in GitHub Actions and deploy to Cloud Run"

- [ ] `.github/workflows/deploy.yml`:
  - Trigger: `on: push: branches: [main]` only. **Never** `pull_request` or `pull_request_target`, so forks can't reach GCP.
  - `permissions: { contents: read, id-token: write }`, and `environment: production`.
  - Steps:
    1. checkout
    2. run the unit tests (`python -m unittest discover tests`)
    3. `google-github-actions/auth@v2`, with `workload_identity_provider: ${{ vars.GCP_WIF_PROVIDER }}` and `service_account: ${{ vars.GCP_DEPLOYER }}`
    4. `google-github-actions/setup-gcloud@v2`
    5. `gcloud auth configure-docker us-west1-docker.pkg.dev --quiet`
    6. `docker build -t us-west1-docker.pkg.dev/$PROJECT/app/ph-statement-import:${{ github.sha }} .` and `docker push …`
    7. Deploy:
       ```bash
       gcloud run deploy ph-statement-import --region=us-west1 \
         --image=us-west1-docker.pkg.dev/$PROJECT/app/ph-statement-import:${{ github.sha }} \
         --service-account=app-runtime@$PROJECT.iam.gserviceaccount.com \
         --allow-unauthenticated --min-instances=0 --max-instances=1 --concurrency=8 \
         --cpu=1 --memory=512Mi --timeout=60 --session-affinity \
         --set-secrets=APP_CONFIG=app-config:latest \
         --set-env-vars=PUBLIC_URL=${{ vars.PUBLIC_URL }},YNAB_CLIENT_ID=${{ vars.YNAB_CLIENT_ID }}
       ```
  - Pin each action to a full version tag, and let Dependabot update them.
- [ ] README: add a "Deploy" section linking to this file.
- [ ] Merge, approve the `production` deployment in GitHub, and watch it finish.
- [ ] Check the URL matches `PUBLIC_URL`:
  ```bash
  gcloud run services describe ph-statement-import --region=us-west1 --format='value(status.url)'
  ```

### C. Check the live app

- [ ] Open the URL: the main page shows only **Connect with YNAB login**, and `/privacy` and `/terms` load.
- [ ] Check the headers:
  ```bash
  curl -sI https://ph-statement-import-<PROJECT_NUMBER>.us-west1.run.app/ | grep -Ei 'content-security|x-frame|referrer|nosniff|set-cookie'
  ```
  The `set-cookie` line, if any, must include `Secure; HttpOnly`.

### D. YNAB OAuth app (Restricted Mode)

- [ ] YNAB → Account Settings → Developer Settings → **New OAuth Application**:
  - **Name:** **"PH Statement Import for YNAB"**. YNAB allows "YNAB" in a name only after "for".
  - **Redirect URI:** `<PUBLIC_URL>/oauth/callback`
  - **Privacy policy URL:** `<PUBLIC_URL>/privacy`
  - **Scope:** the default read/write. The app creates transactions; request nothing more.
- [ ] Put the client secret into a new version of `app-config`, then destroy the old version to stay within 6 active versions:
  ```bash
  umask 077
  gcloud secrets versions access latest --secret=app-config > /tmp/app-config.json
  # edit /tmp/app-config.json: set YNAB_CLIENT_SECRET
  gcloud secrets versions add app-config --data-file=/tmp/app-config.json && rm /tmp/app-config.json
  gcloud secrets versions list app-config
  gcloud secrets versions destroy <OLD_VERSION> --secret=app-config
  ```
- [ ] Set the `YNAB_CLIENT_ID` environment variable in GitHub, then redeploy by re-running the last deploy workflow, so a new revision reads the new secret and client id.
- [ ] Update the privacy page's contact email to `<CONTACT_EMAIL>` in a small `docs:`/`feat:` PR, if it's still a placeholder.

### E. PR 2: `feat/budget-guard`, "feat: disable billing when the budget is exceeded"

The kill switch runs from **the same image** as the app, so it adds nothing to Artifact Registry. It runs as its own private Cloud Run service, under its own service account.

- [ ] `statement_etl/ops/budget_guard.py`, a small Flask app with one route, `POST /`, which receives the Pub/Sub push. Module docstring: what it does and why.
  1. Decode the budget message.
  2. If `costAmount <= budgetAmount`, return 204. Budgets send messages several times a day even when nothing is wrong, so most messages do nothing.
  3. Otherwise, unlink billing from the project through the Cloud Billing API (`projects/<PROJECT_ID>/billingInfo` with `billingAccountName: ""`), following Google's guide "Disable billing usage with notifications", and log one line.
  4. When `BUDGET_GUARD_DRY_RUN=1`, log what it would do instead of doing it.
- [ ] Tests: under-budget does nothing; over-budget calls the billing API (mocked); dry run never calls it; a bad message returns 400.
- [ ] Deploy it once by hand from the app's latest image. It needs no `deploy.yml` change unless you want it redeployed every time:
  ```bash
  gcloud iam service-accounts create budget-guard
  gcloud projects add-iam-policy-binding <PROJECT_ID> \
    --member=serviceAccount:budget-guard@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/billing.projectManager
  gcloud run deploy budget-guard --region=us-west1 \
    --image=us-west1-docker.pkg.dev/<PROJECT_ID>/app/ph-statement-import:<LATEST_SHA> \
    --service-account=budget-guard@<PROJECT_ID>.iam.gserviceaccount.com \
    --no-allow-unauthenticated --min-instances=0 --max-instances=1 \
    --command=gunicorn --args="-w,1,-b,0.0.0.0:8080,statement_etl.ops.budget_guard:app" \
    --set-env-vars=GCP_PROJECT_ID=<PROJECT_ID>,BUDGET_GUARD_DRY_RUN=1
  gcloud iam service-accounts create pubsub-push
  gcloud run services add-iam-policy-binding budget-guard --region=us-west1 \
    --member=serviceAccount:pubsub-push@<PROJECT_ID>.iam.gserviceaccount.com --role=roles/run.invoker
  gcloud pubsub subscriptions create budget-guard --topic=budget-alerts \
    --push-endpoint=$(gcloud run services describe budget-guard --region=us-west1 --format='value(status.url)') \
    --push-auth-service-account=pubsub-push@<PROJECT_ID>.iam.gserviceaccount.com
  ```
  If `roles/billing.projectManager` turns out not to be enough to unlink billing, use the role Google's guide names. Keep it to this one service account.
- [ ] Test in dry run by publishing a fake over-budget message:
  ```bash
  gcloud pubsub topics publish budget-alerts --message='{"costAmount": 2, "budgetAmount": 1}'
  gcloud logging read 'resource.labels.service_name="budget-guard"' --limit=5
  ```
  The log should show "would disable billing". Then turn dry run off:
  ```bash
  gcloud run services update budget-guard --region=us-west1 --remove-env-vars=BUDGET_GUARD_DRY_RUN
  ```
  Don't publish the fake message again after that.

### F. Use it, then invite family

- [ ] With your own YNAB account, through OAuth:
  1. Upload a real GCash statement to a **test** account. The transactions should be created unapproved.
  2. Upload it again. It should create 0 and skip everything.
- [ ] Check the logs hold no secrets or statement text:
  ```bash
  gcloud logging read 'resource.labels.service_name="ph-statement-import"' --limit=200 --format='value(textPayload,jsonPayload)'
  ```
  Look for passwords, tokens, payee names or amounts. There should be none.
- [ ] Invite one family member in the YNAB OAuth app (Restricted Mode → add their YNAB email). Watch them do one upload.
- [ ] Then invite the rest of the family (up to 25 in total).

## Tests

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

New: the `budget_guard` tests (step E). `deploy.yml` runs the unit tests before every deploy.

## Done when

- A merge to `main` deploys automatically after your approval.
- The live app:
  - shows the OAuth-only page with the security headers
  - uploads a real statement through OAuth with no duplicates on re-upload
  - has logs with no secrets or statement content
- One family member uploaded successfully.
- The kill switch passed its dry run and is live.
- **After 2–3 days:** Billing → Reports for the project, grouped by SKU, shows **$0.00** total. Artifact Registry holds 2 or fewer image versions. Secret Manager has 1 active version.

## Docs to update

- README: the "Deploy" and "Hosted version" sections, with the live URL if you want it public.
- `docs/architecture.md`: deployment, the two Cloud Run services, and the secrets.
- `docs/design/gcp-architecture.drawio`, if anything differs from the plan.

## Risks and rollback

| Risk | What to do |
|---|---|
| A deploy breaks the app | Route traffic back to the previous revision: `gcloud run revisions list --service=ph-statement-import --region=us-west1`, then `gcloud run services update-traffic ph-statement-import --region=us-west1 --to-revisions=<PREVIOUS>=100`. Revert the PR, and the next deploy restores normal traffic. |
| A non-zero charge appears | Find the SKU in Billing → Reports and turn off what caused it, using the "Never turn on" list. The kill switch is the backstop; budget data lags by a few hours. |
| The kill switch fires | Billing is unlinked and the app stops. Find the cause first, then re-link billing: `gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>`. |
| Images go over 0.5 GB | Check the cleanup policy ran (`gcloud artifacts docker images list us-west1-docker.pkg.dev/<PROJECT_ID>/app`), and keep the image slim (P3). |
| Someone abuses the public URL | Max 1 instance, a 60 s timeout and rate limits cap the damage; the kill switch caps the cost. |
| The OAuth redirect fails | The redirect URI in YNAB must match `<PUBLIC_URL>/oauth/callback` exactly, including `https`, with no trailing slash. |
