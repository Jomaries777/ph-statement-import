"""
Tests for ynab_api/transfers.py and the transfers argument of
pipeline.import_statement() / starting_balance_check(), against the fake YNAB
(tests/e2e/fake_ynab.py), which makes the other side of a transfer as YNAB does.

The round trip: a GCash statement's "Transfer to GoTyme" row is sent as a
transfer, so YNAB adds its other side to GoTyme; importing the GoTyme
statement afterwards must not add that row again.

Run from the project folder:  python -m unittest discover tests
All data here is made up.
"""

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from statement_etl import pipeline
from statement_etl.sources import TransferFee
from statement_etl.ynab_api.format import fee_import_id
from statement_etl.ynab_api.transfers import apply_transfers, transfer_payee_ids, unlinked
from tests.e2e import fake_ynab

GCASH = fake_ynab.EXISTING_ACCOUNT_ID  # the GCash wallet's YNAB account
GOTYME = fake_ynab.NEW_ACCOUNT_ID  # the GoTyme wallet's YNAB account
PLAN = fake_ynab.PLAN_ID
TOKEN = fake_ynab.TOKEN


def transaction(import_id, amount, day="2026-10-05", payee="Transfer to 0917123"):
    """A transaction dict as ynab_api.format.to_ynab() makes it."""
    return {
        "date": day,
        "amount": amount,
        "payee_name": payee,
        "memo": "",
        "import_id": import_id,
    }


GCASH_STATEMENT = [
    transaction("ETLPH:gcash-transfer", -500000),
    transaction("ETLPH:gcash-coffee", -150250, payee="Sample Coffee Shop"),
]
# The same 500.00 seen from GoTyme, a day later
GOTYME_STATEMENT = [
    transaction("ETLPH:gotyme-transfer", 500000, day="2026-10-06", payee="InstaPay from GCash")
]


class FakeYnabTest(unittest.TestCase):
    def setUp(self):
        self.ynab = fake_ynab.FakeYnab()
        self.enterContext(self.ynab.patch())

    def ynab_calls(self):
        return [method for method, _ in self.ynab.calls]


