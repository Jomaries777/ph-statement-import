"""
Tests for local_settings.py: the JSON settings file used in local mode.

Run from the project folder:  python -m unittest discover tests
Every test uses a file in a temporary folder (LOCAL_SETTINGS_PATH).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from statement_etl import local_settings


class LocalSettingsTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "nested" / "settings.json"
        self.enterContext(mock.patch.dict(os.environ, {"LOCAL_SETTINGS_PATH": str(self.path)}))

    def test_no_file_yet_is_no_settings(self):
        self.assertEqual(local_settings.load_settings(), {})

    def test_save_creates_the_folder_and_keeps_other_fields(self):
        local_settings.save_settings(plan_id="plan-1")
        local_settings.save_settings(transfer_rules=[{"description": "x"}])
        self.assertEqual(
            json.loads(self.path.read_text()),
            {"plan_id": "plan-1", "transfer_rules": [{"description": "x"}]},
        )
        self.assertEqual(local_settings.load_settings()["plan_id"], "plan-1")
        self.assertFalse(self.path.with_name("settings.json.tmp").exists())

    def test_a_broken_file_says_where_it_is(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{not json", "[1, 2]"):
            with self.subTest(text):
                self.path.write_text(text)
                with self.assertRaises(local_settings.LocalSettingsError) as caught:
                    local_settings.load_settings()
                self.assertIn(str(self.path), str(caught.exception))

    def test_default_path_without_the_variable(self):
        with mock.patch.dict(os.environ, {"LOCAL_SETTINGS_PATH": ""}):
            self.assertEqual(local_settings.settings_path(), local_settings.DEFAULT_PATH)


if __name__ == "__main__":
    unittest.main()
