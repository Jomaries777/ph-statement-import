"""
Tests for web/token_store.py: saving the YNAB token in the OS secure store.

Run from the project folder:  python -m unittest discover tests

keyring is replaced with a fake, so the real Keychain / Credential Manager /
Secret Service is never touched.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from keyring.errors import KeyringError, PasswordDeleteError

import tests.fakes  # noqa: F401 - loads the safety net (no YNAB, no keychain)
from statement_etl.web import token_store


def backend(module, priority=5, name="Test Store"):
    """A keyring backend class from `module`, as keyring.get_keyring() returns it."""
    return type("Keyring", (), {"__module__": module, "priority": priority, "name": name})()


class FakeKeyring:
    """Stands in for the keyring module, with one in-memory password."""

    def __init__(self, store=None, error=None):
        self.store = store or backend("keyring.backends.macOS")
        self.error = error
        self.passwords = {}

    def get_keyring(self):
        return self.store

    def get_password(self, service, username):
        if self.error:
            raise self.error
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        if self.error:
            raise self.error
        self.passwords[(service, username)] = password

    def delete_password(self, service, username):
        if self.error:
            raise self.error
        if (service, username) not in self.passwords:
            raise PasswordDeleteError("not found")
        del self.passwords[(service, username)]


def using(fake):
    return mock.patch.object(token_store, "keyring", fake)


class AvailableTest(unittest.TestCase):
    def test_without_keyring(self):
        with using(None):
            self.assertFalse(token_store.available())
            self.assertIsNone(token_store.store_name())

    def test_broken_backend(self):
        broken = SimpleNamespace(get_keyring=mock.Mock(side_effect=RuntimeError("no backend")))
        with using(broken):
            self.assertFalse(token_store.available())

    def test_fail_backend(self):
        with using(FakeKeyring(backend("keyring.backends.fail"))):
            self.assertFalse(token_store.available())

    def test_low_priority_backend(self):
        with using(FakeKeyring(backend("keyrings.alt.file", priority=0.5))):
            self.assertFalse(token_store.available())

    def test_real_store(self):
        with using(FakeKeyring(backend("keyring.backends.macOS", priority=1))):
            self.assertTrue(token_store.available())


class StoreNameTest(unittest.TestCase):
    def test_names(self):
        cases = {
            "keyring.backends.macOS": "macOS Keychain",
            "keyring.backends.Windows": "Windows Credential Manager",
            "keyring.backends.SecretService": "Secret Service keyring",
            "keyring.backends.libsecret": "Secret Service keyring",
            "keyring.backends.kwallet": "KWallet",
            "keyrings.other": "Other Store",
        }
        for module, name in cases.items():
            with self.subTest(module), using(FakeKeyring(backend(module, name="Other Store"))):
                self.assertEqual(token_store.store_name(), name)


class TokenTest(unittest.TestCase):
    def test_save_load_delete(self):
        fake = FakeKeyring()
        with using(fake):
            self.assertIsNone(token_store.load_token())
            self.assertTrue(token_store.save_token("test-token"))
            self.assertEqual(fake.passwords, {("statement-etl-ph", "ynab-personal-access-token"): "test-token"})
            self.assertEqual(token_store.load_token(), "test-token")
            token_store.delete_token()
            self.assertIsNone(token_store.load_token())
            token_store.delete_token()  # nothing saved: no error

    def test_empty_token_is_no_token(self):
        fake = FakeKeyring()
        fake.passwords[(token_store.SERVICE, token_store.USERNAME)] = ""
        with using(fake):
            self.assertIsNone(token_store.load_token())

    def test_locked_store(self):
        with using(FakeKeyring(error=KeyringError("locked"))):
            self.assertIsNone(token_store.load_token())
            self.assertFalse(token_store.save_token("test-token"))
            token_store.delete_token()  # no error

    def test_without_a_store_nothing_happens(self):
        fake = FakeKeyring(backend("keyring.backends.fail"))
        with using(fake):
            self.assertIsNone(token_store.load_token())
            self.assertFalse(token_store.save_token("test-token"))
            token_store.delete_token()
        self.assertEqual(fake.passwords, {})


if __name__ == "__main__":
    unittest.main()
