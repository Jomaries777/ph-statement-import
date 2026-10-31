# P0. Repo, contributor guide and design

Status: Done · Estimate: about 2 days, 4 PRs · Needs: nothing

## Goal

Put the project in a **public GitHub repo** that a developer who has never seen it can clone, set up, test and contribute to by following the docs alone. CI runs the tests and a linter on every PR. `main` is protected. The design files match the plan (no Cloud KMS, region `us-west1`, function diagrams at public-function level).

Every later phase relies on this: PRs, required checks, Conventional Commits, and the code standards in `CONTRIBUTING.md`.

## Before you start

- A GitHub account and the `gh` CLI, logged in (`gh auth status`).
- Python **3.13**, the version CI uses. Check with `python3.13 --version`.
- The project is a local git repo with one commit (`chore: initial commit`) on `main`. It hasn't been pushed, so it can still be amended.

## Steps

### Commit 1: first commit, straight to `main` (before branch protection exists)

Already done (Oct 31, 2026):
- [x] Deleted the editor swap file `.env.swp`.
- [x] Moved the real statement PDFs into `statements/` (gitignored).
- [x] `.gitignore` has `.env`, `*.swp`, `.claude/settings.local.json`, `*.pdf`, `statements/`, `.venv/`, `__pycache__/`, `.DS_Store` and `test-results/`.
- [x] Recreated `.venv` with Python 3.13.
- [x] `git init -b main` and the initial commit. Checked: no `.env`, PDF, `.swp`, `.DS_Store` or `settings.local.json` is tracked.
- [x] **Replaced `LICENSE`** with the unchanged GNU GPL v3 text (the first commit had the AGPL by mistake), amended into the initial commit, so the published history never contains the AGPL:
  ```bash
  curl -fsSL https://www.gnu.org/licenses/gpl-3.0.txt -o LICENSE
  git add LICENSE && git commit --amend --no-edit
  git show HEAD:LICENSE | head -2   # GNU GENERAL PUBLIC LICENSE / Version 3, 29 June 2007
  ```
  The license text has no copyright line. The notice goes in the README (below).
  - Why GPLv3: anyone who distributes a copy, modified or not, must share its source under the same license.
  - Why not AGPL: hosting a copy as a web service doesn't count as distributing under GPLv3, so companies can run it internally without legal pushback.

Still to do, folded into the same commit with `--amend` because nothing has been pushed yet:
- [x] Add a "License" section at the end of the README:
  ```markdown
  ## License

  Copyright (C) 2026 <YOUR NAME>

  ph-statement-import is free software: you can redistribute it and/or modify it under the terms
  of the GNU General Public License as published by the Free Software Foundation, either version 3
  of the License, or (at your option) any later version. See [LICENSE](LICENSE).
  ```
- [x] **Name the project `ph-statement-import`.** `statement-etl-ph` is already one of your GitHub repos, and leaving YNAB out of the name leaves room for other budgeting apps later.
  - README title: `# ph-statement-import`. First line: "Import Philippine e-wallet and bank statement PDFs into your budgeting app. It currently supports YNAB."
  - Hosted app display name, used in YNAB's and Google's OAuth screens: **"PH Statement Import for YNAB"**. YNAB allows "YNAB" in a name only after "for".
  - Keep the Python package name `statement_etl`. Renaming it would touch every import and test for no user-visible gain. `docs/architecture.md` explains the name.
  - Module docstrings: change the first line's "statement-etl-ph pipeline" to "ph-statement-import" (`grep -rn "statement-etl-ph" statement_etl`). Docstrings only; no code changes.
  - **Keep two stored names unchanged**, because users' machines and YNAB accounts already hold them, like source keys:
    - the keychain entry `SERVICE = "statement-etl-ph"` in `web/token_store.py`. Renaming it would make every saved token disappear. Add a comment saying so. The README's Keychain instructions keep that name.
    - the `ETLPH:` `import_id` prefix in `transform.py`. Changing it would duplicate every transaction already imported.
  - Renaming the local folder is optional; nothing depends on it.
