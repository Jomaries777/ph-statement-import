"""
Safety net for every test: nothing may reach YNAB or the OS secure store.

`python -m unittest discover tests` imports the test files as top-level
modules, so this file runs when a test file imports `tests.fakes` (they all do).

    YNAB     - any real API call raises, so a test that forgets to mock YNAB
               fails instead of using the BEARER_TOKEN from .env
    keyring  - the "fail" backend, so no test reads or writes the real
               Keychain / Credential Manager / Secret Service
"""

import keyring
import ynab
from keyring.backends import fail


def _no_ynab(*args, **kwargs):
    raise RuntimeError("A test tried to reach YNAB. Mock the YNAB API in the test.")


ynab.ApiClient.call_api = _no_ynab
keyring.set_keyring(fail.Keyring())
