"""
ph-statement-import - transfer rules: which statement rows are money moved
between two of your own YNAB accounts.

A rule pairs two YNAB accounts, with the text each one's statement shows for
money moved between them:

    TransferRule(
        account_1_id="acc-gcash", account_1_name="GCash - 1161",
        description_1="Transfer to 0917123",
        account_2_id="acc-gotyme", account_2_name="GoTyme",
        description_2="InstaPay from GCash",
    )

In a statement imported into GCash - 1161, a row whose description contains
"Transfer to 0917123" is a transfer to GoTyme; in one imported into GoTyme, a
row containing "InstaPay from GCash" is a transfer to GCash - 1161. With both
descriptions set, either statement can be imported first: the first makes the
YNAB transfer, and the second finds it there (ynab_api/transfers.py says how).
description_2 may be empty: Bank #2's statements are then never matched.

Matching ignores case and extra spaces, and looks at the source's text_column
(sources.py), so it works for every source with no code of its own.

Nothing here knows about YNAB or where rules are stored: find_transfers() only
says which rows match, and where to. ynab_api/transfers.py imports them as
YNAB transfers.
"""

from dataclasses import asdict, dataclass

MAX_RULES = 50
MAX_DESCRIPTION_LENGTH = 100


@dataclass(frozen=True)
class TransferRule:
    account_1_id: str  # YNAB account id of Bank #1
    account_1_name: str  # e.g. "GCash - 1161", for the page
    description_1: str  # text Bank #1's rows contain, e.g. "Transfer to 0917123"
    account_2_id: str
    account_2_name: str
    description_2: str  # "" when Bank #2's statements aren't matched

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(text: str) -> str:
    """Text as rules compare it: "  Transfer  to 0917123 " -> "transfer to 0917123"."""
    return " ".join(text.split()).casefold()


def rules_from_dicts(records: list[dict] | None) -> list[TransferRule]:
    """Stored rules (TransferRule.to_dict() dicts) as TransferRules."""
    return [TransferRule(**record) for record in records or []]


def _sides(rules: list[TransferRule], account_id: str) -> list[tuple[str, str]]:
    """(normalized description, the other account's id) for each rule with a
    description on `account_id`'s side."""
    sides = []
    for rule in rules:
        if rule.account_1_id == account_id and rule.description_1:
            sides.append((normalize(rule.description_1), rule.account_2_id))
        elif rule.account_2_id == account_id and rule.description_2:
            sides.append((normalize(rule.description_2), rule.account_1_id))
    return sides


def find_transfers(
    rows: list[dict], source, account_id: str, rules: list[TransferRule]
) -> dict[int, str]:
    """Which rows of a statement imported into YNAB account `account_id` are
    transfers, and to which account.

    Returns {row index: the other YNAB account id}. The first matching rule wins.
    Rows the parser added to fix the balance (source.is_correction_row) never
    match: they aren't real transactions.

        rows = [{"Description": "Transfer to 0917123 (Juan)", ...}, {"Description": "Load", ...}]
        find_transfers(rows, SOURCES["gcash"], "acc-gcash", [rule above]) -> {0: "acc-gotyme"}
    """
    if not source.text_column:
        return {}
    sides = _sides(rules, account_id)
    matches = {}
    for index, row in enumerate(rows):
        if source.is_correction_row(row):
            continue
        text = normalize(str(row.get(source.text_column) or ""))
        for description, other_account in sides:
            if description in text:
                matches[index] = other_account
                break
    return matches


def clean_rules(
    raw: object, accounts: dict[str, str]
) -> tuple[list[TransferRule] | None, str | None]:
    """Check rules sent by the page: (rules, None), or (None, a message for the user).

    `raw` is a list of {"account_1_id", "description_1", "account_2_id",
    "description_2"} dicts. `accounts` is the plan's open accounts, {id: name}:
    both banks must be in it, and the names saved come from it, not the page.
    """
    if not isinstance(raw, list):
        return None, "Send the rules as a list."
    if len(raw) > MAX_RULES:
        return None, f"Save at most {MAX_RULES} transfer rules."
    rules = []
    seen = set()  # (account id, normalized description), across all rules
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return None, f"Rule {number} is not valid."
        account_1 = str(item.get("account_1_id") or "")
        account_2 = str(item.get("account_2_id") or "")
        description_1 = " ".join(str(item.get("description_1") or "").split())
        description_2 = " ".join(str(item.get("description_2") or "").split())
        if account_1 not in accounts or account_2 not in accounts:
            return None, f"Rule {number}: choose both banks."
        if account_1 == account_2:
            return None, f"Rule {number}: Bank #1 and Bank #2 must differ."
        if not description_1:
            return None, f"Rule {number}: enter Bank #1's description."
        if max(len(description_1), len(description_2)) > MAX_DESCRIPTION_LENGTH:
            return None, (
                f"Rule {number}: keep each description under {MAX_DESCRIPTION_LENGTH} characters."
            )
        sides = {(account_1, normalize(description_1))}
        if description_2:
            sides.add((account_2, normalize(description_2)))
        if sides & seen:
            return None, f"Rule {number} is the same as an earlier one."
        seen |= sides
        rule = TransferRule(
            account_1_id=account_1,
            account_1_name=accounts[account_1],
            description_1=description_1,
            account_2_id=account_2,
            account_2_name=accounts[account_2],
            description_2=description_2,
        )
        rules.append(rule)
    return rules, None
