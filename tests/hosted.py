"""
Run the web app in hosted mode for a test, then go back to local mode.

Hosted mode is read once, when the web modules are imported (config.HOSTED),
so hosted_mode() sets PUBLIC_URL and APP_CONFIG and reloads them:

    with hosted_mode():
        client = web_app.app.test_client()   # the app as it runs on the server

Afterwards the modules are reloaded again with the environment as it was.
All values here are made up.
"""

import contextlib
import importlib
import json
import os
from unittest import mock

from statement_etl.web import (
    app,
    auto_import,
    config,
    limits,
    state,
    transfer_rules,
    upload,
    ynab_auth,
    ynab_oauth,
)

PUBLIC_URL = "https://example.test"
SECRETS = {"FLASK_SECRET_KEY": "test-secret-key", "YNAB_CLIENT_SECRET": "test-client-secret"}
HOSTED_ENV = {
    "PUBLIC_URL": PUBLIC_URL,
    "APP_CONFIG": json.dumps(SECRETS),
    "YNAB_CLIENT_ID": "test-client-id",
}

# In import order: each one reads the ones before it. Reloading state.py
# starts an empty STORE; limits.py, a new limiter with no counts.
# token_store.py doesn't depend on the mode.
WEB_MODULES = (
    config,
    state,
    limits,
    ynab_oauth,
    ynab_auth,
    upload,
    auto_import,
    transfer_rules,
    app,
)


def reload_web():
    for module in WEB_MODULES:
        importlib.reload(module)


@contextlib.contextmanager
def hosted_mode(env=HOSTED_ENV):
    """The web modules reloaded with `env` (hosted mode); local mode again after."""
    try:
        with mock.patch.dict(os.environ, env):
            reload_web()
            yield
    finally:
        reload_web()
