"""
Tests for the source registry and the GCash source.

Run from the project folder:  python -m unittest discover tests

All data here is made up. Never put a real statement, name, account or
reference number in a test.

Adding an e-wallet / bank? Copy the GCash tests below for your source
(see docs/adding-an-e-wallet.md, "Add tests").
"""

import dataclasses
import io
import os
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

import tests.fakes  # noqa: F401 - loads the safety net (no YNAB, no keychain)
from statement_etl import pipeline
from statement_etl.parsers import gcash
from statement_etl.sources import DEFAULT_SOURCE, SOURCES, Source, get_source
from statement_etl.transaction import Transaction
from statement_etl.ynab_api.format import to_ynab

# =============================================================================
# REGISTRY - every source is wired up consistently
# =============================================================================


class RegistryTest(unittest.TestCase):
    def test_every_source_has_a_converter(self):
        for source in SOURCES.values():
            self.assertTrue(callable(source.to_transactions), source.key)

    def test_source_keys_match_their_entries(self):
        for key, source in SOURCES.items():
            self.assertEqual(key, source.key)

    def test_column_settings_name_real_columns(self):
        for source in SOURCES.values():
            with self.subTest(source=source.key):
                columns = set(source.columns)
                self.assertEqual(len(columns), len(source.columns), "duplicate column")
                self.assertLessEqual(set(source.amount_columns), columns)
                self.assertLessEqual(set(source.money_in_columns), set(source.amount_columns))
                self.assertLessEqual(set(source.money_out_columns), set(source.amount_columns))
                if source.text_column is not None:
                    self.assertIn(source.text_column, columns)

    def test_password_env_names(self):
        for source in SOURCES.values():
            self.assertTrue(source.password_env.endswith("_PDF_PASSWORD"))

    def test_unknown_source(self):
        with self.assertRaisesRegex(ValueError, "Known sources: gcash"):
            get_source("nope")

    def test_get_source(self):
        self.assertIs(get_source("gcash"), SOURCES["gcash"])


class SourceDefaultsTest(unittest.TestCase):
    def test_default_source_is_registered(self):
        self.assertIn(DEFAULT_SOURCE, SOURCES)

    def test_optional_functions_default_to_nothing(self):
        source = Source(
            key="plain",
            name="Plain",
            password_env="PLAIN_PDF_PASSWORD",
            columns=("Date",),
            parse=lambda pdf, password: [],
            statement_balances=lambda rows: {},
            to_transactions=lambda rows: [],
        )
        row = {"Date": "2026-10-01"}
        self.assertEqual(source.amount_columns, ())
        self.assertIsNone(source.text_column)
        self.assertIsNone(source.summary([row]))
        self.assertFalse(source.is_correction_row(row))
        self.assertEqual(source.balance_corrections([row]), [])
        self.assertIsNone(source.balance_warning([row]))

    def test_sources_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            SOURCES["gcash"].key = "renamed"


# =============================================================================
# GCASH - synthetic rows in the shape gcash.parse_transactions() returns
# =============================================================================


def gcash_row(date_time, description, reference, debit, credit, balance):
    return {
        "Date and Time": date_time,
        "Description": description,
        "Reference No.": reference,
        "Debit": debit,
        "Credit": credit,
        "Balance": balance,
    }


GCASH_ROWS = [
    gcash_row(
        "2026-10-01 09:00 AM",
        "Received GCash from Test Sender",
        "1000000000001",
        "",
        "1,000.00",
        "1,500.00",
    ),
    gcash_row(
        "2026-10-02 06:50 PM",
        "Payment to Test Shop, Merchant Transaction Number: X1",
        "1000000000002",
        "250.50",
        "",
        "1,249.50",
    ),
]


