# Contributing to ph-statement-import

Want to use the app rather than modify the code? Follow [Getting started](docs/getting-started.md) instead.

For the multi-phase roadmap, see [docs/plan/README.md](docs/plan/README.md). For how the modules fit together, see [docs/architecture.md](docs/architecture.md).

## 1. Set up in 5 commands

Requires **Python 3.13**:

```bash
git clone https://github.com/Jomaries777/ph-statement-import.git && cd ph-statement-import
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m unittest discover tests
```

## 2. Run the app and tests

**Web app (local):**

```bash
python -m statement_etl.web
```

**Command line:**

```bash
python -m statement_etl.ynab_api.accounts
python -m statement_etl.pipeline statement.pdf --dry-run
python -m statement_etl.pipeline statement.pdf
python -m statement_etl.pipeline statement.pdf --source gcash --dry-run
```

**Unit tests:**

```bash
python -m unittest discover tests
```

The tests never reach YNAB or your OS secure store, and they don't need a real statement: the PDF, YNAB and the keychain are faked, and any unmocked YNAB call fails the test.

**End-to-end browser tests (Playwright):**

```bash
python -m playwright install chromium
REQUIRE_E2E=1 python -m unittest discover tests
```

**Formatter and linter (Ruff):**

```bash
ruff format . && ruff check .
```

## 3. Code standards

- **Module docstrings:** Every module starts with a docstring saying what it does and how to run it.
- **Function docstrings:** Public functions have a docstring with a concrete example (e.g. `"1,234.50" -> Decimal("1234.50")`).
- **Comments:** Comments explain *why*. Long functions use numbered step comments. Use `# ===` banners to group a module.
- **Single responsibility:** One job per function and one obvious place per job. Extract repeated logic rather than copying it. Split functions longer than ~50 lines or nested more than 3 levels.
- **Naming:** Names say what something is. Spell words out; domain terms (YNAB, PDF, OAuth, `import_id`, milliunits) are fine.
- **Type hints:** Add type hints on public functions and dataclasses in new or touched code.
- **Money:** Money is `Decimal`, never `float`. Inflow is positive, outflow negative. Convert to milliunits only in `ynab_api/format.py` (currently `transform.py` until P1).
- **Boundary between sources and YNAB:** Sources never import YNAB code: they return neutral `Transaction`s, and `ynab_api/` turns them into YNAB's format.
- **Source keys are permanent:** Never rename a source key (`gcash`, `gotyme`). It's part of every `import_id`, so renaming it creates duplicates in YNAB.
- **No personal data:** Never log or commit personal data: no tokens, passwords, PDF bytes or parsed rows in logs, and no real statements anywhere in the repo, issues or PRs. Tests use made-up rows (`tests/e2e/make_pdf.py`). Keep real PDFs in `statements/` (gitignored).
- **Errors:** A parser raises `ValueError` with a message the user can act on. Library code never calls `sys.exit`.

## 4. Branches, commits and PRs

- **Branches:** `feat/…`, `fix/…`, `refactor/…`, `docs/…`, `test/…`, `chore/…`, `ci/…`, `style/…`.
- **Commits:** Follow [Conventional Commits](https://www.conventionalcommits.org/). Keep the subject line under 72 characters and imperative; use the body to explain *why*.
- **Small PRs:** Keep PRs focused on one thing (under ~400 changed lines). Put refactors and behavior changes in separate PRs.
- **Checks and docs:** Every PR adds tests for what it changes, passes `test` and `lint`, updates the docs it affects, and includes screenshots for page changes.
- **Merging:** PRs are merged with squash.

## 5. Adding an e-wallet or bank

Follow the step-by-step walkthrough in [Adding an e-wallet or bank](docs/adding-an-e-wallet.md).

## 6. Reporting security issues

Never report security issues in a public issue. Follow [SECURITY.md](SECURITY.md) to report privately via GitHub's vulnerability reporting.

## 7. License of contributions

By opening a pull request, you agree that your contribution is licensed under GPL-3.0-or-later, the same as the project. There is no separate CLA.

## 8. New dependencies

Any new dependency must have a GPLv3-compatible license (MIT, BSD, Apache-2.0, MPL-2.0, LGPL or GPL). All current dependencies do.
