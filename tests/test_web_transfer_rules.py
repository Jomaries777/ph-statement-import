"""
Tests for web/transfer_rules.py and its page (/transfer-rules), in both modes:
locally the rules go in the settings file (local_settings.py); hosted, on the
signed-in Google user's record. Either way a save is checked against the
plan's open YNAB accounts.

Run from the project folder:  python -m unittest discover tests
Firestore is replaced with FakeStore (tests/fakes.py), and YNAB's account list
with a mock. All data here is made up.
"""

import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from statement_etl import local_settings
from statement_etl.web import app as web
from statement_etl.web import transfer_rules, upload, ynab_auth
from tests.fakes import ACCOUNT_ID, PLAN_ID
from tests.hosted import HOSTED_ENV, hosted_mode
from tests.test_parsers_gotyme_goalsave import TWO_ACCOUNT_ROWS
from tests.test_web_app import ROWS, TARGET, TRANSACTIONS, WebTest, starting_check
from tests.test_web_auto_import import (
    OTHER_ACCOUNT_ID,
    SUB,
    UNINVITED_EMAIL,
    AutoImportWebTest,
)

# The plan's open accounts, as ynab_api.accounts.get_accounts() returns them
PLAN_ACCOUNTS = [
    {"id": ACCOUNT_ID, "name": "GCash - 1161", "type": "cash", "balance": 0},
    {"id": OTHER_ACCOUNT_ID, "name": "GoTyme", "type": "checking", "balance": 0},
]
# A rule as the page sends it ...
SENT = {
    "account_1_id": ACCOUNT_ID,
    "description_1": " Transfer  to 0917123",
    "account_2_id": OTHER_ACCOUNT_ID,
    "description_2": "InstaPay from GCash",
}
# ... and as it's saved: tidied, with the plan's account names
SAVED = {
    "account_1_id": ACCOUNT_ID,
    "account_1_name": "GCash - 1161",
    "description_1": "Transfer to 0917123",
    "account_2_id": OTHER_ACCOUNT_ID,
    "account_2_name": "GoTyme",
    "description_2": "InstaPay from GCash",
}
# ROWS[2] in tests/test_web_app.py is "Payment to Test Shop"
PAYMENT_RULE = {**SAVED, "description_1": "payment to TEST"}

# GoalSave No. 22222's rows: "Earned interest", "Transfer to Deposit account"
GOALSAVE_ROWS = TWO_ACCOUNT_ROWS[1:]
# A GoTyme Everyday statement: "Transfer to GoalSave", then a card payment
EVERYDAY_ROWS = [
    {
        "Date": "18 Oct 2026",
        "Details": "Transfer to GoalSave",
        "Credits": "0.00",
        "Debits": "2500.00",
        "Running Balance": "1000.00",
    },
    {
        "Date": "19 Oct 2026",
        "Details": "Card payment at SHOPEE PH",
        "Credits": "0.00",
        "Debits": "249.00",
        "Running Balance": "751.00",
    },
]
UNLINKED = (
    "1 GoalSave row imported without a transfer. "
    "Import the GoTyme GoalSave statement first, so its transfers link up."
)


def uploaded(**more):
    """What import_statement() returns, as much as /api/upload reads."""
    return SimpleNamespace(
        created=[],
        skipped=0,
        ending_check=None,
        ending_error=None,
        new_account=None,
        transfers=0,
        **more,
    )


def page_data(html: bytes, name: str):
    """A data-* JSON value the page's script reads, e.g. page_data(html, "rules")."""
    match = re.search(rf"data-{name}='([^']*)'", html.decode())
    return json.loads(match.group(1))


# =============================================================================
# LOCAL MODE - the settings file
# =============================================================================


class LocalSettingsWebTest(WebTest):
    """Local mode, with the settings file in a temporary folder."""

    def setUp(self):
        super().setUp()
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "settings.json"
        self.patch_dict(os.environ, {"LOCAL_SETTINGS_PATH": str(self.path)})


