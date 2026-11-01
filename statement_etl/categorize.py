"""
ph-statement-import - give YNAB's uncategorized transactions a category, picked by Gemini.

Run it after importing a statement: imported transactions arrive in YNAB
without a category unless YNAB knows the payee. This asks Gemini to pick one
from your own YNAB categories for each of them. The hosted web app and
auto-import can also do it right after an import, for only the transactions
that import created (categorize_transactions(), opt-in).

How it works:
    1. Read   - the plan's categories, and the transactions YNAB
                lists as uncategorized                         (ynab_api.categories)
    2. Ask    - send Gemini the category names and each
                transaction's date, account, payee, memo and
                amount; it picks one category for each, or
                "unsure", and says how sure it is (0-100)      (suggest_categories ->
                                                                gemini.generate_json)
    3. Check  - keep only picks that are one of your categories
                and that Gemini is at least MIN_CONFIDENCE
                sure of                                        (suggest_categories)
    4. Save   - set those categories in YNAB in one call.
                Nothing gets approved: you still review each
                transaction in YNAB                            (ynab_api.categories.set_categories)
    5. Output - print each transaction with its new category   (main)

What goes to Google: your category and group names, and each uncategorized
transaction's date, account name, payee, memo and amount. Not your YNAB token,
balances or anything else. On Gemini's free tier, Google may use what you send
to improve its products; see https://ai.google.dev/gemini-api/terms.

Requirements:
    pip install ynab

Environment:
    BEARER_TOKEN    - YNAB personal access token
    YNAB_PLAN_ID    - YNAB plan (budget) id (or pass --plan-id)
    GEMINI_API_KEY  - from Google AI Studio (https://aistudio.google.com/apikey)
    GEMINI_MODEL    - optional, default gemini.DEFAULT_MODEL

Usage:
    python -m statement_etl.categorize --dry-run                  # show Gemini's picks only
    python -m statement_etl.categorize                            # set them in YNAB
    python -m statement_etl.categorize --account-id ID --since 2026-10-01
"""

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date

import ynab
from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

from statement_etl import gemini
from statement_etl.pipeline import _fail, _print_table
from statement_etl.ynab_api.categories import (
    get_categories,
    get_uncategorized_transactions,
    set_categories,
)

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell

# Gemini's answer when no category fits well; that transaction is left uncategorized
UNSURE = "unsure"

# How sure (0-100) Gemini must say it is for a pick to be saved; below it, the
# transaction is left uncategorized. It's Gemini's own estimate, not a measured
# accuracy, so this is the one number to tune if it guesses too much or too little.
MIN_CONFIDENCE = 80

# Transactions per Gemini request, so a long list doesn't make one huge answer
BATCH_SIZE = 100

PROMPT = """\
You categorize transactions in a YNAB budget. The person lives in the Philippines;
amounts are in pesos, negative for money out and positive for money in.

For each transaction, pick the one category from the list that fits it best.
Answer "{unsure}" if none fits well, or if you can't tell what the payee is.
Income usually goes to "Inflow: Ready to Assign".
For each pick, also give your confidence: how sure you are, from 0 to 100,
that the category is right.

Categories, as "Group / Category":
{categories}

Transactions (JSON):
{transactions}
"""


@dataclass
class Suggestion:
    """Gemini's pick for one transaction."""

    transaction: dict  # from ynab_api.categories.get_uncategorized_transactions()
    category: dict | None  # from ynab_api.categories.get_categories(); None if unsure
    confidence: int | None = None  # how sure Gemini said it was, 0-100


# =============================================================================
# STEPS - ask Gemini, then save its picks (REUSABLE)
# =============================================================================


def category_label(category):
    """How a category is shown to Gemini and printed, with its group, so two
    categories with the same name in different groups stay apart:
    {"name": "Groceries", "group": "Everyday"} -> "Everyday / Groceries"."""
    return f"{category['group']} / {category['name']}"


