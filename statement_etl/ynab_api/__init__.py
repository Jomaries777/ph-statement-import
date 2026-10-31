"""
ph-statement-import - the YNAB destination: everything that knows about YNAB.

    format.py   - neutral Transaction -> YNAB transaction dict (milliunits, import_id)
    client.py   - SDK client, duplicate check, batch upload, ending balance check
    accounts.py - user, plans and accounts
    balance.py  - balance checks and a new account's Starting Balance
    categories.py - categories, uncategorized transactions, setting categories

Sources never import this package (see transaction.py).
"""
