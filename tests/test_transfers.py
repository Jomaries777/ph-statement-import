"""
Tests for transfers.py: which statement rows a transfer rule matches, on
either bank's side, and the checks on rules sent by the page.

Run from the project folder:  python -m unittest discover tests
All data here is made up.
"""

import unittest

from statement_etl.sources import SOURCES
from statement_etl.transfers import (
    MAX_RULES,
    TransferRule,
    clean_rules,
    find_transfers,
    normalize,
    rules_from_dicts,
    unlinked_message,
)

GCASH = SOURCES["gcash"]
GOTYME = SOURCES["gotyme"]
GOALSAVE = SOURCES["gotyme_goalsave"]

# The plan's open accounts, {id: name}, as clean_rules() gets them
ACCOUNTS = {"acc-gcash": "GCash - 1161", "acc-gotyme": "GoTyme", "acc-bpi": "BPI Savings"}

GCASH_GOTYME = TransferRule(
    account_1_id="acc-gcash",
    account_1_name="GCash - 1161",
    description_1="Transfer to 0917123",
    account_2_id="acc-gotyme",
    account_2_name="GoTyme",
    description_2="InstaPay from GCash",
)


def gcash_row(description):
    return {
        "Date and Time": "2026-10-05 09:15",
        "Description": description,
        "Reference No.": "1000000000001",
        "Debit": "500.00",
    }


def gotyme_row(details):
    return {"Details": details, "Credits": "500.00"}


def page_rule(account_1="acc-gcash", description_1="Transfer", account_2="acc-gotyme", **more):
    """A rule as the page sends it."""
    return {
        "account_1_id": account_1,
        "description_1": description_1,
        "account_2_id": account_2,
        "description_2": more.get("description_2", ""),
    }


class NormalizeTest(unittest.TestCase):
    def test_ignores_case_and_extra_spaces(self):
        self.assertEqual(normalize("  Transfer\tTO   0917123 "), "transfer to 0917123")


class FindTransfersTest(unittest.TestCase):
    def test_bank_1s_rows_go_to_bank_2(self):
        rows = [gcash_row("Load"), gcash_row("transfer  to 0917123 JUAN D.")]
        self.assertEqual(
            find_transfers(rows, GCASH, "acc-gcash", [GCASH_GOTYME]), {1: "acc-gotyme"}
        )

    def test_bank_2s_rows_go_to_bank_1(self):
        rows = [gotyme_row("InstaPay from GCash 0917123")]
        self.assertEqual(
            find_transfers(rows, GOTYME, "acc-gotyme", [GCASH_GOTYME]), {0: "acc-gcash"}
        )

    def test_each_side_uses_only_its_own_description(self):
        rows = [gotyme_row("Transfer to 0917123")]  # Bank #1's text in Bank #2's statement
        self.assertEqual(find_transfers(rows, GOTYME, "acc-gotyme", [GCASH_GOTYME]), {})

    def test_an_empty_bank_2_description_matches_nothing(self):
        one_sided = TransferRule(
            "acc-gcash", "GCash - 1161", "Transfer", "acc-gotyme", "GoTyme", ""
        )
        rows = [gotyme_row("Transfer from GCash")]
        self.assertEqual(find_transfers(rows, GOTYME, "acc-gotyme", [one_sided]), {})

    def test_an_account_in_no_rule_has_no_transfers(self):
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(find_transfers(rows, GCASH, "acc-bpi", [GCASH_GOTYME]), {})

    def test_the_first_matching_rule_wins(self):
        to_bpi = TransferRule("acc-gcash", "GCash - 1161", "Transfer to", "acc-bpi", "BPI", "")
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(
            find_transfers(rows, GCASH, "acc-gcash", [to_bpi, GCASH_GOTYME]), {0: "acc-bpi"}
        )

    def test_rows_the_parser_added_never_match(self):
        real = gcash_row("Transfer to 0917123")
        added = {**gcash_row("Transfer to 0917123 (missing)"), "Reference No.": "M0000123456789"}
        self.assertEqual(
            find_transfers([added, real], GCASH, "acc-gcash", [GCASH_GOTYME]), {1: "acc-gotyme"}
        )

    def test_no_rules_no_transfers(self):
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(find_transfers(rows, GCASH, "acc-gcash", []), {})


