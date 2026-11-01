"""
ph-statement-import - settings saved on this computer, for local mode.

Hosted mode keeps settings in Firestore (autoimport/store.py). Running the app
on your own computer, they go in one JSON file instead:

    ~/.config/ph-statement-import/settings.json   (LOCAL_SETTINGS_PATH overrides it)

    {
      "plan_id": "...", "plan_name": "My Plan",
      "wallets": [{"id": <YNAB account id>, "source": "gcash", "account_name": "GCash1"}],
      "transfer_rules": [{"description": "Transfer to 0917123",
                          "from_wallet": <wallet id>, "to_wallet": <wallet id>}]
    }

A local wallet is a YNAB account you import one source's statements into, so
its id is that account's id. Nothing secret goes in this file: the YNAB token
stays in the OS secure store (web/token_store.py).
"""

import json
import os
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "ph-statement-import" / "settings.json"


class LocalSettingsError(Exception):
    """The settings file exists but can't be read as settings."""


def settings_path() -> Path:
    """Where the file is: LOCAL_SETTINGS_PATH, read on every call (tests set it), or the default."""
    return Path(os.environ.get("LOCAL_SETTINGS_PATH") or DEFAULT_PATH)


def load_settings() -> dict:
    """Everything in the file; {} when there's no file yet."""
    path = settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        settings = json.loads(text)
    except ValueError as e:
        raise LocalSettingsError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(settings, dict):
        raise LocalSettingsError(f"{path} should hold a JSON object.")
    return settings


def save_settings(**fields) -> None:
    """Set these fields and keep the rest. Writes a new file and swaps it in, so a
    crash halfway never leaves half a file."""
    settings = {**load_settings(), **fields}
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