- [x] Delete the icons nothing uses: `docs/icons/claude.png`, `gemini.svg`, `workos.svg`, `kms.svg` and `cloud-storage.png`. The first three are other companies' logos, which a public repo shouldn't carry without a reason.
- [x] Run the tests, then fold everything into the initial commit, including the files added since (`docs/getting-started.md`, `docs/design/tools/make_drawio.py`, `docs/icons/billing.svg`):
  ```bash
  python -m unittest discover tests
  git add -A
  git status --short
  ```
  Check that nothing listed is `.env`, a `.pdf`, under `statements/`, `.DS_Store` or `.claude/settings.local.json`. If something is, fix `.gitignore` and run `git add -A` again. Then:
  ```bash
  git commit --amend --no-edit
  ```
- [x] Push:
  ```bash
  gh repo create <you>/ph-statement-import --public --source . --push
  ```
  The README and `docs/getting-started.md` link to `github.com/<you>/ph-statement-import`; check the links work once it's pushed.
- [x] In GitHub → Settings:
  - **Code security:** turn on Dependabot alerts, secret scanning, push protection, and private vulnerability reporting.
  - **Actions → General:** set "Require approval for all outside collaborators" for workflows from forks.
  - **General → Pull Requests:** allow **squash merging** only, and turn on "Automatically delete head branches".
- [x] Protect `main` (Settings → Branches → Add rule for `main`):
  - require a pull request before merging
  - require the `test` status check
  - block force pushes and deletion

  You'll add the `lint` check in PR 3.

### PR 1: `chore/tooling`, "chore: add ruff config, pin dependencies and Python version"

- [x] Add `pyproject.toml`. It holds tool settings only; the app isn't packaged.
  ```toml
  [tool.ruff]
  line-length = 100
  target-version = "py313"

  [tool.ruff.lint]
  select = ["E", "F", "I", "B", "UP", "SIM"]
  ```
- [x] Add `ruff` to `requirements-dev.txt`, pinned to an exact version (`ruff==X.Y.Z`).
- [x] Pin exact versions in `requirements.txt` for every top-level package (`flask`, `pdfplumber`, `ynab`, `python-dotenv`, `keyring`):
  ```bash
  pip freeze | grep -iE '^(flask|pdfplumber|ynab|python-dotenv|keyring)=='
  ```
  Keep the comments at the top of each requirements file.
- [x] Add `.python-version` containing `3.13`.
- [x] Add `.github/dependabot.yml` with weekly updates for `pip` (at `/`) and `github-actions` (at `/`).
- [x] Run `python -m unittest discover tests`; it should pass unchanged.

### PR 2: `style/ruff-format`, "style: apply ruff format and safe fixes"

This PR only reformats. It contains no logic changes, so reviewers can skim it, and `git blame` can be told to skip it.

- [x] Reformat and apply the safe fixes:
  ```bash
  ruff format .
  ruff check --fix .
  ruff check .
  ```
  Fix anything `ruff check .` still reports **by hand only if the fix is purely mechanical**. Otherwise add a `# noqa: <rule>` with a reason, and note it in the PR.
- [x] Run `python -m unittest discover tests` and `REQUIRE_E2E=1 python -m unittest discover tests`; both should pass.

### PR 3: `ci/lint`, "ci: add lint job and ignore the format commit in blame"

- [x] Add a `lint` job to `.github/workflows/tests.yml`, next to the `test` job:
  ```yaml
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: python -m pip install ruff==X.Y.Z   # same version as requirements-dev.txt
      - run: ruff format --check .
      - run: ruff check .
  ```
- [x] Add `.git-blame-ignore-revs` containing the hash of PR 2's squash commit **on `main`** (`git log --oneline main`), with a comment line above it saying what it is.
- [x] After merging, add `lint` as a required check in the `main` branch rule.

