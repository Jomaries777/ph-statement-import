"""
ph-statement-import - remember the YNAB personal access token on this computer.

Saves the token in the operating system's secure store, via the `keyring`
package, so the web app only asks for it once:
    macOS   - Keychain                    (Keychain Access app)
    Windows - Credential Manager          (Control Panel -> Credential Manager)
    Linux   - Secret Service              (Seahorse / KWallet Manager)
The entry is named "statement-etl-ph". The token never goes into the project
folder, .env, the browser or git.

Without keyring, or without a secure store (e.g. a headless server),
available() is False and the web app asks for the token every session.
No function here raises: a locked or missing store just means "no token".

Used by the ph-statement-import web app (ynab_auth.py, upload.py and app.py).

Requirements:
    pip install keyring
"""

# Entry name in Keychain / Credential Manager / Seahorse. Never rename: changing it
# would make every saved token on users' machines disappear.
SERVICE = "statement-etl-ph"
USERNAME = "ynab-personal-access-token"

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError
except ImportError:  # keyring not installed -> feature off
    keyring = None


def available():
    """True if keyring is installed and a real secure store is behind it."""
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - a broken backend means "not available"
        return False
    # keyring's "fail" backend, or one with priority < 1, can't store anything safely
    return type(backend).__module__ != "keyring.backends.fail" and backend.priority >= 1


def store_name():
    """Human name of the store, e.g. "macOS Keychain", for messages."""
    if not available():
        return None
    name = type(keyring.get_keyring()).__module__
    if "macOS" in name:
        return "macOS Keychain"
    if "Windows" in name:
        return "Windows Credential Manager"
    if "SecretService" in name or "libsecret" in name:
        return "Secret Service keyring"
    if "kwallet" in name.lower():
        return "KWallet"
    return keyring.get_keyring().name


def load_token():
    """The saved token, or None if there isn't one (or the store can't be read)."""
    if not available():
        return None
    try:
        return keyring.get_password(SERVICE, USERNAME) or None
    except KeyringError:
        return None


def save_token(token):
    """Save the token. Returns True on success, False if it couldn't be saved."""
    if not available():
        return False
    try:
        keyring.set_password(SERVICE, USERNAME, token)
        return True
    except KeyringError:
        return False


def delete_token():
    """Remove the saved token; nothing happens if there isn't one."""
    if not available():
        return
    try:  # noqa: SIM105  # explicit try/except avoids importing contextlib
        keyring.delete_password(SERVICE, USERNAME)
    except (PasswordDeleteError, KeyringError):
        pass
