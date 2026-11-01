"""
ph-statement-import - transfer rules: which statement rows are money moved
between your own wallets.

A rule says: in statements from one wallet, a row whose description contains
this text is a transfer to that other wallet.

    TransferRule(description="Transfer to 0917123", from_wallet="gcash", to_wallet="gotyme")

A wallet is one account at a source, by its id ("gcash", "gcash-2", ...;
web/transfer_rules.py says where each mode gets its wallets from). Matching
ignores case and extra spaces, and looks at the source's text_column
(sources.py), so it works for every source with no code of its own.

Nothing here knows about YNAB or where rules are stored: find_transfers() only
says which rows match. Importing them as YNAB transfers is up to the caller.
"""

from dataclasses import asdict, dataclass

MAX_RULES = 50
MAX_DESCRIPTION_LENGTH = 100


@dataclass(frozen=True)
class TransferRule:
    description: str  # text the row's description contains, e.g. "Transfer to 0917123"
    from_wallet: str  # wallet id of the statement the row is in
    to_wallet: str  # wallet id the money went to (or came from)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(text: str) -> str:
    """Text as rules compare it: "  Transfer  to 0917123 " -> "transfer to 0917123"."""
    return " ".join(text.split()).casefold()


def rules_from_dicts(records: list[dict]) -> list[TransferRule]:
    """Stored rules ({"description", "from_wallet", "to_wallet"} dicts) as TransferRules."""
    return [TransferRule(r["description"], r["from_wallet"], r["to_wallet"]) for r in records or []]


def find_transfers(
    rows: list[dict], source, wallet_id: str, rules: list[TransferRule]
) -> dict[int, str]:
    """Which rows of a statement from `wallet_id` are transfers, and to where.

    Returns {row index: destination wallet id}. The first matching rule wins.
    Rows the parser added to fix the balance (source.is_correction_row) never
    match: they aren't real transactions.

        rows = [{"Description": "Transfer to 0917123 (Juan)", ...}, {"Description": "Load", ...}]
        find_transfers(rows, SOURCES["gcash"], "gcash", [rule above]) -> {0: "gotyme"}
    """
    if not source.text_column:
        return {}
    wanted = [
        (normalize(rule.description), rule.to_wallet)
        for rule in rules
        if rule.from_wallet == wallet_id
    ]
    matches = {}
    for index, row in enumerate(rows):
        if source.is_correction_row(row):
            continue
        text = normalize(str(row.get(source.text_column) or ""))
        for description, to_wallet in wanted:
            if description in text:
                matches[index] = to_wallet
                break
    return matches


def clean_rules(raw: object, wallet_ids: set[str]) -> tuple[list[TransferRule] | None, str | None]:
    """Check rules sent by the page: (rules, None), or (None, a message for the user).

    `raw` is a list of {"description", "from_wallet", "to_wallet"} dicts, and
    both wallets must be in `wallet_ids`.
    """
    if not isinstance(raw, list):
        return None, "Send the rules as a list."
    if len(raw) > MAX_RULES:
        return None, f"Save at most {MAX_RULES} transfer rules."
    rules = []
    seen = set()
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return None, f"Rule {number} is not valid."
        description = " ".join(str(item.get("description") or "").split())
        from_wallet = str(item.get("from_wallet") or "")
        to_wallet = str(item.get("to_wallet") or "")
        if not description:
            return None, f"Rule {number}: enter the text the description contains."
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return None, f"Rule {number}: keep the text under {MAX_DESCRIPTION_LENGTH} characters."
        if from_wallet not in wallet_ids or to_wallet not in wallet_ids:
            return None, f"Rule {number}: choose the source and destination banks."
        if from_wallet == to_wallet:
            return None, f"Rule {number}: the source and destination banks must differ."
        key = (normalize(description), from_wallet)
        if key in seen:
            return None, f"Rule {number} is the same as an earlier one."
        seen.add(key)
        rules.append(TransferRule(description, from_wallet, to_wallet))
    return rules, None
