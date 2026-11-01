"""
ph-statement-import - rate limits for hosted mode (Flask-Limiter).

    limiter            - set up in app.py; on only in hosted mode (config.HOSTED)
    PARSE_LIMIT        - POST /api/parse, the most expensive request (reads a PDF)
    CATEGORIZE_LIMIT   - POST /api/categorize (asks Gemini, on the server's key)
    OAUTH_LIMIT        - /oauth/login, /oauth/callback and /oauth/logout
    AUTO_IMPORT_LIMIT  - /google/* and /api/auto-import*

Counted per client IP. On Cloud Run that's the X-Forwarded-For address, which
ProxyFix in app.py puts in request.remote_addr. The counts are kept in this
process's memory, which is enough for the one instance hosted mode runs.

A blocked request gets a 429 JSON error (app.py) before its route runs, so it
costs no parsing work.
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from statement_etl.web.config import HOSTED

PARSE_LIMIT = "10 per minute"
CATEGORIZE_LIMIT = "5 per minute"
OAUTH_LIMIT = "20 per minute"
AUTO_IMPORT_LIMIT = "20 per minute"

limiter = Limiter(get_remote_address, storage_uri="memory://", enabled=HOSTED)