def suggest_categories(
    transactions, categories, api_key, model=None, deadline=None
) -> list[Suggestion]:
    """Ask Gemini to pick a category for each transaction, BATCH_SIZE at a time.

    transactions - from ynab_api.categories.get_uncategorized_transactions()
    categories   - from ynab_api.categories.get_categories()
    deadline     - optional time.monotonic() value to give up by (gemini.py)

    Returns one Suggestion per transaction, in the same order. A pick that
    isn't one of the categories, is "unsure", or that Gemini is less than
    MIN_CONFIDENCE sure of becomes category=None.
    Raises gemini.GeminiError if Gemini fails.
    """
    by_label = {category_label(c): c for c in categories}
    suggestions = []
    for start in range(0, len(transactions), BATCH_SIZE):
        batch = transactions[start : start + BATCH_SIZE]
        picks = _ask_gemini(batch, list(by_label), api_key, model, deadline)
        for index, transaction in enumerate(batch):
            label, confidence = picks.get(index, (None, None))
            sure = confidence is not None and confidence >= MIN_CONFIDENCE
            category = by_label.get(label) if sure else None
            suggestions.append(Suggestion(transaction, category, confidence))
    return suggestions


def _ask_gemini(transactions, labels, api_key, model, deadline):
    """One Gemini request: {transaction's index in the list: (category label, confidence)}.

    Transactions are numbered 0, 1, 2, ... rather than sent with their YNAB
    ids: a short number is harder to get wrong, and the ids stay in YNAB.
    """
    prompt = PROMPT.format(
        unsure=UNSURE,
        categories="\n".join(labels),
        transactions=json.dumps(
            [
                {
                    "index": index,
                    "date": t["date"],
                    "account": t["account"],
                    "payee": t["payee"],
                    "memo": t["memo"],
                    "amount": str(t["amount"]),
                }
                for index, t in enumerate(transactions)
            ],
            ensure_ascii=False,
        ),
    )
    # The answer can only use these labels, and must cover each index
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "index": {"type": "INTEGER"},
                "category": {"type": "STRING", "enum": [*labels, UNSURE]},
                "confidence": {"type": "INTEGER"},
            },
            "required": ["index", "category", "confidence"],
        },
    }
    answer = gemini.generate_json(prompt, schema, api_key, model, deadline=deadline)
    if not isinstance(answer, list):
        raise gemini.GeminiError("Gemini's answer wasn't a list.")
    # A pick without a confidence counts as unsure (None)
    return {
        pick["index"]: (pick["category"], _confidence(pick.get("confidence")))
        for pick in answer
        if isinstance(pick, dict) and "index" in pick and "category" in pick
    }


