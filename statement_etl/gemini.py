"""
ph-statement-import - Google Gemini REST client: ask a question, get JSON back.

    generate_json(prompt, schema, api_key) - one generateContent call whose
                                             answer must match a JSON schema

When Gemini is busy (HTTP 429 or 5xx, e.g. "This model is currently
experiencing high demand"), the call waits and tries again, up to
len(RETRY_WAITS) more times, before giving up.

Uses plain HTTPS via `urllib`, like autoimport/gmail.py, so there's no Google
client library to install. Used by categorize.py.

Environment:
    GEMINI_API_KEY - from Google AI Studio (https://aistudio.google.com/apikey)
    GEMINI_MODEL   - optional, default DEFAULT_MODEL

Tests:
    python -m unittest tests.test_gemini
"""

import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Picking one category from a short list is a simple job: the light model is fast,
# cheap, and was answering in Oct 2026 while gemini-3.8-flash was overloaded
DEFAULT_MODEL = "gemini-3.5-flash-lite"
TIMEOUT_SECONDS = 120  # a long list of transactions can take a while to answer

# Gemini's "busy, try again later" answers: too many requests, or overloaded
BUSY_STATUSES = {429, 500, 502, 503, 504}
# Seconds to wait before each retry; longer each time, so a busy spell can pass
RETRY_WAITS = (5, 15, 30, 60)


class GeminiError(RuntimeError):
    """A failed Gemini call, safe to show or log (never contains the API key)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status, if Gemini answered with an error


def generate_json(prompt: str, schema: dict, api_key: str, model: str | None = None):
    """Ask Gemini `prompt` and return its answer, parsed from JSON.

    schema is the shape the answer must have, in Gemini's responseSchema
    format, e.g. {"type": "ARRAY", "items": {"type": "STRING"}} -> ["a", "b"].
    model defaults to $GEMINI_MODEL, then DEFAULT_MODEL.

    Raises GeminiError if Gemini can't be reached, stays busy after every
    retry, refuses, or answers something that isn't JSON.
    """
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    url = f"{GEMINI_API_BASE}/{urllib.parse.quote(model)}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
    }
    # The key goes in a header, not the URL, so it never shows up in an error or a log
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    response = json.loads(_send_with_retries(request))

    try:
        text = response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        reason = (response.get("promptFeedback") or {}).get("blockReason") or "no answer"
        raise GeminiError(f"Gemini gave no answer ({reason}).") from None
    try:
        return json.loads(text)
    except ValueError:
        raise GeminiError("Gemini's answer wasn't valid JSON.") from None


def _send_with_retries(request: urllib.request.Request) -> bytes:
    """_send(request), waiting RETRY_WAITS seconds and trying again while Gemini is busy.

    Any other error is raised at once: retrying a wrong key or a bad request
    wouldn't help. Each wait is logged as a warning, which the command line
    prints, so a long wait doesn't look like a hang.
    """
    tries = len(RETRY_WAITS) + 1
    for wait in (*RETRY_WAITS, None):  # None: the last try, with no wait after it
        try:
            return _send(request)
        except GeminiError as e:
            if e.status not in BUSY_STATUSES:
                raise
            if wait is None:
                raise GeminiError(f"{e} (tried {tries} times)", status=e.status) from None
            log.warning("Gemini is busy (HTTP %s). Trying again in %s seconds.", e.status, wait)
        time.sleep(wait)


def _send(request: urllib.request.Request) -> bytes:
    """urlopen(request).read(), with errors turned into GeminiError."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read()).get("error", {})
            detail = error.get("message") or error.get("status") or e.reason
        except (ValueError, AttributeError, OSError, http.client.HTTPException):
            detail = e.reason
        finally:
            e.close()
        raise GeminiError(f"Gemini returned HTTP {e.code}: {detail}", status=e.code) from None
    except urllib.error.URLError as e:
        raise GeminiError(f"Could not reach Gemini: {e.reason}") from None
    except (TimeoutError, http.client.HTTPException) as e:
        raise GeminiError(f"Could not reach Gemini: {e or type(e).__name__}") from None
