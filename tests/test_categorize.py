"""
Tests for categorize.py: asking Gemini for categories, checking its picks,
and saving only those to YNAB.

Run from the project folder:  python -m unittest discover tests

Neither YNAB nor Gemini is called: ynab_api.categories' functions and
gemini.generate_json are mocked. All data here is made up.
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

import tests  # noqa: F401 - loads the safety net (no YNAB, no Google)
from statement_etl import categorize
from tests.fakes import PLAN_ID

API_KEY = "test-gemini-key"

CATEGORIES = [
    {"id": "c-income", "name": "Inflow: Ready to Assign", "group": "Internal Master Category"},
    {"id": "c-food", "name": "Groceries", "group": "Everyday"},
    {"id": "c-misc", "name": "Misc", "group": "Everyday"},
    {"id": "c-misc-2", "name": "Misc", "group": "Travel"},
]


def transaction(id, payee, amount):
    return {
        "id": id,
        "date": "2026-10-05",
        "account": "Test Wallet",
        "payee": payee,
        "memo": f"Ref {id}",
        "amount": Decimal(amount),
    }


SHOP = transaction("t1", "Test Grocery", "-249.00")
SALARY = transaction("t2", "Test Employer", "15000.00")
MYSTERY = transaction("t3", "XYZ 123", "-50.00")


class SuggestCategoriesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(categorize.gemini, "generate_json")
        self.generate_json = patcher.start()
        self.addCleanup(patcher.stop)

    def test_maps_each_pick_back_to_its_category(self):
        self.generate_json.return_value = [
            {"index": 0, "category": "Everyday / Groceries"},
            {"index": 1, "category": "Internal Master Category / Inflow: Ready to Assign"},
            {"index": 2, "category": "unsure"},
        ]

        suggestions = categorize.suggest_categories(
            [SHOP, SALARY, MYSTERY], CATEGORIES, API_KEY, "test-model"
        )

        self.assertEqual(
            [(s.transaction["id"], s.category and s.category["id"]) for s in suggestions],
            [("t1", "c-food"), ("t2", "c-income"), ("t3", None)],
        )

    def test_prompt_has_the_categories_and_transactions_but_not_ynab_ids(self):
        self.generate_json.return_value = []

        categorize.suggest_categories([SHOP], CATEGORIES, API_KEY, "test-model")

        prompt, schema, api_key, model = self.generate_json.call_args.args
        self.assertIn("Everyday / Misc\nTravel / Misc", prompt)
        self.assertIn('"payee": "Test Grocery"', prompt)
        self.assertIn('"amount": "-249.00"', prompt)
        self.assertNotIn('"t1"', prompt)
        self.assertEqual(
            schema["items"]["properties"]["category"]["enum"],
            [
                "Internal Master Category / Inflow: Ready to Assign",
                "Everyday / Groceries",
                "Everyday / Misc",
                "Travel / Misc",
                "unsure",
            ],
        )
        self.assertEqual((api_key, model), (API_KEY, "test-model"))

    def test_ignores_made_up_categories_and_indexes(self):
        self.generate_json.return_value = [
            {"index": 0, "category": "Everyday / Snacks"},  # not a category
            {"index": 7, "category": "Everyday / Groceries"},  # no such transaction
            "not a pick",
        ]

        suggestions = categorize.suggest_categories([SHOP], CATEGORIES, API_KEY)

        self.assertIsNone(suggestions[0].category)

    def test_asks_in_batches(self):
        many = [transaction(f"t{i}", "Test Grocery", "-1.00") for i in range(250)]
        self.generate_json.side_effect = lambda prompt, *args: [
            {"index": i, "category": "Everyday / Groceries"}
            for i in range(len(json.loads(prompt.split("Transactions (JSON):\n")[1])))
        ]

        suggestions = categorize.suggest_categories(many, CATEGORIES, API_KEY)

        self.assertEqual(self.generate_json.call_count, 3)  # 100 + 100 + 50
        self.assertEqual(len(suggestions), 250)
        self.assertTrue(all(s.category["id"] == "c-food" for s in suggestions))

    def test_answer_that_is_not_a_list(self):
        self.generate_json.return_value = {"index": 0}

        with self.assertRaises(categorize.gemini.GeminiError):
            categorize.suggest_categories([SHOP], CATEGORIES, API_KEY)


class CategorizeUncategorizedTest(unittest.TestCase):
    def setUp(self):
        self.get_transactions = self.patch("get_uncategorized_transactions")
        self.get_categories = self.patch("get_categories", return_value=CATEGORIES)
        self.set_categories = self.patch("set_categories", return_value=1)
        self.generate_json = self.patch("generate_json", target=categorize.gemini)
        self.generate_json.return_value = [
            {"index": 0, "category": "Everyday / Groceries"},
            {"index": 1, "category": "unsure"},
        ]

    def patch(self, name, target=categorize, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_saves_only_the_sure_picks(self):
        self.get_transactions.return_value = [SHOP, MYSTERY]

        suggestions, updated = categorize.categorize_uncategorized(
            PLAN_ID, API_KEY, account_id="acct", access_token="page-token"
        )

        self.assertEqual(updated, 1)
        self.assertEqual(len(suggestions), 2)
        self.get_transactions.assert_called_once_with(PLAN_ID, "acct", None, "page-token")
        self.set_categories.assert_called_once_with(PLAN_ID, {"t1": "c-food"}, "page-token")

    def test_dry_run_changes_nothing(self):
        self.get_transactions.return_value = [SHOP, MYSTERY]

        suggestions, updated = categorize.categorize_uncategorized(PLAN_ID, API_KEY, dry_run=True)

        self.assertEqual((len(suggestions), updated), (2, 0))
        self.set_categories.assert_not_called()

    def test_nothing_uncategorized_asks_no_one(self):
        self.get_transactions.return_value = []

        self.assertEqual(categorize.categorize_uncategorized(PLAN_ID, API_KEY), ([], 0))
        self.get_categories.assert_not_called()
        self.generate_json.assert_not_called()

    def test_gemini_failing_saves_nothing(self):
        self.get_transactions.return_value = [SHOP]
        self.generate_json.side_effect = categorize.gemini.GeminiError("Gemini returned HTTP 503")

        with self.assertRaises(categorize.gemini.GeminiError):
            categorize.categorize_uncategorized(PLAN_ID, API_KEY)
        self.set_categories.assert_not_called()


class MainTest(unittest.TestCase):
    ENV = {"BEARER_TOKEN": "test-token", "GEMINI_API_KEY": API_KEY, "YNAB_PLAN_ID": PLAN_ID}

    def run_main(self, *args, env=None):
        output = io.StringIO()
        with (
            mock.patch.dict("os.environ", env if env is not None else self.ENV, clear=True),
            mock.patch("sys.argv", ["categorize", *args]),
            redirect_stdout(output),
        ):
            categorize.main()
        return output.getvalue()

    def test_prints_each_pick(self):
        suggestions = [
            categorize.Suggestion(SHOP, CATEGORIES[1]),
            categorize.Suggestion(MYSTERY, None),
        ]
        with mock.patch.object(
            categorize, "categorize_uncategorized", return_value=(suggestions, 1)
        ) as run:
            output = self.run_main("--since", "2026-10-01")

        self.assertEqual(run.call_args.args, (PLAN_ID, API_KEY))
        self.assertEqual(str(run.call_args.kwargs["since_date"]), "2026-10-01")
        self.assertIn("Everyday / Groceries", output)
        self.assertIn("(unsure, left as is)", output)
        self.assertIn("Categorized 1 of 2 transactions in YNAB.", output)

    def test_dry_run_says_nothing_changed(self):
        suggestions = [categorize.Suggestion(SHOP, CATEGORIES[1])]
        with mock.patch.object(
            categorize, "categorize_uncategorized", return_value=(suggestions, 0)
        ) as run:
            output = self.run_main("--dry-run")

        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertIn("Nothing was changed in YNAB.", output)

    def test_needs_the_gemini_key(self):
        env = {k: v for k, v in self.ENV.items() if k != "GEMINI_API_KEY"}
        with (
            mock.patch.object(categorize, "load_dotenv"),
            mock.patch("sys.stderr", io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            self.run_main(env=env)
        self.assertIn("GEMINI_API_KEY", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
