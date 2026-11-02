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

Some moves need no rule: a GoTyme GoalSave row "Transfer to Deposit account"
is always money moved to the person's GoTyme Everyday account
(Source.own_transfers). find_transfers() matches those too, given the person's
YNAB account for each source (own_accounts). The Everyday row "Transfer from
GoalSave" doesn't say which GoalSave account, so with several its destination
is all of them, a tuple: it's linked only to a transfer one of them already
made (ynab_api/transfers.py).

Nothing here knows about YNAB or where rules are stored: find_transfers() only
says which rows match, and where to. ynab_api/transfers.py imports them as
YNAB transfers.
"""

from dataclasses import asdict, dataclass

from statement_etl.sources import SOURCES

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


def _own_sides(source, own_accounts: dict[str, list[str]]) -> list[tuple[str, str | tuple]]:
    """(normalized description, the other account) for the moves `source`'s
    rows make without a rule (Source.own_transfers), with `own_accounts`, the
    person's YNAB accounts per source: {"gotyme": ["acc-everyday"],
    "gotyme_goalsave": ["acc-22222", "acc-11111"]}.

    The other account is an id, or, when the row doesn't say which of several
    it is, a tuple of them. An empty tuple is any account: the person's
    accounts at that source aren't known (a manual upload).
    """
    sides = []
    own = source.own_transfers
    if own and own_accounts.get(own.other_source):  # GoalSave -> the Everyday account
        sides.append((normalize(own.description), own_accounts[own.other_source][0]))
    for other in SOURCES.values():  # Everyday -> one of the GoalSave accounts
        if other.own_transfers and other.own_transfers.other_source == source.key:
            accounts = tuple(own_accounts.get(other.key, ()))
            to = accounts[0] if len(accounts) == 1 else accounts
            sides.append((normalize(other.own_transfers.other_description), to))
    return sides


def find_transfers(
    rows: list[dict],
    source,
    account_id: str,
    rules: list[TransferRule],
    own_accounts: dict[str, list[str]] | None = None,
) -> dict[int, str | tuple]:
    """Which rows of a statement imported into YNAB account `account_id` are
    transfers, and to which account.

    Returns {row index: the other YNAB account id}. The first matching rule wins.
    Rows the parser added to fix the balance (source.is_correction_row) never
    match: they aren't real transactions.

        rows = [{"Description": "Transfer to 0917123 (Juan)", ...}, {"Description": "Load", ...}]
        find_transfers(rows, SOURCES["gcash"], "acc-gcash", [rule above]) -> {0: "acc-gotyme"}

    Then the moves that need no rule (Source.own_transfers), with
    `own_accounts` as in _own_sides(): an Everyday row "Transfer to GoalSave"
    with two GoalSave accounts -> {3: ("acc-22222", "acc-11111")}.
    """
    if not source.text_column:
        return {}
    sides = _sides(rules, account_id) + _own_sides(source, own_accounts or {})
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


def unlinked_message(source, count: int) -> str:
    """Why `count` rows of a transfer that names no account (an Everyday
    "Transfer to GoalSave") were imported as ordinary rows: the statement
    they're with wasn't imported first, so there was no transfer to link to.

    unlinked_message(SOURCES["gotyme"], 2) -> "2 GoalSave rows imported
    without a transfer. Import the GoTyme GoalSave statement first, so its
    transfers link up."
    """
    other = next(
        s
        for s in SOURCES.values()
        if s.own_transfers and s.own_transfers.other_source == source.key
    )
    label = other.own_transfers.other_description
    rows = f"1 {label} row" if count == 1 else f"{count} {label} rows"
    return (
        f"{rows} imported without a transfer. "
        f"Import the {other.name} statement first, so its transfers link up."
    )


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
        if not account_1 or not account_2:
            return None, f"Rule {number}: choose both banks."
        for side, account in ((1, account_1), (2, account_2)):
            if account not in accounts:
                # e.g. an account of another plan, or since closed in YNAB
                name = str(item.get(f"account_{side}_name") or "The account")
                return None, (
                    f"Rule {number}: {name} isn't an open account in this plan. "
                    f"Choose Bank #{side} again."
                )
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
