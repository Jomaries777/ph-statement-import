# P6. GoTyme auto-import

Status: Not started · Estimate: about 1 day, 1 PR · Needs: P2 (GoTyme parser) and P5 (auto-import)

## Goal

GoTyme shows up as a row in the `/auto-import` mapping table, so a GoTyme statement email is imported to its own YNAB account automatically, just like GCash. Because P5 built the table from `Source` fields, this phase is **only new `Source` settings plus tests**. No page or runner changes should be needed. If some are, that's a sign the P5 design leaked GCash-specific code, so fix that instead of adding a special case.

## What the GoTyme email looks like

- **From:** `no-reply@gotyme.com.ph`. **Subject:** "GoTyme Bank Statement of Account as of DD Mon YYYY".
- **Two PDF attachments in one email:**
  - `GoTymeStatementForEverydayDepositAccount_YYYYMMDD_HHMMSS.pdf`: the Everyday account, which is **this source (`gotyme`)**.
  - `GoTyme_BankStatement_SavingsAccounts_YYYYMMDD_HHMMSS.pdf`: savings (GoalSave). **Not in scope.** It gets its own source (`gotyme_goalsave`) and rows in the mapping table in [P9](P9-gotyme-goalsave.md).
- **Password:** your **date of birth as `MMDDYYYY`**, e.g. 31 Jan 1990 → `01311990`. The email doesn't contain it, so the user enters the date once.
  - Google could prefill it, but only with the sensitive `user.birthday.read` scope, and only if the user saved a birthday in Google. Not worth an extra consent screen.
- The statement shows a "Request Date", so like GCash it probably arrives only when requested in the GoTyme app. Confirm with a second email and update this file.

## Before you start

- P2 merged: `parsers/gotyme.py` and the `gotyme` Source exist; manual upload works.
- P5 live: auto-import works for GCash.
- A real GoTyme statement email in your Gmail. Request one in the GoTyme app.

## Steps

### PR 1: `feat/gotyme-auto-import`, "feat: import GoTyme statement emails automatically"

- [ ] `parsers/gotyme.py`: add `build_password(inputs)`, with a docstring example:
  ```python
  def build_password(inputs):
      """GoTyme PDF password: date of birth as MMDDYYYY.

      {"birth_date": "1990-01-31"} -> "01311990"
      """
      return date.fromisoformat(inputs["birth_date"]).strftime("%m%d%Y")
  ```
- [ ] `sources.py`, on the `gotyme` entry:
  ```python
  email_query = (
      'from:no-reply@gotyme.com.ph subject:"GoTyme Bank Statement of Account" has:attachment filename:pdf',
  )
  attachment_pattern = (r"^GoTymeStatementForEverydayDepositAccount_\d{8}_\d{6}\.pdf$",)
  password_inputs = (("birth_date",),)
  build_password = (gotyme.build_password,)
  ```
- [ ] `static/auto_import.js` and the template: an input named `birth_date` shows as a **date field** labelled "Date of birth", with the hint "GoTyme password = date of birth as MMDDYYYY".
  - If P5 renders inputs generically by name, add `birth_date` to the input-type map; don't add a GoTyme special case.
  - The GoTyme Savings row from the wireframe ("Coming soon", greyed out) is **not** added until `gotyme_goalsave` exists ([P9](P9-gotyme-goalsave.md)).
- [ ] `docs/adding-an-e-wallet.md`: GoTyme's email settings as the second auto-import example, including the two-PDF email and `attachment_pattern`.
- [ ] Privacy page: list the date of birth among the stored, encrypted password inputs.

## Tests

- `build_password({"birth_date": "1990-01-31"}) == "01311990"`
- In `tests/test_autoimport.py`, with a fake email holding **both** made-up PDFs:
  - for `gotyme`, only the `GoTymeStatementForEverydayDepositAccount_…` attachment is downloaded and parsed
  - the savings PDF is ignored
- GCash and GoTyme mapped to two different accounts: each email goes to its own account
- GCash and GoTyme mapped to the **same** account: saving is rejected (P5's rule, now with a real second source)
- The mapping table lists a GoTyme row with a `birth_date` input

```bash
python -m unittest discover tests
REQUIRE_E2E=1 python -m unittest discover tests
ruff format --check . && ruff check .
```

## Done when

- On `/auto-import`, the GoTyme row shows a date-of-birth field. Test on the newest GoTyme email returns OK.
- A real GoTyme email, after `gcloud scheduler jobs run poll-gmail --location=us-west1`:
  - only the Everyday account's transactions are imported, unapproved, to the mapped GoTyme account
  - the run log shows "GoTyme Everyday → <account>", OK
  - running again creates no duplicates
- Billing → Reports still shows **$0.00**.

## Docs to update

README (GoTyme auto-import), `docs/adding-an-e-wallet.md`, and the privacy page. Update this file with the confirmed email cadence.

## Risks and rollback

| Risk | What to do |
|---|---|
| GoTyme renames the attachment | The run shows "no attachment". Update `attachment_pattern` and add a test with the new name. |
| A date-of-birth format mix-up (DDMM vs MMDD) | The Test button catches it before saving; there's also a unit test for `build_password`. |
| Users map GoTyme Savings PDFs by mistake | They can't: the pattern matches only the Everyday file name. |

Rollback: revert the PR. GoTyme disappears from the mapping table, and existing GoTyme mappings are ignored because the source no longer has an `email_query`.

## Later (separate work, not this phase)

- `gotyme_goalsave`: a parser and source for the GoalSave statement. Planned in [P9](P9-gotyme-goalsave.md).
- With both GoTyme accounts mapped, "Transfer to/from GoalSave" could become a linked YNAB transfer between the two accounts, instead of an outflow plus an inflow.
