"""
Tests for web/transfer_rules.py and its page (/transfer-rules), in both modes:
locally the rules and wallets go in the settings file (local_settings.py);
hosted, the rules go on the signed-in Google user's record and the wallets are
their auto-import mappings.

Run from the project folder:  python -m unittest discover tests
Firestore is replaced with FakeStore (tests/fakes.py). All data here is made up.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from statement_etl import local_settings
from statement_etl.web import app as web
from statement_etl.web import transfer_rules
from tests.fakes import ACCOUNT_ID, PLAN_ID
from tests.hosted import HOSTED_ENV, hosted_mode
from tests.test_web_app import WebTest
from tests.test_web_auto_import import (
    OTHER_ACCOUNT_ID,
    SUB,
    UNINVITED_EMAIL,
    AutoImportWebTest,
)

RULE = {"description": "Transfer to 0917123", "from_wallet": "gcash", "to_wallet": "gotyme"}


def page_data(html: bytes, name: str):
    """A data-* value the page's script reads, e.g. page_data(html, "wallets")."""
    match = re.search(rf"data-{name}='([^']*)'", html.decode())
    return json.loads(match.group(1))


# =============================================================================
# LOCAL MODE - the settings file
# =============================================================================


class LocalTransferRulesTest(WebTest):
    def setUp(self):
        super().setUp()
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "settings.json"
        self.patch_dict(os.environ, {"LOCAL_SETTINGS_PATH": str(self.path)})

    def wallets(self):
        return [
            {"id": ACCOUNT_ID, "source": "gcash", "account_name": "GCash1"},
            {"id": OTHER_ACCOUNT_ID, "source": "gotyme", "account_name": "GoTyme2"},
        ]

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
        with self.client.get("/static/transfer_rules.js") as script:
            self.assertEqual(script.status_code, 200)

    def test_save_writes_the_file_and_the_page_shows_it(self):
        rule = {
            "description": "Transfer to 0917123",
            "from_wallet": ACCOUNT_ID,
            "to_wallet": OTHER_ACCOUNT_ID,
        }
        response = self.save(
            plan_id=PLAN_ID, plan_name="Test Plan", wallets=self.wallets(), rules=[rule]
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json(), {"saved": True, "rules": 1})
        self.assertEqual(
            json.loads(self.path.read_text()),
            {
                "plan_id": PLAN_ID,
                "plan_name": "Test Plan",
                "wallets": self.wallets(),
                "transfer_rules": [rule],
            },
        )

        page = self.client.get("/transfer-rules").data
        self.assertEqual(page_data(page, "rules"), [rule])
        self.assertEqual(
            [(w["name"], w["account_name"]) for w in page_data(page, "wallets")],
            [("GCash", "GCash1"), ("GoTyme", "GoTyme2")],
        )
        self.assertIn(f'data-plan-id="{PLAN_ID}"'.encode(), page)

    def test_rules_must_use_saved_wallets(self):
        response = self.save(
            wallets=self.wallets()[:1],
            rules=[{"description": "x", "from_wallet": ACCOUNT_ID, "to_wallet": OTHER_ACCOUNT_ID}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "Rule 1: choose the source and destination banks."
        )
        self.assertFalse(self.path.exists())

    def test_wallets_are_checked(self):
        cases = {
            "not a list": ({}, "Send the banks as a list."),
            "unknown source": (
                [{"id": ACCOUNT_ID, "source": "bpi"}],
                "Bank 1: choose which bank.",
            ),
            "no account": ([{"id": "", "source": "gcash"}], "Bank 1: choose a YNAB account."),
            "same account twice": (
                [{"id": ACCOUNT_ID, "source": "gcash"}, {"id": ACCOUNT_ID, "source": "gotyme"}],
                "Each YNAB account can be only one bank.",
            ),
        }
        for name, (wallets, expected) in cases.items():
            with self.subTest(name):
                response = self.save(wallets=wallets, rules=[])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], expected)

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


class HostedTransferRulesTest(AutoImportWebTest):
    def setUp(self):
        super().setUp()
        self.patch(transfer_rules, "store", new=self.store)
        no_file = AssertionError("hosted mode used the local settings file")
        self.patch(local_settings, "load_settings", side_effect=no_file)
        self.patch(local_settings, "save_settings", side_effect=no_file)

    def sign_in_with_wallets(self, **fields):
        self.sign_in_google(
            enabled=True,
            plan_id=PLAN_ID,
            mappings={
                "gcash": {
                    "source": "gcash",
                    "account_id": ACCOUNT_ID,
                    "account_name": "GCash1",
                    "enabled": True,
                },
                "gotyme": {
                    "source": "gotyme",
                    "account_id": OTHER_ACCOUNT_ID,
                    "account_name": "GoTyme2",
                    "enabled": True,
                },
                "gcash-2": {"source": "gcash", "enabled": False},  # Don't import: no account
            },
            password_inputs={
                "gcash": {
                    "last_name": "Dela Cruz",
                    "mobile_last4": "1234",
                    "mobile_first7": "0917123",
                },
            },
            **fields,
        )

    def test_signed_out_page_asks_to_sign_in(self):
        page = self.client.get("/transfer-rules")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="card-sign-in"', page.data)
        self.assertNotIn(b'id="rules-table"', page.data)
        response = self.client.post("/api/transfer-rules", json={"rules": [RULE]})
        self.assertEqual(response.status_code, 401)

    def test_uninvited_account_is_refused(self):
        self.sign_in_google(email=UNINVITED_EMAIL)
        response = self.client.post("/api/transfer-rules", json={"rules": [RULE]})
        self.assertEqual(response.status_code, 403)

    def test_page_lists_the_mapped_wallets(self):
        self.sign_in_with_wallets(transfer_rules=[RULE])
        page = self.client.get("/transfer-rules").data
        # gcash-2 is set to "Don't import": no YNAB account, so not a wallet here
        self.assertEqual(
            page_data(page, "wallets"),
            [
                {
                    "id": "gcash",
                    "name": "GCash · 0917123",
                    "source": "gcash",
                    "account_id": ACCOUNT_ID,
                    "account_name": "GCash1",
                },
                {
                    "id": "gotyme",
                    "name": "GoTyme",
                    "source": "gotyme",
                    "account_id": OTHER_ACCOUNT_ID,
                    "account_name": "GoTyme2",
                },
            ],
        )
        self.assertEqual(page_data(page, "rules"), [RULE])
        self.assertIn(b'id="transfer-rules-link"', self.client.get("/auto-import").data)

    def test_save_puts_the_rules_on_the_user(self):
        self.sign_in_with_wallets()
        response = self.client.post(
            "/api/transfer-rules",
            json={"rules": [{**RULE, "description": " Transfer  to 0917123"}]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.store.get_user(SUB)["transfer_rules"], [RULE])

    def test_only_mapped_wallets(self):
        self.sign_in_with_wallets()
        response = self.client.post(
            "/api/transfer-rules", json={"rules": [{**RULE, "to_wallet": "gcash-2"}]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("transfer_rules", self.store.get_user(SUB))


class HostedWithoutGoogleTest(unittest.TestCase):
    def test_no_rules_page_without_google_sign_in(self):
        with hosted_mode({**HOSTED_ENV, "GOOGLE_CLIENT_ID": ""}):
            client = web.app.test_client()
            self.assertNotIn(b"/transfer-rules", client.get("/").data)
            self.assertEqual(client.get("/transfer-rules").status_code, 404)
            self.assertEqual(client.post("/api/transfer-rules", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