def _confidence(value) -> int | None:
    """Gemini's confidence as an int 0-100, or None if it isn't a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, min(100, int(value)))


def categorize_transactions(
    plan_id,
    transactions,
    gemini_api_key,
    access_token=None,
    model=None,
    dry_run=False,
    deadline=None,
) -> tuple[list[Suggestion], int]:
    """ph-statement-import step: categorize these transactions with Gemini
    (steps 2-4 above; step 1's categories are read here too).

        created = ynab_api.categories.uncategorized_created(imported.created)
        suggestions, updated = categorize_transactions(plan_id, created, api_key, token)
        len(suggestions), updated  ->  (286, 240)   # 46 left uncategorized

    transactions - transaction dicts (ynab_api.categories.transaction_dict()).
    Only these are changed, never anything else in the plan. With dry_run,
    nothing is saved and updated is 0. deadline: see suggest_categories().

    Raises ynab.ApiException if YNAB rejects a request, gemini.GeminiError if
    Gemini fails. Nothing is saved unless Gemini answered for every batch.
    """
    if not transactions:
        return [], 0
    categories = get_categories(plan_id, access_token)

    # 2 + 3. Ask Gemini, keeping only sure picks that are real categories
    suggestions = suggest_categories(transactions, categories, gemini_api_key, model, deadline)

    # 4. Save the picks in one call; the others stay uncategorized
    if dry_run:
        return suggestions, 0
    category_ids = {s.transaction["id"]: s.category["id"] for s in suggestions if s.category}
    return suggestions, set_categories(plan_id, category_ids, access_token)


def categorize_uncategorized(
    plan_id,
    gemini_api_key,
    account_id=None,
    since_date=None,
    access_token=None,
    model=None,
    dry_run=False,
) -> tuple[list[Suggestion], int]:
    """ph-statement-import step: categorize the plan's uncategorized
    transactions with Gemini (steps 1-4 above).

        suggestions, updated = categorize_uncategorized(plan_id, api_key)
        len(suggestions), updated  ->  (12, 10)   # 2 left uncategorized

    account_id and since_date narrow which transactions (see
    ynab_api.categories.get_uncategorized_transactions()). With dry_run,
    nothing is saved and updated is 0.

    Raises ynab.ApiException if YNAB rejects a request, gemini.GeminiError if
    Gemini fails. Nothing is saved unless Gemini answered for every batch.
    """
    # 1. Read: the transactions without a category (and, in there, the categories)
    transactions = get_uncategorized_transactions(plan_id, account_id, since_date, access_token)
    return categorize_transactions(
        plan_id, transactions, gemini_api_key, access_token, model, dry_run=dry_run
    )


# =============================================================================
# MAIN - read settings from the command line / .env, run, print (REUSABLE)
# =============================================================================


def main():
    """Command-line entry point: categorize, then print what Gemini picked."""
    ap = argparse.ArgumentParser(
        description="Give YNAB's uncategorized transactions a category picked by Gemini."
    )
    ap.add_argument(
        "--plan-id",
        default=os.environ.get("YNAB_PLAN_ID"),
        help="YNAB plan id (default: $YNAB_PLAN_ID)",
    )
    ap.add_argument("--account-id", help="Only this YNAB account's transactions (default: all)")
    ap.add_argument(
        "--since",
        type=date.fromisoformat,
        help="Only transactions on or after this date, YYYY-MM-DD (default: the last year)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show Gemini's picks; don't change YNAB")
    args = ap.parse_args()

    if not args.plan_id:
        ap.error("--plan-id or YNAB_PLAN_ID is required")
    if "BEARER_TOKEN" not in os.environ:
        ap.error("BEARER_TOKEN environment variable is not set")
    # The API key always comes from the environment, never the command line
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        ap.error("GEMINI_API_KEY environment variable is not set")

    try:
        suggestions, updated = categorize_uncategorized(
            args.plan_id,
            api_key,
            account_id=args.account_id,
            since_date=args.since,
            dry_run=args.dry_run,
        )
    except ynab.ApiException as e:
        _fail(f"YNAB returned {e.status} {e.reason}\n{e.body or ''}")
    except gemini.GeminiError as e:
        _fail(str(e))

    if not suggestions:
        print("No uncategorized transactions in YNAB.")
        return
    _print_table(
        ("Date", "Account", "Payee", "Amount", "Category", "Sure"),
        [
            (
                s.transaction["date"],
                s.transaction["account"],
                s.transaction["payee"],
                f"{s.transaction['amount']:,.2f}",
                category_label(s.category) if s.category else "(unsure, left as is)",
                "" if s.confidence is None else f"{s.confidence}%",
            )
            for s in suggestions
        ],
        right_aligned=("Amount", "Sure"),
    )
    picked = sum(1 for s in suggestions if s.category)
    if args.dry_run:
        print(f"Dry run: Gemini picked a category for {picked} of {len(suggestions)}.")
        print("Nothing was changed in YNAB.")
    else:
        print(f"Categorized {updated} of {len(suggestions)} transactions in YNAB.")
        print("Their approval didn't change: review them in YNAB.")


if __name__ == "__main__":
    main()
