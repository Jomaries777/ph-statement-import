"""
Tests for autoimport/store.py: auto-import settings, encrypted in Firestore.
Also the auto-import startup check in web/config.py, and the safety nets in
tests/__init__.py that keep tests away from Firestore and Google's APIs.

Run from the project folder:  python -m unittest discover tests

Firestore is replaced with FakeFirestore below, a dict of documents, so no
test needs Google credentials or a database. All values are made up.
"""

import copy
import inspect
import json
import os
import unittest
import urllib.request
from datetime import UTC, datetime, timedelta
from unittest import mock

from cryptography.fernet import Fernet
from google.cloud import firestore

from statement_etl.autoimport import store
from statement_etl.web import config
from tests.fakes import ENCRYPTION_KEY, FakeStore
from tests.hosted import HOSTED_ENV, SECRETS, hosted_mode

SUB = "100000000000000000001"  # a Google account ID ("sub")
GMAIL_TOKEN = "1//made-up-gmail-refresh-token"
YNAB_TOKEN = "made-up-ynab-refresh-token"
INPUTS = {"last_name": "Dela Cruz", "mobile_last4": "1234"}


# =============================================================================
# FAKE FIRESTORE - the parts of firestore.Client that store.py uses
# =============================================================================


class FakeFirestore:
    """Stands in for firestore.Client: documents in a dict, keyed by their path.

    documents = {"auto_import_users/123": {...}, "auto_import_users/123/runs/m1:gcash": {...}}
    reads lists every read: ("get", document path) or ("list", collection path).
    """

    def __init__(self):
        self.documents = {}
        self.reads = []

    def collection(self, name):
        return FakeQuery(self, name)


class FakeDocument:
    def __init__(self, db, path):
        self.db = db
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def collection(self, name):
        return FakeQuery(self.db, f"{self.path}/{name}")

    def get(self):
        self.db.reads.append(("get", self.path))
        return FakeSnapshot(self.id, self.db.documents.get(self.path))

    def set(self, data, merge=False):
        data = copy.deepcopy(data)
        if merge:  # a list of fields: replace just those
            self.db.documents.setdefault(self.path, {}).update({name: data[name] for name in merge})
        else:
            self.db.documents[self.path] = data

    def delete(self):
        self.db.documents.pop(self.path, None)


class FakeSnapshot:
    def __init__(self, id, fields):
        self.id = id
        self.exists = fields is not None
        self.fields = fields

    def to_dict(self):
        return copy.deepcopy(self.fields)


class FakeQuery:
    """A collection, or a query on one: where("=="), order_by(), limit()."""

    def __init__(self, db, path, filters=(), order=None, count=None):
        self.db = db
        self.path = path
        self.filters = filters
        self.order = order
        self.count = count

    def document(self, id):
        return FakeDocument(self.db, f"{self.path}/{id}")

    def where(self, *, filter):
        assert filter.op_string == "==", "FakeQuery only supports =="
        return FakeQuery(self.db, self.path, (*self.filters, filter), self.order, self.count)

    def order_by(self, field, direction):
        return FakeQuery(self.db, self.path, self.filters, (field, direction), self.count)

    def limit(self, count):
        return FakeQuery(self.db, self.path, self.filters, self.order, count)

    def list_documents(self):
        self.db.reads.append(("list", self.path))
        children = [path for path in self.db.documents if path.rsplit("/", 1)[0] == self.path]
        return [FakeDocument(self.db, path) for path in children]

    def stream(self):
        snapshots = [document.get() for document in self.list_documents()]
        for match in self.filters:
            snapshots = [s for s in snapshots if s.fields.get(match.field_path) == match.value]
        if self.order:
            field, direction = self.order
            reverse = direction == firestore.Query.DESCENDING
            snapshots.sort(key=lambda snapshot: snapshot.fields[field], reverse=reverse)
        return snapshots[: self.count]


class StoreTestCase(unittest.TestCase):
    """Each test gets an empty FakeFirestore and the made-up ENCRYPTION_KEY."""

    def setUp(self):
        self.firestore = FakeFirestore()
        self.enterContext(mock.patch.object(store, "firestore_client", return_value=self.firestore))
        self.enterContext(mock.patch.dict(os.environ, ENCRYPTION_KEYS=ENCRYPTION_KEY))


# =============================================================================
# ENCRYPTION
# =============================================================================


