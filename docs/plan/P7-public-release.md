# P7. Public release (YNAB review)

Status: Not started · Estimate: a few hours of work, then 2–4 weeks of waiting · Needs: P4 stable; P5 and P6 optional

## Goal

Lift YNAB's Restricted Mode (25 users), so any YNAB user can use the hosted manual upload with **Connect with YNAB login**. Gmail auto-import stays **invite-only** (`ALLOWED_GOOGLE_EMAILS`). Opening it to the public would need Google's verification of the restricted `gmail.readonly` scope, including a paid security assessment. That's out of scope.

## Before you start

- The hosted app has run for at least a few weeks for you and your family, with no open bugs about data handling.
- Billing has shown **$0.00** every month.
- The privacy policy is current, with a "Last updated" date.

## Steps

### PR 1: `docs/public-release`, "docs: prepare for YNAB review"

- [ ] Re-read the privacy policy against YNAB's review checklist:
  - why data is used
  - how it's handled, stored, secured and retained
  - no third-party sharing without users knowing
  - how to request deletion
  - a "Last updated" date
- [ ] Check the app name and URL follow YNAB's naming rule: "YNAB" only after "for": "PH Statement Import for YNAB".
- [ ] README:
  - a short "Use it online" section with the URL
  - what's stored (nothing for manual uploads)
  - how to report a security issue (`SECURITY.md`)
- [ ] Look at the free-tier headroom before more users arrive (P4 cost table):
  - Cloud Run requests and vCPU-seconds (Cloud Run → Metrics)
  - outbound traffic, which must stay under 1 GB a month
  - rate limits

  If public use could push any of these near its limit, lower the limits or keep the app invite-only.

### Submit for review

- [ ] YNAB → Developer Settings → your OAuth application → request review / remove Restricted Mode, using YNAB's current form. Include:
  - the app URL
  - the privacy policy URL
  - the source code link (public repo)
  - a short description: "Imports GCash and GoTyme statement PDFs into YNAB as unapproved transactions; PDFs are processed in memory and never stored."
- [ ] Answer YNAB's questions. Each change goes in its own small PR.

## Tests

No new tests. All checks must be green on `main`:

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- YNAB approves the app, and a YNAB user outside your invite list can connect and upload.
- Billing → Reports still shows **$0.00** a month after opening up.

## Docs to update

README (public URL, status) and `docs/plan/README.md` (mark P7 done).

## Later: other budgeting apps (separate work)

P1 already made sources return neutral `Transaction`s, so supporting another app means adding a destination next to `ynab_api/`, without touching any source:

1. Pick one app and check its API first:
   - **Actual Budget, Lunch Money, Firefly III:** public APIs; they fit the same create-transactions, skip-duplicates and check-balance flow.
   - **CSV export:** works with apps that have no public API (e.g. Money Lover, Spendee). No OAuth, nothing stored.
   - **Google Sheets:** an API destination for DIY trackers.
2. Write `statement_etl/<app>_api/` (or `destinations/<app>/`): a `to_<app>(transaction)` formatter, an upload, and, if the app supports it, balance checks. Duplicate detection uses `Transaction.source` + `Transaction.key`, as YNAB's `import_id` does.
3. **Only then** add the shared parts that two destinations need:
   - a small `Destination` registry
   - a tracker picker on the page
   - per-tracker auto-import settings

   Generalize `pipeline.import_statement()` at the same time.
4. Each step gets its own phase file in `docs/plan/`, using the same template.

## Later: a double-click desktop app (idea, not planned yet)

Today the local version needs Python and a terminal (`docs/getting-started.md`). A packaged app would make it "download, double-click":
- PyInstaller builds a macOS `.dmg`, a Windows installer and a Linux AppImage. A `release.yml` workflow builds them on each `v*` tag and attaches them to the GitHub release, for $0.
- A launcher picks a free port on `127.0.0.1`, starts the app and opens the browser. The page gets a **Quit** button.
- Security: a random per-launch key in the URL, and a check that the `Host` header is `127.0.0.1:<port>`, so other websites can't call the local app.
- Unsigned apps show a warning on first launch (macOS: System Settings → Privacy & Security → **Open Anyway**; Windows: SmartScreen → **Run anyway**). Signing costs $99 a year on macOS; for Windows, the SignPath Foundation signs open-source projects for free.

If it's picked up, it gets its own phase file. It needs only P1 and can run alongside the hosted phases.

## Risks and rollback

| Risk | What to do |
|---|---|
| Public traffic nears a free-tier limit | Lower the rate limits, or go back to invite-only by re-enabling Restricted Mode in YNAB. The budget kill switch remains the backstop. |
| Abuse or scraping of the public URL | Rate limits plus max 1 instance cap it. If needed, require YNAB login before `/api/parse`. |
| The review asks for changes you don't want | Stay in Restricted Mode. Family use keeps working as is. |