class LocalTransferRulesTest(LocalSettingsWebTest):
    def setUp(self):
        super().setUp()
        self.get_accounts = self.patch(transfer_rules, "get_accounts", return_value=PLAN_ACCOUNTS)

    def save(self, **body):
        return self.client.post("/api/transfer-rules", json=body)

    def test_page_links_and_has_no_inline_code(self):
        self.assertIn(b'href="/transfer-rules"', self.client.get("/").data)
        page = self.client.get("/transfer-rules")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'<script src="/static/transfer_rules.js">', page.data)
        self.assertNotIn(b"<script>", page.data)
        self.assertNotIn(b"style=", page.data)
        self.assertIn(str(self.path).encode(), page.data)
        for column in (b"Bank #1", b"Bank #1 description", b"Bank #2", b"Bank #2 description"):
            self.assertIn(b"<th>" + column + b"</th>", page.data)
        with self.client.get("/static/transfer_rules.js") as script:
            self.assertEqual(script.status_code, 200)

    def test_save_writes_the_file_and_the_page_shows_it(self):
        self.connected()
        response = self.save(plan_id=PLAN_ID, rules=[SENT])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json(), {"saved": True, "rules": 1})
        self.get_accounts.assert_called_once_with(PLAN_ID, "test-token")
        self.assertEqual(
            json.loads(self.path.read_text()),
            {"transfer_plan_id": PLAN_ID, "transfer_pairs": [SAVED]},
        )

        page = self.client.get("/transfer-rules").data
        self.assertEqual(page_data(page, "rules"), [SAVED])
        self.assertIn(f'data-plan-id="{PLAN_ID}"'.encode(), page)

    def test_banks_must_be_in_the_plan(self):
        self.connected()
        response = self.save(plan_id=PLAN_ID, rules=[{**SENT, "account_2_id": "not-in-plan"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Rule 1: choose both banks.")
        self.assertFalse(self.path.exists())

    def test_rules_need_a_plan_and_ynab(self):
        response = self.save(rules=[SENT])
        self.assertEqual(response.get_json()["error"], "Choose a YNAB plan.")
        response = self.save(plan_id=PLAN_ID, rules=[SENT])  # not connected
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.path.exists())

    def test_no_rules_saves_without_ynab(self):
        response = self.save(rules=[])
        self.assertEqual(response.status_code, 200)
        self.get_accounts.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["transfer_pairs"], [])

    def test_not_json(self):
        response = self.client.post("/api/transfer-rules", data="rules")
        self.assertEqual(response.status_code, 400)

    def test_a_broken_file_is_shown_not_a_crash(self):
        self.path.write_text("{not json")
        page = self.client.get("/transfer-rules")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="settings-error"', page.data)


# =============================================================================
# HOSTED MODE - the Google user's record
# =============================================================================


class HostedRulesWebTest(AutoImportWebTest):
    """Hosted mode with auto-import; FakeStore for Firestore, and no settings file."""

    def setUp(self):
        super().setUp()
        self.patch(transfer_rules, "store", new=self.store)
        self.patch(transfer_rules, "get_accounts", return_value=PLAN_ACCOUNTS)
        no_file = AssertionError("hosted mode used the local settings file")
        self.patch(local_settings, "load_settings", side_effect=no_file)
        self.patch(local_settings, "save_settings", side_effect=no_file)

    def connect_ynab(self, **state):
        self.set_ynab_state(
            access_token="test-token",
            token_kind="oauth",
            expires_at=time.time() + 3600,
            **state,
        )


class HostedTransferRulesTest(HostedRulesWebTest):
    def test_signed_out_page_asks_to_sign_in(self):
        page = self.client.get("/transfer-rules")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="card-sign-in"', page.data)
        self.assertNotIn(b'id="rules-table"', page.data)
        response = self.client.post("/api/transfer-rules", json={"rules": [SENT]})
        self.assertEqual(response.status_code, 401)

    def test_uninvited_account_is_refused(self):
        self.sign_in_google(email=UNINVITED_EMAIL)
        response = self.client.post("/api/transfer-rules", json={"rules": [SENT]})
        self.assertEqual(response.status_code, 403)

    def test_page_needs_no_auto_import_mappings(self):
        # Signed in with Google only: no Gmail, no mappings (what disabled "+ Add a rule")
        self.sign_in_google()
        page = self.client.get("/transfer-rules").data
        self.assertIn(b'id="rules-table"', page)
        self.assertIn(b'id="add-rule-btn"', page)
        self.assertIn(b'data-has-saved-ynab="false"', page)
        self.assertEqual(page_data(page, "rules"), [])
        self.assertIn(b'id="transfer-rules-link"', self.client.get("/auto-import").data)

    def test_page_shows_the_saved_rules_and_plan(self):
        self.sign_in_google(
            enabled=True,
            plan_id="auto-import-plan",
            transfer_pairs=[SAVED],
            ynab_refresh_token="ynab-refresh",
        )
        page = self.client.get("/transfer-rules").data
        self.assertEqual(page_data(page, "rules"), [SAVED])
        self.assertIn(b'data-plan-id="auto-import-plan"', page)  # no transfer plan saved yet
        self.assertIn(b'data-has-saved-ynab="true"', page)

    def test_save_puts_the_rules_on_the_user(self):
        self.sign_in_google(enabled=True)
        self.connect_ynab()
        response = self.client.post(
            "/api/transfer-rules", json={"plan_id": PLAN_ID, "rules": [SENT]}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        user = self.store.get_user(SUB)
        self.assertEqual((user["transfer_pairs"], user["transfer_plan_id"]), ([SAVED], PLAN_ID))
        self.assertIn(f'data-plan-id="{PLAN_ID}"'.encode(), self.client.get("/transfer-rules").data)

    def test_a_refused_rule_saves_nothing(self):
        self.sign_in_google()
        self.connect_ynab()
        response = self.client.post(
            "/api/transfer-rules",
            json={"plan_id": PLAN_ID, "rules": [{**SENT, "account_2_id": ACCOUNT_ID}]},
        )
        self.assertEqual(response.get_json()["error"], "Rule 1: Bank #1 and Bank #2 must differ.")
        self.assertNotIn("transfer_pairs", self.store.get_user(SUB))


class HostedWithoutGoogleTest(unittest.TestCase):
    def test_no_rules_page_without_google_sign_in(self):
        with hosted_mode({**HOSTED_ENV, "GOOGLE_CLIENT_ID": ""}):
            client = web.app.test_client()
            self.assertNotIn(b"/transfer-rules", client.get("/").data)
            self.assertEqual(client.get("/transfer-rules").status_code, 404)
            self.assertEqual(client.post("/api/transfer-rules", json={}).status_code, 404)


class YnabLoginReturnsHereTest(unittest.TestCase):
    def test_the_rules_page_is_a_page_to_return_to(self):
        self.assertEqual(ynab_auth.NEXT_PAGES["/transfer-rules"], "/transfer-rules")


# =============================================================================
# UPLOAD - the manual upload uses the saved rules
# =============================================================================


class LocalUploadTransfersTest(LocalSettingsWebTest):
    """Locally, the upload reads the rules from the settings file."""

    def setUp(self):
        super().setUp()
        local_settings.save_settings(transfer_pairs=[PAYMENT_RULE])
        self.parsed()

    def test_the_starting_check_marks_the_matching_rows(self):
        check = starting_check()
        with mock.patch.object(upload, "starting_balance_check", return_value=check) as start:
            response = self.client.post("/api/starting-balance", json=TARGET)
        self.assertEqual(response.get_json()["transfers"], [{"row": 2, "to": "GoTyme"}])
        self.assertEqual(start.call_args.kwargs["transfers"], {2: OTHER_ACCOUNT_ID})

    def test_the_upload_sends_them_as_transfers(self):
        imported = SimpleNamespace(
            created=[],
            skipped=0,
            ending_check=None,
            ending_error=None,
            new_account=None,
            transfers=1,
        )
        with mock.patch.object(upload, "import_statement", return_value=imported) as send:
            response = self.client.post("/api/upload", json=TARGET)
        self.assertEqual(send.call_args.kwargs["transfers"], {2: OTHER_ACCOUNT_ID})
        self.assertEqual(response.get_json()["transfers"], 1)

    def test_another_account_has_no_transfers(self):
        check = starting_check()
        with mock.patch.object(upload, "starting_balance_check", return_value=check):
            response = self.client.post(
                "/api/starting-balance", json={**TARGET, "account_id": "not-in-a-rule"}
            )
        self.assertEqual(response.get_json()["transfers"], [])

    def test_a_broken_settings_file_uploads_without_transfers(self):
        self.path.write_text("{not json")
        check = starting_check()
        with mock.patch.object(upload, "starting_balance_check", return_value=check) as start:
            response = self.client.post("/api/starting-balance", json=TARGET)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.kwargs["transfers"], {})


class LocalUploadOwnTransfersTest(LocalSettingsWebTest):
    """GoalSave and GoTyme Everyday moves need no rule (Source.own_transfers)."""

    def start(self, **body):
        check = {**starting_check(), "unlinked_transfers": body.pop("unlinked", [])}
        with mock.patch.object(upload, "starting_balance_check", return_value=check) as start:
            response = self.client.post("/api/starting-balance", json={**TARGET, **body})
        return start.call_args.kwargs["transfers"], response.get_json()

    def test_goalsave_rows_go_to_the_picked_account(self):
        self.connected(source="gotyme_goalsave", rows=GOALSAVE_ROWS, transactions=TRANSACTIONS)
        transfers, shown = self.start(own_transfer_account_id=OTHER_ACCOUNT_ID)
        self.assertEqual(transfers, {1: OTHER_ACCOUNT_ID})
        self.assertEqual(shown["transfers"], [{"row": 1, "to": "GoTyme"}])

    def test_goalsave_rows_without_a_pick_are_ordinary(self):
        self.connected(source="gotyme_goalsave", rows=GOALSAVE_ROWS, transactions=TRANSACTIONS)
        self.assertEqual(self.start()[0], {})

    def test_everyday_rows_link_to_any_accounts_transfer(self):
        self.connected(source="gotyme", rows=EVERYDAY_ROWS, transactions=TRANSACTIONS)
        transfers, shown = self.start()
        self.assertEqual(transfers, {0: ()})  # nothing mapped locally: any account
        self.assertEqual(shown["transfers"], [{"row": 0, "to": "GoTyme GoalSave"}])
        self.assertIsNone(shown["unlinked_message"])

    def test_everyday_rows_with_no_transfer_yet(self):
        self.connected(source="gotyme", rows=EVERYDAY_ROWS, transactions=TRANSACTIONS)
        _, shown = self.start(unlinked=[0])
        self.assertEqual(shown["transfers"], [])  # uploaded as an ordinary row
        self.assertEqual(shown["unlinked_message"], UNLINKED)

        with mock.patch.object(
            upload, "import_statement", return_value=uploaded(unlinked_transfers=[0])
        ):
            response = self.client.post("/api/upload", json=TARGET)
        self.assertEqual(response.get_json()["unlinked_message"], UNLINKED)

    def test_the_upload_sends_the_pick(self):
        self.connected(source="gotyme_goalsave", rows=GOALSAVE_ROWS, transactions=TRANSACTIONS)
        with mock.patch.object(upload, "import_statement", return_value=uploaded()) as send:
            self.client.post("/api/upload", json={**TARGET, "own_transfer_account_id": "acc-x"})
        self.assertEqual(send.call_args.kwargs["transfers"], {1: "acc-x"})

    def test_the_parsed_goalsave_statement_asks_where_they_go(self):
        self.connected(
            source="gotyme_goalsave",
            filename="savings.pdf",
            accounts={"22222": (GOALSAVE_ROWS, TRANSACTIONS)},
        )
        response = self.client.post("/api/statement-account", json={"account": "22222"})
        self.assertEqual(
            response.get_json()["own_transfers"],
            {"description": "Deposit account", "other_name": "GoTyme", "account_id": ""},
        )


class HostedUploadTransfersTest(HostedRulesWebTest):
    """Hosted, the upload reads the signed-in Google user's rules."""

    def test_signed_in_user_rules_apply(self):
        self.sign_in_google(transfer_pairs=[PAYMENT_RULE])
        self.connect_ynab(source="gcash", rows=ROWS, transactions=TRANSACTIONS)
        check = starting_check()
        with mock.patch.object(upload, "starting_balance_check", return_value=check):
            response = self.client.post("/api/starting-balance", json=TARGET)
        self.assertEqual(response.get_json()["transfers"], [{"row": 2, "to": "GoTyme"}])

    def test_mapped_goalsave_accounts_are_the_everyday_rows_choices(self):
        self.sign_in_google(
            mappings={
                "gotyme": {"account_id": OTHER_ACCOUNT_ID, "enabled": True},
                "gotyme_goalsave": {
                    "source": "gotyme_goalsave",
                    "account_id": "acc-22222",
                    "enabled": True,
                },
                "gotyme_goalsave-2": {
                    "source": "gotyme_goalsave",
                    "account_id": "acc-11111",
                    "enabled": True,
                },
            }
        )
        self.connect_ynab(source="gotyme", rows=EVERYDAY_ROWS, transactions=TRANSACTIONS)
        check = {**starting_check(), "unlinked_transfers": []}
        with mock.patch.object(upload, "starting_balance_check", return_value=check) as start:
            self.client.post("/api/starting-balance", json=TARGET)
        self.assertEqual(start.call_args.kwargs["transfers"], {0: ("acc-22222", "acc-11111")})

        # A GoalSave upload: "Transfers to/from Deposit account" starts on the Everyday account
        self.connect_ynab(
            source="gotyme_goalsave",
            filename="savings.pdf",
            accounts={"22222": (GOALSAVE_ROWS, TRANSACTIONS)},
        )
        response = self.client.post("/api/statement-account", json={"account": "22222"})
        self.assertEqual(response.get_json()["own_transfers"]["account_id"], OTHER_ACCOUNT_ID)

    def test_not_signed_in_no_transfers(self):
        self.connect_ynab(source="gcash", rows=ROWS, transactions=TRANSACTIONS)
        check = starting_check()
        with mock.patch.object(upload, "starting_balance_check", return_value=check) as start:
            self.client.post("/api/starting-balance", json=TARGET)
        self.assertEqual(start.call_args.kwargs["transfers"], {})


if __name__ == "__main__":
    unittest.main()
