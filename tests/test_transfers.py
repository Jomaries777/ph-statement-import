"""
Tests for transfers.py: which statement rows a transfer rule matches, and the
checks on rules sent by the page.

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
    transfer_accounts,
)

GCASH = SOURCES["gcash"]
GOTYME = SOURCES["gotyme"]

TO_GOTYME = TransferRule("Transfer to 0917123", from_wallet="gcash", to_wallet="gotyme")


def gcash_row(description):
    return {
        "Date and Time": "2026-10-05 09:15",
        "Description": description,
        "Reference No.": "1000000000001",
        "Debit": "500.00",
    }


class NormalizeTest(unittest.TestCase):
    def test_ignores_case_and_extra_spaces(self):
        self.assertEqual(normalize("  Transfer\tTO   0917123 "), "transfer to 0917123")


class FindTransfersTest(unittest.TestCase):
    def test_a_row_containing_the_text_is_a_transfer(self):
        rows = [gcash_row("Load"), gcash_row("transfer  to 0917123 JUAN D.")]
        self.assertEqual(find_transfers(rows, GCASH, "gcash", [TO_GOTYME]), {1: "gotyme"})

    def test_only_rules_from_this_wallet_apply(self):
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(find_transfers(rows, GCASH, "gcash-2", [TO_GOTYME]), {})

    def test_the_first_matching_rule_wins(self):
        to_savings = TransferRule("Transfer to", from_wallet="gcash", to_wallet="gcash-2")
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(
            find_transfers(rows, GCASH, "gcash", [to_savings, TO_GOTYME]), {0: "gcash-2"}
        )

    def test_reads_each_sources_own_text_column(self):
        rule = TransferRule("InstaPay to GCash", from_wallet="gotyme", to_wallet="gcash")
        rows = [{"Details": "InstaPay to GCash 0917123", "Debits": "500.00"}]
        self.assertEqual(find_transfers(rows, GOTYME, "gotyme", [rule]), {0: "gcash"})

    def test_rows_the_parser_added_never_match(self):
        rule = TransferRule("Transfer", from_wallet="gcash", to_wallet="gotyme")
        real = gcash_row("Transfer to 0917123")
        added = {**gcash_row("Transfer to 0917123 (missing)"), "Reference No.": "M0000123456789"}
        self.assertEqual(find_transfers([added, real], GCASH, "gcash", [rule]), {1: "gotyme"})

    def test_no_rules_no_transfers(self):
        self.assertEqual(find_transfers([gcash_row("Transfer to 0917123")], GCASH, "gcash", []), {})


class TransferAccountsTest(unittest.TestCase):
    WALLETS = {"gcash": "acc-gcash", "gotyme": "acc-gotyme", "gcash-2": ""}

    def test_destination_wallets_become_their_accounts(self):
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(
            transfer_accounts(rows, GCASH, "acc-gcash", self.WALLETS, [TO_GOTYME]),
            {0: "acc-gotyme"},
        )

    def test_an_account_that_is_no_wallet_has_no_transfers(self):
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(transfer_accounts(rows, GCASH, "acc-bpi", self.WALLETS, [TO_GOTYME]), {})

    def test_a_destination_without_an_account_is_no_transfer(self):
        rule = TransferRule("Transfer", from_wallet="gcash", to_wallet="gcash-2")
        rows = [gcash_row("Transfer to 0917123")]
        self.assertEqual(transfer_accounts(rows, GCASH, "acc-gcash", self.WALLETS, [rule]), {})


class RulesFromDictsTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(rules_from_dicts([TO_GOTYME.to_dict()]), [TO_GOTYME])

    def test_none_is_no_rules(self):
        self.assertEqual(rules_from_dicts(None), [])


class CleanRulesTest(unittest.TestCase):
    WALLETS = {"gcash", "gotyme"}

    def clean(self, *rules):
        return clean_rules(list(rules), self.WALLETS)

    def test_a_valid_rule_with_its_spaces_tidied(self):
        rules, error = self.clean(
            {
                "description": "  Transfer   to 0917123 ",
                "from_wallet": "gcash",
                "to_wallet": "gotyme",
            }
        )
        self.assertIsNone(error)
        self.assertEqual(rules, [TO_GOTYME])

    def test_refused(self):
        cases = {
            "not a list": ({"rules": []}, "Send the rules as a list."),
            "no text": (
                [{"description": " ", "from_wallet": "gcash", "to_wallet": "gotyme"}],
                "Rule 1: enter the text the description contains.",
            ),
            "unknown wallet": (
                [{"description": "x", "from_wallet": "gcash", "to_wallet": "bpi"}],
                "Rule 1: choose the source and destination banks.",
            ),
            "same wallet": (
                [{"description": "x", "from_wallet": "gcash", "to_wallet": "gcash"}],
                "Rule 1: the source and destination banks must differ.",
            ),
            "too long": (
                [{"description": "x" * 101, "from_wallet": "gcash", "to_wallet": "gotyme"}],
                "Rule 1: keep the text under 100 characters.",
            ),
            "duplicate": (
                [
                    {"description": "Transfer", "from_wallet": "gcash", "to_wallet": "gotyme"},
                    {"description": "TRANSFER ", "from_wallet": "gcash", "to_wallet": "gotyme"},
                ],
                "Rule 2 is the same as an earlier one.",
            ),
            "not a dict": (["Transfer"], "Rule 1 is not valid."),
            "too many": (
                [{"description": "r", "from_wallet": "gcash", "to_wallet": "gotyme"}]
                * (MAX_RULES + 1),
                f"Save at most {MAX_RULES} transfer rules.",
            ),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(clean_rules(raw, self.WALLETS), (None, expected))


if __name__ == "__main__":
    unittest.main()
