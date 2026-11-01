"""
ph-statement-import - auto-import settings and run log, encrypted in Firestore.

What's stored: auto-import runs every 15 minutes with no browser, so for each
Google account that turns it on it keeps everything a run needs. One Firestore
document per account, in the project's (default) database:

    auto_import_users/{google_sub}
        email, enabled, last_checked, runs_cleared_at
        progress              (the import running now, if any; runner.Progress)
        gmail_refresh_token   (encrypted)
        ynab_refresh_token    (encrypted)
        plan_id, plan_name
        categorize            (True: Gemini categorizes what each import creates)
        mappings: {wallet id: {source, account_id, account_name, enabled}}
                  A wallet is one account at a source: "gcash", then "gcash-2",
                  ... for more GCash accounts. Saved before there could be
                  several: no "source", and the id is the source key
                  (runner.wallet_source()).
        password_inputs: {wallet id: <encrypted JSON {last_name, mobile_last4,
                          mobile_first7}>}  (mobile_first7 tells GCash accounts apart)
        transfer_pairs: [{account_1_id, account_1_name, description_1,
                          account_2_id, account_2_name, description_2}]
                        (transfers.py; saved on /transfer-rules, also without
                        auto-import on)
        transfer_plan_id: the YNAB plan /transfer-rules last listed accounts from
        runs/{gmail_msg_id}:{source}
            source, account_id, account_name, status, created, skipped, message,
            received_at, ran_at, step, duration_seconds, wallet, wallet_name,
            categorized, categorize_error   (only when categorize is on)
            transfers                       (rows imported as transfers, if any)

Nothing is stored for users who don't turn it on, and "Turn off and delete"
removes the document and its runs (delete_user()).

Why it's encrypted: the refresh tokens open someone's Gmail and YNAB, and the
password inputs open their statements. They're encrypted here, in the app,
before they're sent to Firestore, so someone who can read the database (in the
console or an export) still can't use them without ENCRYPTION_KEYS, which only
the app has. Cloud KMS would do the same, but it isn't free.

Secrets go in as plain text and come back encrypted: save_user() encrypts
them, get_user() returns them as stored, and the code that needs one calls
decrypt() right before using it. A page that only shows the settings never
holds a plain-text token.

Environment (APP_CONFIG):
    ENCRYPTION_KEYS - comma-separated Fernet keys, newest first. New data is
                      encrypted with the first key; any of them decrypts. To
                      rotate, put a new key first and keep the old ones until
                      all data has been rewritten. Make a key with:
        python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'

Firestore finds its project and credentials the usual Google way: on Cloud
Run, the service's own service account (app-runtime).

Requirements:
    pip install google-cloud-firestore cryptography
"""

import functools
import json
import os
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from google.cloud import firestore

USERS = "auto_import_users"  # top-level collection, one document per Google account
RUNS = "runs"  # subcollection of each user: one document per email and source

# Record fields that are saved encrypted. Everything else is stored as is.
ENCRYPTED_TOKENS = ("gmail_refresh_token", "ynab_refresh_token")
ENCRYPTED_INPUTS = "password_inputs"  # {source key: {input name: value}}


class DecryptError(Exception):
    """Stored data can't be decrypted with any key in ENCRYPTION_KEYS."""


# =============================================================================
# ENCRYPTION
# =============================================================================


def _fernet() -> MultiFernet:
    """A MultiFernet with every key in ENCRYPTION_KEYS, newest (first) one first.

    Read on every call, so a test or a new key in the environment takes effect at once.
    """
    keys = [key.strip() for key in os.environ.get("ENCRYPTION_KEYS", "").split(",")]
    keys = [key for key in keys if key]
    if not keys:
        raise RuntimeError("ENCRYPTION_KEYS is not set.")
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError:  # the message never includes the key itself
        raise RuntimeError(
            "ENCRYPTION_KEYS has a key that isn't a Fernet key (32 url-safe base64 bytes)."
        ) from None


def encrypt(text: str) -> str:
    """Encrypt text with the newest key: "1//refresh-token" -> "gAAAAAB..."."""
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    """The text encrypt() was given: "gAAAAAB..." -> "1//refresh-token".

    Raises DecryptError if no key in ENCRYPTION_KEYS matches, e.g. because the
    key that encrypted it was removed.
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise DecryptError(
            "Can't decrypt stored auto-import data with any key in ENCRYPTION_KEYS."
            " Was the key that encrypted it removed?"
        ) from None


def encrypt_secrets(fields: dict) -> dict:
    """A copy of record fields with the tokens and password inputs encrypted.

    {"ynab_refresh_token": "abc", "password_inputs": {"gcash": {"last_name": "Dela Cruz"}}}
    -> {"ynab_refresh_token": "gAAAAAB...", "password_inputs": {"gcash": "gAAAAAB..."}}
    """
    fields = dict(fields)
    for name in ENCRYPTED_TOKENS:
        if name in fields:
            fields[name] = encrypt(fields[name])
    if ENCRYPTED_INPUTS in fields:
        fields[ENCRYPTED_INPUTS] = {
            source_key: encrypt(json.dumps(inputs))
            for source_key, inputs in fields[ENCRYPTED_INPUTS].items()
        }
    return fields


# =============================================================================
# FIRESTORE DOCUMENTS
# =============================================================================


@functools.cache
def firestore_client() -> firestore.Client:
    """One Firestore client for the process (it keeps a connection open)."""
    return firestore.Client()


def _user_document(sub: str) -> firestore.DocumentReference:
    """auto_import_users/{sub}, the user's settings."""
    return firestore_client().collection(USERS).document(sub)