### PR 4: `docs/contributor-guide`, "docs: add contributor guide, architecture overview and GitHub templates"

- [x] `CONTRIBUTING.md`, with these sections:
  1. **Set up in 5 commands:** clone, venv, install (`requirements.txt` and `requirements-dev.txt`), `cp .env.example .env`, run the tests. People who only want to use the app follow `docs/getting-started.md` instead; link it at the top.
  2. **Run the app and tests:** web app, CLI, `python -m unittest discover tests`, e2e with `REQUIRE_E2E=1`, `ruff format . && ruff check .`.
  3. **Code standards:**
     - Every module starts with a docstring saying what it does and how to run it.
     - Public functions have a docstring with a concrete example (`"1,234.50" -> Decimal("1234.50")`).
     - Comments explain *why*. Long functions use numbered step comments. Use `# ===` banners to group a module.
     - One job per function and one obvious place per job. Extract repeated logic rather than copying it. Split functions longer than ~50 lines or nested more than 3 levels.
     - Names say what something is. Spell words out; domain terms (YNAB, PDF, OAuth, import_id, milliunits) are fine.
     - Type hints on public functions and dataclasses in new or touched code.
     - Money is `Decimal`, never `float`. Inflow is positive, outflow negative. Convert to milliunits only in `ynab_api/format.py`.
     - Sources never import YNAB code: they return neutral `Transaction`s, and `ynab_api/` turns them into YNAB's format.
     - Never rename a source key (`gcash`, `gotyme`). It's part of every `import_id`, so renaming it creates duplicates in YNAB.
     - Never log or commit personal data: no tokens, passwords, PDF bytes or parsed rows in logs, and no real statements anywhere in the repo, issues or PRs. Tests use made-up rows (`tests/e2e/make_pdf.py`).
     - A parser raises `StatementError` (a `ValueError` subclass in `parsers/common.py`, added after P0) with a message the user can act on. Library code never calls `sys.exit`.
  4. **Branches, commits and PRs:**
     - Branches: `feat/…`, `fix/…`, `refactor/…`, `docs/…`, `test/…`.
     - Commits: Conventional Commits. Subject line under 72 characters and imperative; the body explains why.
     - Small PRs that do one thing (under ~400 changed lines). Refactors and behavior changes go in separate PRs.
     - Every PR adds tests for what it changes, passes `test` and `lint`, updates the docs it affects, and includes screenshots for page changes.
     - PRs are merged with squash.
  5. **Adding an e-wallet or bank:** a link to `docs/adding-an-e-wallet.md`.
  6. **Reporting security issues:** a link to `SECURITY.md`.
  7. **License of contributions:** "By opening a pull request, you agree that your contribution is licensed under GPL-3.0-or-later, the same as the project." No CLA.
  8. **New dependencies** must have a GPLv3-compatible license (MIT, BSD, Apache-2.0, MPL-2.0, LGPL or GPL). Today's dependencies all are.
- [x] `docs/architecture.md`: one page covering:
  - the three parts: **Source** (parsers: PDF rows → neutral `Transaction`) → **Transaction** (`transaction.py`) → **YNAB** (`ynab_api/`: format, upload, balance checks), plus the pipeline entry point, web routes and the auto-import runner (from P5)
  - "Adding a wallet or bank" means writing a Source. "Adding another budgeting app" means writing a new destination next to `ynab_api/` that takes the same `Transaction`s; until a second one exists, there's deliberately no destination registry
  - which module owns what (a table)
  - links to `docs/design/` and `docs/plan/`

  Keep it to what exists today, and update it in each phase.
- [x] `SECURITY.md`: report through GitHub's "Report a vulnerability" (private), never in a public issue. In scope: anything exposing tokens, PDFs, passwords or another user's data.
- [x] `CODE_OF_CONDUCT.md`: Contributor Covenant 2.1, with your contact method.
- [x] `.github/pull_request_template.md`:
  ```markdown
  ## What and why
  ## How I tested it
  - [ ] Tests added or updated
  - [ ] `ruff format . && ruff check .` pass
  - [ ] Docs updated (README, getting-started, CONTRIBUTING, architecture, adding-an-e-wallet, design) where affected
  - [ ] No real statement data, tokens or passwords in code, tests or screenshots
  - [ ] No source key renamed
  ```
