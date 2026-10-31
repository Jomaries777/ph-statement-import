"""
Safety net for every test: nothing may reach YNAB, Google or the OS secure store.

`python -m unittest discover tests` imports the test files as top-level
modules, so this file runs when a test file imports `tests.fakes` (they all do).

    YNAB       - any real API call raises, so a test that forgets to mock YNAB
                 fails instead of using the BEARER_TOKEN from .env
    keyring    - the "fail" backend, so no test reads or writes the real
                 Keychain / Credential Manager / Secret Service
    Firestore  - creating a client raises, so a test that forgets to fake it
                 fails instead of looking for Google credentials and a database
    googleapis - any urllib request to *.googleapis.com (Gmail, Google OAuth)
                 raises, so a test that forgets to fake it never reaches Google
"""

import urllib.parse
import urllib.request

import keyring
import ynab
from google.cloud import firestore
from keyring.backends import fail


def _no_ynab(*args, **kwargs):
    raise RuntimeError("A test tried to reach YNAB. Mock the YNAB API in the test.")


def _no_firestore(*args, **kwargs):
    raise RuntimeError("A test tried to reach Firestore. Fake store.firestore_client in the test.")


_real_open = urllib.request.OpenerDirector.open


def _no_googleapis(opener, url, *args, **kwargs):
    """OpenerDirector.open, except for Google's APIs.

    urlopen() and every opener go through OpenerDirector.open, so this catches
    a request however the module under test imported urllib.
    """
    full_url = url if isinstance(url, str) else url.full_url  # a str or a Request
    host = urllib.parse.urlsplit(full_url).hostname or ""
    if host == "googleapis.com" or host.endswith(".googleapis.com"):
        raise RuntimeError(f"A test tried to reach {host}. Mock the HTTP call in the test.")
    return _real_open(opener, url, *args, **kwargs)


ynab.ApiClient.call_api = _no_ynab
keyring.set_keyring(fail.Keyring())
firestore.Client.__init__ = _no_firestore
urllib.request.OpenerDirector.open = _no_googleapis