class EncryptionTest(StoreTestCase):
    def test_round_trip(self):
        token = store.encrypt(GMAIL_TOKEN)
        self.assertNotIn(GMAIL_TOKEN, token)
        self.assertEqual(store.decrypt(token), GMAIL_TOKEN)

    def test_old_key_still_decrypts_after_a_new_key_is_put_first(self):
        old_token = store.encrypt(GMAIL_TOKEN)
        new_key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, ENCRYPTION_KEYS=f"{new_key}, {ENCRYPTION_KEY}"):
            self.assertEqual(store.decrypt(old_token), GMAIL_TOKEN)
            new_token = store.encrypt(GMAIL_TOKEN)
        with mock.patch.dict(os.environ, ENCRYPTION_KEYS=new_key):  # encrypted with the new one
            self.assertEqual(store.decrypt(new_token), GMAIL_TOKEN)

    def test_wrong_key_gives_a_clear_error(self):
        token = store.encrypt(GMAIL_TOKEN)
        other_key = Fernet.generate_key().decode()
        with (
            mock.patch.dict(os.environ, ENCRYPTION_KEYS=other_key),
            self.assertRaises(store.DecryptError) as caught,
        ):
            store.decrypt(token)
        self.assertIn("any key in ENCRYPTION_KEYS", str(caught.exception))
        self.assertNotIn(other_key, str(caught.exception))

    def test_missing_or_bad_key_names_the_setting_only(self):
        for keys in ("", " , ", "do-not-print-this"):
            with self.subTest(keys=keys), mock.patch.dict(os.environ, ENCRYPTION_KEYS=keys):
                with self.assertRaisesRegex(RuntimeError, "^ENCRYPTION_KEYS ") as caught:
                    store.encrypt(GMAIL_TOKEN)
                self.assertNotIn("do-not-print-this", str(caught.exception))


# =============================================================================
# USER SETTINGS
# =============================================================================


class UserTest(StoreTestCase):
    def save_secrets(self):
        store.save_user(SUB, gmail_refresh_token=GMAIL_TOKEN, password_inputs={"gcash": INPUTS})
        store.save_ynab_tokens(SUB, {"access_token": "made-up-access", "refresh_token": YNAB_TOKEN})

    def test_firestore_never_receives_a_plain_text_secret(self):
        self.save_secrets()
        stored = repr(self.firestore.documents)
        for secret in (GMAIL_TOKEN, YNAB_TOKEN, "made-up-access", "Dela Cruz", "mobile_last4"):
            self.assertNotIn(secret, stored)

    def test_secrets_come_back_encrypted_and_decrypt(self):
        self.save_secrets()
        user = store.get_user(SUB)
        self.assertEqual(user["sub"], SUB)
        self.assertNotEqual(user["gmail_refresh_token"], GMAIL_TOKEN)
        self.assertEqual(store.decrypt(user["gmail_refresh_token"]), GMAIL_TOKEN)
        self.assertEqual(store.decrypt(user["ynab_refresh_token"]), YNAB_TOKEN)
        self.assertEqual(json.loads(store.decrypt(user["password_inputs"]["gcash"])), INPUTS)

    def test_unknown_user(self):
        self.assertIsNone(store.get_user(SUB))

    def test_save_replaces_the_given_fields_and_keeps_the_rest(self):
        store.save_user(SUB, email="juan@example.com", mappings={"gcash": {}, "gotyme": {}})
        store.save_user(SUB, mappings={"gcash": {"account_id": "acct-1"}})
        user = store.get_user(SUB)
        self.assertEqual(user["email"], "juan@example.com")
        self.assertEqual(user["mappings"], {"gcash": {"account_id": "acct-1"}})  # no gotyme

    def test_set_last_checked(self):
        when = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        store.set_last_checked(SUB, when)
        self.assertEqual(store.get_user(SUB)["last_checked"], when)

    def test_list_enabled_users(self):
        store.save_user("on", enabled=True)
        store.save_user("off", enabled=False)
        self.assertEqual([user["sub"] for user in store.list_enabled_users()], ["on"])

    def test_delete_user_removes_the_runs_too(self):
        store.save_user(SUB, enabled=True)
        store.write_run(SUB, "msg-1", "gcash", status="ok")
        store.write_run(SUB, "msg-2", "gcash", status="ok")
        store.write_run("someone-else", "msg-1", "gcash", status="ok")
        store.delete_user(SUB)
        self.assertIsNone(store.get_user(SUB))
        self.assertEqual(store.list_runs(SUB), [])
        self.assertTrue(store.run_exists("someone-else", "msg-1", "gcash"))


# =============================================================================
# RUNS
# =============================================================================