- [x] `.github/ISSUE_TEMPLATE/`:
  - `bug_report.md`: steps, expected vs actual, and in bold at the top: "Remove names, account numbers and amounts before posting. Never attach a statement."
  - `new_source.md`: e-wallet or bank name, column headers, one **made-up** example row, the PDF password rule, and how the statement email arrives. Says "Don't attach the PDF."
  - `feature_request.md`
  - `config.yml` with `blank_issues_enabled: false`.
- [x] `.github/CODEOWNERS`: `* @<you>`.
- [x] README: shorten the existing "Contributing" section to a few lines linking `CONTRIBUTING.md` and `docs/plan/`. The test commands it lists today move into `CONTRIBUTING.md`.

### User guide: already written (Oct 31, 2026)

`docs/getting-started.md` takes someone who doesn't know Python from download to first upload, on macOS, Windows and Linux, with troubleshooting. It was checked on a fresh clone. The README's "Getting started" section links it, and `.env.example` now says every setting is optional for the web app. It's part of the first commit.
- [ ] When a later phase changes how the app is installed or run locally, or what the page asks for, update `docs/getting-started.md` in the same PR.

### Design files: already updated (Oct 31, 2026)

The design files were updated before P0, so they're part of the first commit. There's no separate PR.
- `docs/design/diagrams.html`:
  - D2 shows the post-P1 modules: blueprints, `transaction.py`, `ynab_api/format.py`, no `TRANSFORMERS`, no KMS.
  - Use cases U1 (manual upload), U2 (auto-import poll) and U3 (the `Source` contract), with public functions only.
  - Both sequence diagrams are updated.
- `docs/design/gcp-architecture.drawio`:
  - `us-west1` and free-tier notes
  - no Cloud KMS; the one `app-config` secret
  - GitHub Actions builds the image
  - the budget → Pub/Sub → `budget-guard` kill switch
- The drawio is generated by `docs/design/tools/make_drawio.py`. Edit the script and run it again instead of editing the `.drawio` by hand:
  ```bash
  python docs/design/tools/make_drawio.py docs/icons docs/design/gcp-architecture.drawio
  ```
- [ ] When a later phase changes a module or public function name, update `diagrams.html` in the same PR.

## Tests

- No new tests. Existing tests must pass after every PR:
  ```bash
  python -m unittest discover tests
  REQUIRE_E2E=1 python -m unittest discover tests
  ```
- After PR 3, CI must also pass `ruff format --check .` and `ruff check .`.

## Done when

- The repo is public; `main` requires a PR plus passing `test` and `lint` checks.
- A teammate can follow `CONTRIBUTING.md` on a fresh clone and get green tests without asking anything.
- Someone who isn't a developer can follow `docs/getting-started.md` and reach the page.
- GitHub shows the license as **GPL-3.0**.
- `git status` on a fresh clone shows no PDFs, `.env` or `.swp` files.

## Docs to update

README (title, License and Contributing sections), and the new `CONTRIBUTING.md`, `docs/architecture.md`, `SECURITY.md` and `docs/design/*`. `docs/getting-started.md` is already written.

## Risks and rollback

| Risk | What to do |
|---|---|
| A secret or real statement gets committed | **Don't just delete it in a new commit.** Rotate the secret immediately (new YNAB token or new secret key). For a statement, delete the repo or rewrite history before anyone clones it, then turn on push protection. |
| The format PR changes behavior | It must not. If tests fail after `ruff check --fix`, revert that file's fix and add a `# noqa` with a reason. |
| Branch protection blocks your own fix | Use a PR like everyone else. Admins can bypass in an emergency, but note why in the PR. |
