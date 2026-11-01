# Implementation plan: hosted ph-statement-import

This folder is the step-by-step plan for turning ph-statement-import (formerly statement-etl-ph) from a local tool into a hosted web app on Google Cloud, with optional Gmail auto-import. Each phase has its own file. Follow them in order, tick the checkboxes as you go, and set `Status: Done` at the top of a file when its phase is merged.

New here? Read [CONTRIBUTING.md](../../CONTRIBUTING.md) first (added in P0), then the phase file you're working on. Each file stands on its own. Just want to use the app? See [Getting started](../getting-started.md).

## Decisions

| Decision | Why |
|---|---|
| **The hosted app uses YNAB OAuth only.** Personal access tokens stay in the local version. | YNAB's API terms forbid asking for anyone else's access token. OAuth is the only option for apps that other people use. |
| **Family and friends first**, then YNAB's review. | A new YNAB OAuth app starts in Restricted Mode (up to 25 users). YNAB's review (2–4 weeks) lifts the limit. |
| **Google Cloud, free tier only: the bill must be $0.** | See the cost table in [P4](P4-go-live-on-gcp.md#cost-check). No Cloud KMS, no Cloud Build, no load balancer, no custom domain. |
| **Region `us-west1` (Oregon).** | Cloud Run's free outbound traffic (1 GB a month) applies only to North America. Singapore would cost a fraction of a cent a month, which is not $0. Of the low-CO2 North American regions, Oregon and Iowa (`us-central1`) are fastest from the Philippines (about 200 ms a request, measured Oct 2026); Oregon has the cleaner grid (98 vs 432 g CO₂/kWh, mostly hydro). |
| **Manual uploads are kept in memory only.** | Nothing about a manual upload is stored. The PDF and its password are gone after the upload or after 30 minutes idle. |
| **Gmail auto-import is opt-in, for invited users only.** | Only people who turn it on have encrypted tokens and PDF-password inputs stored (Firestore, encrypted in the app). |
| **Name: `ph-statement-import`** (display name "PH Statement Import for YNAB"). The Python package stays `statement_etl`. | `statement-etl-ph` is taken among your repos. Leaving YNAB out of the repo name leaves room for other budgeting apps. |
| **License: GNU GPL v3 (GPL-3.0-or-later).** | Anyone who distributes a copy, modified or not, must share its source, so the core stays open. Hosting a copy isn't distributing under GPLv3 (unlike AGPL), so companies can run it internally without legal pushback. Contributions come in under the same license (no CLA). |
| **Three parts: Source → Transaction → YNAB.** Sources return neutral `Transaction`s; YNAB formatting lives in `ynab_api/`. | Adding a wallet never touches YNAB code, and another budgeting app (Actual Budget, Lunch Money, Firefly III, CSV export, …) can later be added next to `ynab_api/`. There's no destination registry until a second one actually exists. |
| **Readability first.** | The repo is public and meant for contributors. One obvious place per job; refactors in separate PRs from behavior changes. |

## Phases

| Phase | File | Goal | Done when |
|---|---|---|---|
| P0 | [P0-repo-and-contributor-guide.md](P0-repo-and-contributor-guide.md) | Public repo a newcomer can contribute to | CI and lint pass; contributor docs in place; design files current |
| P1 | [P1-refactors.md](P1-refactors.md) | One import function, one registry per source, Blueprints | All tests green; CLI output unchanged |
| P2 | [P2-gotyme-parser.md](P2-gotyme-parser.md) | Import GoTyme statements by hand | A real GoTyme statement parses, checks out and uploads |
| P3 | [P3-hosted-mode.md](P3-hosted-mode.md) | App safe to run for other people | Hosted tests pass; Docker shows the OAuth-only page |
| P4 | [P4-go-live-on-gcp.md](P4-go-live-on-gcp.md) | Manual upload on Cloud Run, at $0 | You, then one family member, upload successfully |
| P5 | [P5-gmail-auto-import-gcash.md](P5-gmail-auto-import-gcash.md) | GCash statements import on their own | A real GCash email imports once; "Turn off and delete" works |
| P5b | [P5b-gmail-push.md](P5b-gmail-push.md) | Statements import seconds after the email arrives | A real GCash email imports within a minute; the 15-minute poll still runs |
| P6 | [P6-gotyme-auto-import.md](P6-gotyme-auto-import.md) | GoTyme in the auto-import mapping table | A real GoTyme email imports only its Everyday account PDF |
| P7 | [P7-public-release.md](P7-public-release.md) | Open to all YNAB users | YNAB review approved |
| P8 | [P8-transactions.md](P8-transactions.md) | Less hand work after an import: categories, references, transfers, several GCash accounts, live progress | Each item in its "Done when" checked on the hosted app |
| P9 | [P9-gotyme-goalsave.md](P9-gotyme-goalsave.md) | GoTyme GoalSave statements: 1–5 accounts, each to its own YNAB account | The sample parses into its accounts; a real email imports them all once |

## Order

```
P0 ──► P1 ──┬──► P2 ───────────────────────┐
            └──► P3 ──► P4 ──► P5 ──► P6 ◄─┘ ──► P7
                                 └──► P5b ──────► P8 ──► P9
```

- P1 comes first because P2, P3 and P5 build on its new modules.
- **P2 and P3 can be worked on at the same time.** They touch different files:
  - P2: `parsers/gotyme.py`, `sources.py` and the docs
  - P3: `web/*`, the `Dockerfile` and `requirements.txt`
- P6 needs both P2 (the parser) and P5 (auto-import).
- P5b (Gmail push) needs P5 only, and can go before or after P6.
- P8 needs P5b and P6 (auto-import for both wallets). It doesn't wait for P7.
- P9 needs P8 (several accounts per source, transfers).

## Design files

- [docs/design/gcp-architecture.drawio](../design/gcp-architecture.drawio): what runs where in Google Cloud.
- [docs/design/diagrams.html](../design/diagrams.html): how the `.py` files call each other, three use-case diagrams (public functions only), and the sequence diagrams.
- [docs/design/wireframes.html](../design/wireframes.html): the main page, the auto-import page (off and on), and the mobile view.

## Working on a phase

1. Branch from `main` using the branch name the phase file gives, e.g. `refactor/import-statement`.
2. Do that PR's steps, run the tests it lists, and open the PR with the Conventional Commit title it gives.
3. After merging, tick the boxes in the phase file in the same PR or a follow-up `docs:` PR.