class RunTest(StoreTestCase):
    def test_run_exists_after_write_run(self):
        self.assertFalse(store.run_exists(SUB, "msg-1", "gcash"))
        store.write_run(SUB, "msg-1", "gcash", status="ok", created=3)
        self.assertTrue(store.run_exists(SUB, "msg-1", "gcash"))
        self.assertFalse(store.run_exists(SUB, "msg-1", "gotyme"))  # same email, other source

    def test_run_exists_reads_one_document_and_never_lists(self):
        store.write_run(SUB, "msg-1", "gcash", status="ok")
        self.firestore.reads.clear()
        store.run_exists(SUB, "msg-1", "gcash")
        path = f"auto_import_users/{SUB}/runs/msg-1:gcash"
        self.assertEqual(self.firestore.reads, [("get", path)])

    def test_list_runs_newest_first(self):
        times = [datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=n) for n in range(3)]
        for number, ran_at in enumerate(times):
            with mock.patch.object(store, "datetime") as clock:
                clock.now.return_value = ran_at
                store.write_run(SUB, f"msg-{number}", "gcash", status="ok")
        runs = store.list_runs(SUB, limit=2)
        self.assertEqual(runs[0], {"status": "ok", "source": "gcash", "ran_at": times[2]})
        self.assertEqual(runs[1]["ran_at"], times[1])


# =============================================================================
# FAKE STORE - tests/fakes.py, for the tests of the code that uses the store
# =============================================================================


class FakeStoreTest(StoreTestCase):
    STORE_FUNCTIONS = (
        "encrypt",
        "decrypt",
        "get_user",
        "save_user",
        "list_enabled_users",
        "delete_user",
        "set_last_checked",
        "save_ynab_tokens",
        "run_exists",
        "write_run",
        "list_runs",
    )

    def test_same_functions_as_the_store(self):
        def parameters(function):
            return [
                (p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()
            ]

        fake = FakeStore()
        for name in self.STORE_FUNCTIONS:
            with self.subTest(name):
                self.assertEqual(parameters(getattr(fake, name)), parameters(getattr(store, name)))

    def test_secrets_are_kept_encrypted(self):
        fake = FakeStore()
        fake.save_ynab_tokens(SUB, {"refresh_token": YNAB_TOKEN})
        self.assertNotIn(YNAB_TOKEN, repr(fake.users))
        self.assertEqual(fake.decrypt(fake.get_user(SUB)["ynab_refresh_token"]), YNAB_TOKEN)


# =============================================================================
# STARTUP - web/config.py
# =============================================================================


class AutoImportStartupTest(unittest.TestCase):
    """Hosted mode needs the auto-import keys only when GOOGLE_CLIENT_ID is set."""

    GOOGLE_ENV = {"GOOGLE_CLIENT_ID": "made-up-google-client-id", "GOOGLE_CLIENT_SECRET": ""}

    def start(self, **env):
        # ENCRYPTION_KEYS="" so a key in the shell or .env can't make a test pass
        with (
            self.assertRaises(SystemExit) as caught,
            hosted_mode({**HOSTED_ENV, "ENCRYPTION_KEYS": "", **env}),
        ):
            pass
        return str(caught.exception)

    def test_missing_key_is_named_and_no_value_printed(self):
        app_config = {**SECRETS, "GOOGLE_CLIENT_SECRET": "do-not-print-this"}
        message = self.start(**self.GOOGLE_ENV, APP_CONFIG=json.dumps(app_config))
        self.assertEqual(
            message,
            "Auto-import (GOOGLE_CLIENT_ID is set) needs ENCRYPTION_KEYS"
            " in APP_CONFIG or the environment.",
        )

    def test_every_missing_key_is_named(self):
        message = self.start(**self.GOOGLE_ENV)
        self.assertIn("needs GOOGLE_CLIENT_SECRET, ENCRYPTION_KEYS", message)

    def test_starts_without_google_settings(self):
        # Today's deploy: hosted mode, no GOOGLE_CLIENT_ID, no auto-import keys
        env = {
            **HOSTED_ENV,
            "GOOGLE_CLIENT_ID": "",
            "GOOGLE_CLIENT_SECRET": "",
            "ENCRYPTION_KEYS": "",
        }
        with hosted_mode(env):
            self.assertTrue(config.HOSTED)

    def test_starts_with_every_google_setting(self):
        secrets = {**SECRETS, "GOOGLE_CLIENT_SECRET": "made-up", "ENCRYPTION_KEYS": ENCRYPTION_KEY}
        with hosted_mode({**HOSTED_ENV, **self.GOOGLE_ENV, "APP_CONFIG": json.dumps(secrets)}):
            self.assertTrue(config.HOSTED)


# =============================================================================
# SAFETY NETS - tests/__init__.py
# =============================================================================


class SafetyNetTest(unittest.TestCase):
    def test_unfaked_firestore_fails(self):
        with self.assertRaisesRegex(RuntimeError, "tried to reach Firestore"):
            store.get_user(SUB)

    def test_unmocked_googleapis_request_fails(self):
        requests = (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            urllib.request.Request("https://oauth2.googleapis.com/token", data=b"made-up"),
        )
        for request in requests:
            with (
                self.subTest(request=request),
                self.assertRaisesRegex(RuntimeError, "tried to reach .*googleapis.com"),
            ):
                urllib.request.urlopen(request, timeout=5)


if __name__ == "__main__":
    unittest.main()