class ApplyTransfersTest(FakeYnabTest):
    def test_transfer_payees_of_the_plans_accounts(self):
        self.assertEqual(
            transfer_payee_ids(PLAN, TOKEN),
            {
                GCASH: fake_ynab.transfer_payee_id(GCASH),
                GOTYME: fake_ynab.transfer_payee_id(GOTYME),
            },
        )

    def test_the_matched_row_gets_the_destinations_transfer_payee(self):
        transactions, already = apply_transfers(GCASH_STATEMENT, {0: GOTYME}, GCASH, PLAN, TOKEN)
        self.assertEqual(transactions[0]["payee_id"], fake_ynab.transfer_payee_id(GOTYME))
        self.assertNotIn("payee_id", transactions[1])
        self.assertEqual(already, set())
        self.assertNotIn("payee_id", GCASH_STATEMENT[0])  # the caller's list is unchanged

    def test_no_transfers_no_ynab_calls(self):
        for transfers in (None, {}):
            with self.subTest(transfers):
                result = apply_transfers(GCASH_STATEMENT, transfers, GCASH, PLAN, TOKEN)
                self.assertEqual(result, (GCASH_STATEMENT, set()))
        self.assertEqual(self.ynab_calls(), [])

    def test_a_destination_outside_the_plan_stays_an_ordinary_row(self):
        for to_account in ("00000000-0000-4000-8000-0000000000ff", GCASH):
            with self.subTest(to_account):
                transactions, _ = apply_transfers(
                    GCASH_STATEMENT, {0: to_account}, GCASH, PLAN, TOKEN
                )
                self.assertEqual(transactions, GCASH_STATEMENT)

    def test_finds_the_other_side_ynab_added(self):
        self.ynab._add(
            GOTYME, date(2026, 10, 5), "Transfer : GCash", 500000, transfer_account_id=GCASH
        )
        _, already = apply_transfers(GOTYME_STATEMENT, {0: GCASH}, GOTYME, PLAN, TOKEN)
        self.assertEqual(already, {"ETLPH:gotyme-transfer"})

    def test_the_other_side_must_match(self):
        cases = {
            "other amount": dict(amount=400000, day="2026-10-05", account=GCASH),
            "too many days apart": dict(amount=500000, day="2026-10-01", account=GCASH),
            "from another account": dict(amount=500000, day="2026-10-05", account=GOTYME),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.ynab.reset()
                self.ynab._add(
                    GOTYME,
                    date.fromisoformat(case["day"]),
                    "Transfer",
                    case["amount"],
                    transfer_account_id=case["account"],
                )
                _, already = apply_transfers(GOTYME_STATEMENT, {0: GCASH}, GOTYME, PLAN, TOKEN)
                self.assertEqual(already, set())

    def test_one_other_side_matches_one_row(self):
        self.ynab._add(GOTYME, date(2026, 10, 6), "Transfer", 500000, transfer_account_id=GCASH)
        twice = GOTYME_STATEMENT + [transaction("ETLPH:again", 500000, day="2026-10-06")]
        _, already = apply_transfers(twice, {0: GCASH, 1: GCASH}, GOTYME, PLAN, TOKEN)
        self.assertEqual(already, {"ETLPH:gotyme-transfer"})


class ImportTest(FakeYnabTest):
    """pipeline.import_statement() and starting_balance_check(), with the
    statements' own balances left out."""

    transfer_fee = None  # the source's Source.transfer_fee

    def setUp(self):
        super().setUp()
        # The statements' own balances don't matter here
        source = SimpleNamespace(
            statement_balances=lambda rows: {"starting": Decimal(0), "ending": Decimal(0)},
            transfer_fee=self.transfer_fee,
        )
        self.enterContext(mock.patch.object(pipeline, "get_source", return_value=source))

    def import_statement(self, transactions, account_id, transfers):
        return pipeline.import_statement(
            [], transactions, account_id, PLAN, TOKEN, transfers=transfers
        )


class ImportTransfersTest(ImportTest):
    """pipeline.import_statement() and starting_balance_check() with transfers."""

    def test_round_trip_makes_one_transfer(self):
        # 1. GCash: the transfer row is sent as a transfer; YNAB adds its other side to GoTyme
        first = self.import_statement(GCASH_STATEMENT, GCASH, {0: GOTYME})
        self.assertEqual((len(first.created), first.skipped, first.transfers), (2, 0, 1))
        other_side = self.ynab.transactions(GOTYME)[-1]
        self.assertEqual((other_side.amount, other_side.transfer_account_id), (500000, GCASH))

        # 2. GoTyme: its row for the same money is already there
        check = pipeline.starting_balance_check(
            [], GOTYME_STATEMENT, GOTYME, PLAN, TOKEN, transfers={0: GCASH}
        )
        self.assertEqual((check["already_in_ynab"], check["to_upload"]), (1, 0))
        before = list(self.ynab.transactions(GOTYME))
        second = self.import_statement(GOTYME_STATEMENT, GOTYME, {0: GCASH})
        self.assertEqual((len(second.created), second.skipped, second.transfers), (0, 1, 1))
        self.assertEqual(self.ynab.transactions(GOTYME), before)
        # Nothing left to send, so only the GCash import called create_transaction
        self.assertEqual(self.ynab_calls().count("create_transaction"), 1)

    def test_the_other_order_also_makes_one_transfer(self):
        # A rule with both sides' descriptions lets either statement go first
        first = self.import_statement(GOTYME_STATEMENT, GOTYME, {0: GCASH})
        self.assertEqual((len(first.created), first.transfers), (1, 1))
        second = self.import_statement(GCASH_STATEMENT, GCASH, {0: GOTYME})
        self.assertEqual((len(second.created), second.skipped), (1, 1))  # only the coffee
        transfers = [t for t in self.ynab.transactions(GCASH) if t.transfer_account_id]
        self.assertEqual(
            [(t.amount, t.transfer_account_id) for t in transfers], [(-500000, GOTYME)]
        )

    def test_importing_again_skips_by_import_id(self):
        self.import_statement(GCASH_STATEMENT, GCASH, {0: GOTYME})
        again = self.import_statement(GCASH_STATEMENT, GCASH, {0: GOTYME})
        self.assertEqual((len(again.created), again.skipped), (0, 2))
        self.assertEqual(len([t for t in self.ynab.transactions(GOTYME) if t.amount == 500000]), 1)

    def test_without_a_rule_for_the_other_side_it_is_an_ordinary_deposit(self):
        self.import_statement(GCASH_STATEMENT, GCASH, {0: GOTYME})
        second = self.import_statement(GOTYME_STATEMENT, GOTYME, None)
        self.assertEqual((len(second.created), second.transfers), (1, 0))


# GCash charges 10.00 for each transfer out, and its statement shows it in
# the row: 510.00 leaves GCash, 500.00 reaches GoTyme
FEE = TransferFee(amount=Decimal("10.00"), payee="InstaPay fee")
GCASH_STATEMENT_WITH_FEE = [
    transaction("ETLPH:gcash-transfer", -510000),
    transaction("ETLPH:gcash-coffee", -150250, payee="Sample Coffee Shop"),
]


class TransferFeeTest(FakeYnabTest):
    """apply_transfers() with a fee: a transfer out is split into the transfer
    and the fee."""

    def test_a_transfer_out_is_split_into_the_transfer_and_the_fee(self):
        result, _ = apply_transfers(
            GCASH_STATEMENT_WITH_FEE, {0: GOTYME}, GCASH, PLAN, TOKEN, fee=FEE
        )
        transfer, coffee, fee = result
        self.assertEqual(
            (transfer["amount"], transfer["payee_id"]),
            (-500000, fake_ynab.transfer_payee_id(GOTYME)),
        )
        self.assertEqual(coffee, GCASH_STATEMENT_WITH_FEE[1])
        # At the end, so every statement row keeps its index
        self.assertEqual(
            fee,
            {
                **transaction(fee_import_id("ETLPH:gcash-transfer"), -10000),
                "payee_name": "InstaPay fee",
            },
        )
        self.assertNotEqual(fee["import_id"], transfer["import_id"])

    def test_no_fee_on_other_rows(self):
        cases = {
            "money in": (transaction("ETLPH:in", 500000), GOTYME),
            "not more than the fee": (transaction("ETLPH:small", -10000), GOTYME),
            "no named account": (transaction("ETLPH:any", -510000), (GOTYME,)),
        }
        for name, (row, to) in cases.items():
            with self.subTest(name):
                result, _ = apply_transfers([row], {0: to}, GCASH, PLAN, TOKEN, fee=FEE)
                self.assertEqual([t["amount"] for t in result], [row["amount"]])
        coffee = GCASH_STATEMENT_WITH_FEE[1]  # not a transfer
        result, _ = apply_transfers([coffee], {}, GCASH, PLAN, TOKEN, fee=FEE)
        self.assertEqual(result, [coffee])


class TransferFeeImportTest(ImportTest):
    """The round trip with GCash's fee: the 500.00 transfer matches GoTyme's
    500.00 in either order, and the 10.00 fee is its own transaction."""

    transfer_fee = FEE

    def gcash_fee(self):
        return [t for t in self.ynab.transactions(GCASH) if t.payee_name == "InstaPay fee"]

    def test_gcash_first(self):
        first = self.import_statement(GCASH_STATEMENT_WITH_FEE, GCASH, {0: GOTYME})
        self.assertEqual((len(first.created), first.transfers), (3, 1))
        self.assertEqual([t.amount for t in self.gcash_fee()], [-10000])
        self.assertEqual(self.ynab.transactions(GOTYME)[-1].amount, 500000)

        second = self.import_statement(GOTYME_STATEMENT, GOTYME, {0: GCASH})
        self.assertEqual((len(second.created), second.skipped), (0, 1))

    def test_gotyme_first_then_only_the_fee_and_the_coffee(self):
        self.import_statement(GOTYME_STATEMENT, GOTYME, {0: GCASH})
        check = pipeline.starting_balance_check(
            [], GCASH_STATEMENT_WITH_FEE, GCASH, PLAN, TOKEN, transfers={0: GOTYME}
        )
        self.assertEqual((check["already_in_ynab"], check["to_upload"]), (1, 2))

        second = self.import_statement(GCASH_STATEMENT_WITH_FEE, GCASH, {0: GOTYME})
        self.assertEqual((len(second.created), second.skipped), (2, 1))
        self.assertEqual(sorted(t.amount for t in second.created), [-150250, -10000])
        transfers = [t for t in self.ynab.transactions(GCASH) if t.transfer_account_id]
        self.assertEqual([t.amount for t in transfers], [-500000])

    def test_importing_again_skips_the_fee_too(self):
        self.import_statement(GCASH_STATEMENT_WITH_FEE, GCASH, {0: GOTYME})
        again = self.import_statement(GCASH_STATEMENT_WITH_FEE, GCASH, {0: GOTYME})
        self.assertEqual((len(again.created), again.skipped), (0, 3))
        self.assertEqual(len(self.gcash_fee()), 1)


# GoTyme Everyday and two GoalSave accounts. The same day, 1,500.00 goes to
# one and 2,500.00 to the other; the Everyday rows don't say which.
EVERYDAY = fake_ynab.EXISTING_ACCOUNT_ID
GOALSAVE_22222 = fake_ynab.NEW_ACCOUNT_ID
GOALSAVE_11111 = "00000000-0000-4000-8000-000000000004"
GOALSAVES = (GOALSAVE_22222, GOALSAVE_11111)

GOALSAVE_22222_STATEMENT = [
    transaction("ETLPH:22222-in", 1500000, day="2026-10-18", payee="Transfer from Deposit account")
]
GOALSAVE_11111_STATEMENT = [
    transaction("ETLPH:11111-in", 2500000, day="2026-10-18", payee="Transfer from Deposit account")
]
EVERYDAY_STATEMENT = [
    transaction("ETLPH:everyday-2500", -2500000, day="2026-10-18", payee="GoalSave"),
    transaction("ETLPH:everyday-1500", -1500000, day="2026-10-18", payee="GoalSave"),
]


class GoalSaveTransfersTest(ImportTest):
    """Rows that name no account (Everyday's "Transfer to GoalSave", a tuple of
    the GoalSave accounts): linked to the transfer a GoalSave import made."""

    def setUp(self):
        super().setUp()
        self.ynab.accounts[GOALSAVE_11111] = {"name": "GoalSave Travel", "transactions": []}

    def import_goalsave_statements(self):
        self.import_statement(GOALSAVE_22222_STATEMENT, GOALSAVE_22222, {0: EVERYDAY})
        self.import_statement(GOALSAVE_11111_STATEMENT, GOALSAVE_11111, {0: EVERYDAY})

    def test_each_everyday_row_finds_its_own_goalsave_account(self):
        self.import_goalsave_statements()

        transactions, already = apply_transfers(
            EVERYDAY_STATEMENT, {0: GOALSAVES, 1: GOALSAVES}, EVERYDAY, PLAN, TOKEN
        )

        self.assertEqual(already, {"ETLPH:everyday-2500", "ETLPH:everyday-1500"})
        self.assertEqual(
            [t["payee_id"] for t in transactions],
            [
                fake_ynab.transfer_payee_id(GOALSAVE_11111),
                fake_ynab.transfer_payee_id(GOALSAVE_22222),
            ],
        )

    def test_goalsave_first_then_everyday_makes_one_transfer_each(self):
        self.import_goalsave_statements()
        everyday = self.import_statement(EVERYDAY_STATEMENT, EVERYDAY, {0: GOALSAVES, 1: GOALSAVES})

        self.assertEqual((len(everyday.created), everyday.skipped), (0, 2))
        self.assertEqual(everyday.unlinked_transfers, [])
        for account, amount in ((GOALSAVE_22222, 1500000), (GOALSAVE_11111, 2500000)):
            transfers = [t for t in self.ynab.transactions(account) if t.transfer_account_id]
            self.assertEqual(
                [(t.amount, t.transfer_account_id) for t in transfers], [(amount, EVERYDAY)]
            )

    def test_everyday_first_imports_them_as_ordinary_rows(self):
        check = pipeline.starting_balance_check(
            [], EVERYDAY_STATEMENT, EVERYDAY, PLAN, TOKEN, transfers={0: GOALSAVES, 1: GOALSAVES}
        )
        self.assertEqual(check["unlinked_transfers"], [0, 1])

        everyday = self.import_statement(EVERYDAY_STATEMENT, EVERYDAY, {0: GOALSAVES, 1: GOALSAVES})

        self.assertEqual((len(everyday.created), everyday.transfers), (2, 0))
        self.assertEqual(everyday.unlinked_transfers, [0, 1])
        self.assertEqual(self.ynab.transactions(GOALSAVE_11111), [])  # no transfer made

    def test_an_empty_tuple_is_any_account(self):
        self.import_goalsave_statements()
        _, already = apply_transfers(EVERYDAY_STATEMENT, {0: (), 1: ()}, EVERYDAY, PLAN, TOKEN)
        self.assertEqual(len(already), 2)

    def test_unlinked_is_only_tuple_rows_with_no_transfer(self):
        transfers = {0: GOALSAVES, 1: GOALSAVE_22222}
        self.assertEqual(unlinked(transfers, EVERYDAY_STATEMENT, set()), [0])
        self.assertEqual(unlinked(transfers, EVERYDAY_STATEMENT, {"ETLPH:everyday-2500"}), [])
        self.assertEqual(unlinked(None, EVERYDAY_STATEMENT, set()), [])


if __name__ == "__main__":
    unittest.main()
