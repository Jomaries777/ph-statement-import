"""
Tests for gemini.py: the generateContent request, reading its JSON answer,
and errors that never contain the API key.

Run from the project folder:  python -m unittest discover tests

Gemini is never called: urlopen is mocked. All values here are made up.
"""

import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest import mock

import tests  # noqa: F401 - loads the safety net (no YNAB, no Google)
from statement_etl import gemini
from statement_etl.gemini import GeminiError

API_KEY = "test-gemini-key-do-not-leak"
SCHEMA = {"type": "ARRAY", "items": {"type": "STRING"}}


def gemini_answer(text):
    """A generateContent response whose answer is `text`."""
    body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return mock.MagicMock(**{"__enter__.return_value.read.return_value": json.dumps(body)})


def http_error(status, body):
    return urllib.error.HTTPError(
        "https://gemini.example.test",
        status,
        "Error",
        Message(),
        io.BytesIO(json.dumps(body).encode()),
    )


class MockedUrlopenTest(unittest.TestCase):
    """Patches urlopen, and time.sleep so retries don't really wait."""

    def setUp(self):
        patcher = mock.patch.object(gemini.urllib.request, "urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        # Retries wait for real seconds; the tests check how long they'd wait instead
        patcher = mock.patch.object(gemini.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)


class GenerateJsonTest(MockedUrlopenTest):
    def test_sends_the_prompt_and_schema_and_parses_the_answer(self):
        self.urlopen.return_value = gemini_answer('["a", "b"]')

        answer = gemini.generate_json("Pick some", SCHEMA, API_KEY, model="test-model")

        self.assertEqual(answer, ["a", "b"])
        request = self.urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent",
        )
        self.assertNotIn(API_KEY, request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), API_KEY)
        body = json.loads(request.data)
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "Pick some")
        self.assertEqual(body["generationConfig"]["responseSchema"], SCHEMA)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_model_comes_from_the_environment_then_the_default(self):
        self.urlopen.return_value = gemini_answer("[]")
        with mock.patch.dict("os.environ", {"GEMINI_MODEL": "env-model"}):
            gemini.generate_json("x", SCHEMA, API_KEY)
        self.assertIn("/env-model:", self.urlopen.call_args.args[0].full_url)

        with mock.patch.dict("os.environ", {"GEMINI_MODEL": ""}):
            gemini.generate_json("x", SCHEMA, API_KEY)
        self.assertIn(f"/{gemini.DEFAULT_MODEL}:", self.urlopen.call_args.args[0].full_url)

    def test_http_error_gives_googles_message_without_the_key(self):
        self.urlopen.side_effect = http_error(400, {"error": {"message": "API key not valid."}})

        with self.assertRaises(GeminiError) as caught:
            gemini.generate_json("x", SCHEMA, API_KEY)

        self.assertEqual(str(caught.exception), "Gemini returned HTTP 400: API key not valid.")
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_unreachable(self):
        self.urlopen.side_effect = urllib.error.URLError("no network")

        with self.assertRaisesRegex(GeminiError, "Could not reach Gemini: no network"):
            gemini.generate_json("x", SCHEMA, API_KEY)

    def test_blocked_prompt(self):
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
        self.urlopen.return_value = mock.MagicMock(
            **{"__enter__.return_value.read.return_value": json.dumps(blocked)}
        )

        with self.assertRaisesRegex(GeminiError, r"no answer \(SAFETY\)"):
            gemini.generate_json("x", SCHEMA, API_KEY)

    def test_answer_that_is_not_json(self):
        self.urlopen.return_value = gemini_answer("Sure! Here you go")

        with self.assertRaisesRegex(GeminiError, "wasn't valid JSON"):
            gemini.generate_json("x", SCHEMA, API_KEY)


class RetryTest(MockedUrlopenTest):
    BUSY = {"error": {"message": "This model is currently experiencing high demand."}}

    def test_busy_then_answers(self):
        self.urlopen.side_effect = [http_error(503, self.BUSY), gemini_answer("[]")]

        with self.assertLogs(gemini.log, "WARNING") as logs:
            answer = gemini.generate_json("x", SCHEMA, API_KEY)

        self.assertEqual(answer, [])
        self.sleep.assert_called_once_with(gemini.RETRY_WAITS[0])
        self.assertIn("Gemini is busy (HTTP 503). Trying again in 5 seconds.", logs.output[0])

    def test_too_many_requests_is_retried_too(self):
        self.urlopen.side_effect = [http_error(429, {}), gemini_answer("[]")]

        with self.assertLogs(gemini.log, "WARNING"):
            gemini.generate_json("x", SCHEMA, API_KEY)

        self.assertEqual(self.urlopen.call_count, 2)

    def test_stays_busy_gives_up_after_every_wait(self):
        tries = len(gemini.RETRY_WAITS) + 1
        self.urlopen.side_effect = [http_error(503, self.BUSY) for _ in range(tries)]

        with self.assertLogs(gemini.log, "WARNING"), self.assertRaises(GeminiError) as caught:
            gemini.generate_json("x", SCHEMA, API_KEY)

        self.assertEqual(self.urlopen.call_count, tries)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], list(gemini.RETRY_WAITS))
        self.assertEqual(
            str(caught.exception),
            "Gemini returned HTTP 503: This model is currently experiencing high demand."
            f" (tried {tries} times)",
        )
        self.assertEqual(caught.exception.status, 503)

    def test_other_errors_are_not_retried(self):
        self.urlopen.side_effect = http_error(400, {"error": {"message": "API key not valid."}})

        with self.assertRaises(GeminiError):
            gemini.generate_json("x", SCHEMA, API_KEY)

        self.assertEqual(self.urlopen.call_count, 1)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