class GCashTest(unittest.TestCase):
    def test_rows_have_exactly_the_columns(self):
        for row in GCASH_ROWS:
            self.assertEqual(tuple(row), gcash.COLUMNS)

    def test_row_to_ynab(self):
        [t] = [to_ynab(t) for t in gcash.to_transactions([GCASH_ROWS[1]])]
        self.assertEqual(t["date"], "2026-10-02")
        self.assertEqual(t["amount"], -250500)  # milliunits, money out is negative
        self.assertEqual(t["payee_name"], "Test Shop")
        self.assertEqual(t["memo"], "GCash Ref: 1000000000002")
        self.assertIsNone(t["flag_color"])

    def test_import_id_is_stable(self):
        first = to_ynab(gcash.to_transactions([GCASH_ROWS[1]])[0])["import_id"]
        again = to_ynab(gcash.to_transactions([dict(GCASH_ROWS[1])])[0])["import_id"]
        self.assertEqual(first, again)
        self.assertLessEqual(len(first), 36)  # YNAB's limit

    def test_statement_balances(self):
        balances = gcash.statement_balances(GCASH_ROWS)
        self.assertEqual(balances, {"starting": Decimal("500.00"), "ending": Decimal("1249.50")})

    def test_correction_rows(self):
        self.assertFalse(gcash.is_correction_row(GCASH_ROWS[0]))
        self.assertTrue(
            gcash.is_correction_row({**GCASH_ROWS[0], "Reference No.": "R1000000000001"})
        )
        self.assertIsNone(gcash.balance_warning(GCASH_ROWS))

    def test_summary(self):
        self.assertEqual(
            gcash.summary(GCASH_ROWS),
            "Total debit 250.50 · Total credit 1,000.00 · matches statement ✓",
        )


# =============================================================================
# A SOURCE WITH OTHER COLUMNS - the pipeline and the web app only use what
# the Source says, never GCash's column names
# =============================================================================

FAKE_ROWS = [
    {"Date": "01/10/2026", "Details": "Cash in", "Amount": "+200.00", "Running Balance": "300.00"},
    {
        "Date": "02/10/2026",
        "Details": "Bills payment",
        "Amount": "-50.00",
        "Running Balance": "250.00",
    },
]


def fake_to_transactions(rows):
    transactions = []
    for row in rows:
        day, month, year = row["Date"].split("/")
        transactions.append(
            Transaction(
                source="fake",
                key=row["Date"] + row["Running Balance"],
                date=date(int(year), int(month), int(day)),
                payee=row["Details"],
                amount=Decimal(row["Amount"]),
            )
        )
    return transactions


FAKE = Source(
    key="fake",
    name="Fake Wallet",
    password_env="FAKE_PDF_PASSWORD",
    columns=("Date", "Details", "Amount", "Running Balance"),
    amount_columns=("Amount", "Running Balance"),
    text_column="Details",
    parse=lambda pdf, password: [dict(r) for r in FAKE_ROWS],
    statement_balances=lambda rows: {"starting": Decimal("100.00"), "ending": Decimal("250.00")},
    to_transactions=fake_to_transactions,
)


@mock.patch.dict(SOURCES, {"fake": FAKE})
class OtherColumnsTest(unittest.TestCase):
    def test_parse_statement(self):
        rows, transactions = pipeline.parse_statement(None, "", source="fake")
        self.assertEqual(rows, FAKE_ROWS)
        self.assertEqual([t["amount"] for t in transactions], [200000, -50000])

    def test_balance_check_uses_the_source(self):
        with (
            mock.patch.object(pipeline, "find_already_imported", return_value=set()),
            mock.patch.object(pipeline, "check_starting_balance") as check,
        ):
            pipeline.starting_balance_check(FAKE_ROWS, [], "account", "plan", source="fake")
        self.assertEqual(check.call_args.args[0], Decimal("100.00"))

    def test_web_parse(self):
        os.environ.setdefault("FLASK_SECRET_KEY", "test")
        from statement_etl.web.app import app

        response = app.test_client().post(
            "/api/parse",
            data={"source": "fake", "password": "", "pdf": (io.BytesIO(b"%PDF"), "fake.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        parsed = response.get_json()
        self.assertEqual(parsed["columns"], list(FAKE.columns))
        self.assertEqual(parsed["rows"][1], ["02/10/2026", "Bills payment", "-50.00", "250.00"])
        self.assertEqual(parsed["amount_columns"], ["Amount", "Running Balance"])
        self.assertEqual(parsed["text_column"], "Details")
        self.assertEqual(parsed["source_name"], "Fake Wallet")
        self.assertEqual(parsed["starting_balance"], "100.00")
        self.assertIsNone(parsed["summary"])


if __name__ == "__main__":
    unittest.main()