class OwnTransfersTest(unittest.TestCase):
    """GoalSave and GoTyme Everyday moves, which need no rule (Source.own_transfers)."""

    GOALSAVE_ROWS = [
        {"Details": "Earned interest"},
        {"Details": "Transfer to Deposit account"},
        {"Details": "Transfer from  deposit ACCOUNT"},  # case and spaces don't matter
    ]
    EVERYDAY_ROWS = [gotyme_row("Transfer from GoalSave"), gotyme_row("Card payment at SHOPEE")]
    MAPPED = {"gotyme": ["acc-gotyme"], "gotyme_goalsave": ["acc-22222", "acc-11111"]}

    def test_goalsave_rows_go_to_the_everyday_account(self):
        self.assertEqual(
            find_transfers(self.GOALSAVE_ROWS, GOALSAVE, "acc-22222", [], self.MAPPED),
            {1: "acc-gotyme", 2: "acc-gotyme"},
        )

    def test_everyday_rows_go_to_one_of_the_goalsave_accounts(self):
        self.assertEqual(
            find_transfers(self.EVERYDAY_ROWS, GOTYME, "acc-gotyme", [], self.MAPPED),
            {0: ("acc-22222", "acc-11111")},
        )

    def test_with_one_goalsave_account_the_everyday_row_names_it(self):
        mapped = {"gotyme": ["acc-gotyme"], "gotyme_goalsave": ["acc-22222"]}
        self.assertEqual(
            find_transfers(self.EVERYDAY_ROWS, GOTYME, "acc-gotyme", [], mapped), {0: "acc-22222"}
        )

    def test_with_nothing_mapped(self):
        # GoalSave: no Everyday account to send to. Everyday: any account's transfer.
        self.assertEqual(find_transfers(self.GOALSAVE_ROWS, GOALSAVE, "acc-22222", []), {})
        self.assertEqual(find_transfers(self.EVERYDAY_ROWS, GOTYME, "acc-gotyme", []), {0: ()})

    def test_a_rule_still_comes_first(self):
        rule = TransferRule("acc-gotyme", "GoTyme", "GoalSave", "acc-bpi", "BPI Savings", "")
        self.assertEqual(
            find_transfers(self.EVERYDAY_ROWS, GOTYME, "acc-gotyme", [rule], self.MAPPED),
            {0: "acc-bpi"},
        )

    def test_gcash_has_none(self):
        rows = [gcash_row("Transfer to GoalSave")]
        self.assertEqual(find_transfers(rows, GCASH, "acc-gcash", [], self.MAPPED), {})

    def test_the_unlinked_message(self):
        self.assertEqual(
            unlinked_message(GOTYME, 2),
            "2 GoalSave rows imported without a transfer. "
            "Import the GoTyme GoalSave statement first, so its transfers link up.",
        )
        self.assertTrue(unlinked_message(GOTYME, 1).startswith("1 GoalSave row imported"))


class RulesFromDictsTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(rules_from_dicts([GCASH_GOTYME.to_dict()]), [GCASH_GOTYME])

    def test_none_is_no_rules(self):
        self.assertEqual(rules_from_dicts(None), [])


class CleanRulesTest(unittest.TestCase):
    def test_a_valid_rule_with_names_from_the_plan(self):
        sent = {
            "account_1_id": "acc-gcash",
            "account_1_name": "Not used",  # the name comes from the plan's accounts
            "description_1": "  Transfer   to 0917123 ",
            "account_2_id": "acc-gotyme",
            "description_2": "InstaPay from GCash",
        }
        self.assertEqual(clean_rules([sent], ACCOUNTS), ([GCASH_GOTYME], None))

    def test_bank_2s_description_is_optional(self):
        rules, error = clean_rules([page_rule(description_2=" ")], ACCOUNTS)
        self.assertIsNone(error)
        self.assertEqual(rules[0].description_2, "")

    def test_refused(self):
        cases = {
            "not a list": ({"rules": []}, "Send the rules as a list."),
            "not a dict": (["Transfer"], "Rule 1 is not valid."),
            "no bank #2": ([page_rule(account_2="")], "Rule 1: choose both banks."),
            "not in the plan": ([page_rule(account_2="acc-other")], "Rule 1: choose both banks."),
            "same bank": (
                [page_rule(account_2="acc-gcash")],
                "Rule 1: Bank #1 and Bank #2 must differ.",
            ),
            "no description 1": (
                [page_rule(description_1=" ")],
                "Rule 1: enter Bank #1's description.",
            ),
            "too long": (
                [page_rule(description_2="x" * 101)],
                "Rule 1: keep each description under 100 characters.",
            ),
            "same side twice": (
                [page_rule(), page_rule(description_1="TRANSFER ", account_2="acc-bpi")],
                "Rule 2 is the same as an earlier one.",
            ),
            "the other side of an earlier rule": (
                [
                    page_rule(description_2="InstaPay"),
                    page_rule(
                        account_1="acc-gotyme", description_1="instapay", account_2="acc-bpi"
                    ),
                ],
                "Rule 2 is the same as an earlier one.",
            ),
            "too many": (
                [page_rule()] * (MAX_RULES + 1),
                f"Save at most {MAX_RULES} transfer rules.",
            ),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(clean_rules(raw, ACCOUNTS), (None, expected))


if __name__ == "__main__":
    unittest.main()