def _run_document(sub: str, msg_id: str, source_key: str) -> firestore.DocumentReference:
    """auto_import_users/{sub}/runs/{msg_id}:{source_key}, one email's import for one source."""
    return _user_document(sub).collection(RUNS).document(f"{msg_id}:{source_key}")


def _user_record(snapshot: firestore.DocumentSnapshot) -> dict:
    """A user document's fields, plus its ID as "sub"."""
    return {"sub": snapshot.id, **snapshot.to_dict()}


# =============================================================================
# USER SETTINGS
# =============================================================================


def get_user(sub: str) -> dict | None:
    """The user's record, or None if they haven't set up auto-import.

    get_user("1234") -> {"sub": "1234", "email": "juan@example.com", "enabled": True, ...}
    Tokens and password inputs are still encrypted: decrypt() them where they're used.
    """
    snapshot = _user_document(sub).get()
    return _user_record(snapshot) if snapshot.exists else None


def save_user(sub: str, **fields) -> None:
    """Set these fields on the user's record (created if new); other fields are kept.

    save_user("1234", enabled=True, ynab_refresh_token="abc",
              password_inputs={"gcash": {"last_name": "Dela Cruz", "mobile_last4": "1234"}})
    Give tokens and password inputs in plain text: they're encrypted here.
    """
    # merge=<these fields> replaces each given field whole. merge=True would
    # merge maps key by key, so a source removed from `mappings` would stay.
    _user_document(sub).set(encrypt_secrets(fields), merge=list(fields))


def list_enabled_users() -> list[dict]:
    """Every user with auto-import turned on, as get_user() returns them."""
    # Only the enabled ones: each document read counts toward Firestore's free daily reads
    query = (
        firestore_client()
        .collection(USERS)
        .where(filter=firestore.FieldFilter("enabled", "==", True))
    )
    return [_user_record(snapshot) for snapshot in query.stream()]


def delete_user(sub: str) -> None:
    """Remove the user's record and all their runs ("Turn off and delete")."""
    user = _user_document(sub)
    # Deleting a document leaves its subcollections behind, so the runs go
    # first. If this stops halfway, the user is still there to delete again.
    for run in user.collection(RUNS).list_documents():
        run.delete()
    user.delete()


def set_last_checked(sub: str, when: datetime) -> None:
    """Remember when Gmail was last checked; the next check asks for newer emails only."""
    save_user(sub, last_checked=when)


def set_progress(sub: str, progress: dict) -> None:
    """Save the import running now, for Recent imports on the page (runner.Progress)."""
    save_user(sub, progress=progress)


def clear_progress(sub: str) -> None:
    """The run has ended: nothing is running."""
    save_user(sub, progress=None)


def save_ynab_tokens(sub: str, tokens: dict) -> None:
    """Save the refresh token from a YNAB token response (ynab_oauth.refresh()).

    YNAB rotates refresh tokens, so this runs after every refresh: the old one
    stops working.
    """
    save_user(sub, ynab_refresh_token=tokens["refresh_token"])


# =============================================================================
# RUNS - one per email and source, so an email is never imported twice
# =============================================================================


def run_exists(sub: str, msg_id: str, source_key: str) -> bool:
    """True if this email was already imported (or tried) for this source."""
    # A document get is one read. Listing the runs would be one read per run.
    return _run_document(sub, msg_id, source_key).get().exists


def write_run(sub: str, msg_id: str, source_key: str, **result) -> None:
    """Record one email's import, with ran_at set to now (UTC).

    write_run("1234", "18c2f0a1b2", "gcash", account_id="...", account_name="GCash",
              status="ok", created=12, skipped=0, message="", received_at=<datetime>)
    """
    record = {**result, "source": source_key, "ran_at": datetime.now(UTC)}
    _run_document(sub, msg_id, source_key).set(record)


def list_runs(sub: str, limit: int = 20, after: datetime | None = None) -> list[dict]:
    """The user's newest runs first, at most `limit`, for the run log on the page.

    after - only runs after this time, e.g. the user's runs_cleared_at
    """
    query = _user_document(sub).collection(RUNS)
    if after is not None:
        query = query.where(filter=firestore.FieldFilter("ran_at", ">", after))
    query = query.order_by("ran_at", direction=firestore.Query.DESCENDING).limit(limit)
    return [snapshot.to_dict() for snapshot in query.stream()]


def clear_runs(sub: str) -> None:
    """Hide the user's runs so far from the run log ("Clear" on the page).

    The runs stay stored: they're also the list of emails already imported
    (run_exists()), and deleting them could import an email again.
    "Turn off and delete" (delete_user()) removes them for good.
    """
    save_user(sub, runs_cleared_at=datetime.now(UTC))
