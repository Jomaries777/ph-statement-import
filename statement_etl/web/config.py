"""
ph-statement-import - web app settings that more than one web module needs.

Read once from the environment (.env is loaded here, before anything reads it):
    APP_PORT  - optional, default 5050 (5000 is used by macOS AirPlay)

REDIRECT_URI is where YNAB sends the browser back after OAuth login. It must
match the redirect URI of your YNAB OAuth application exactly.
"""

import os

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]

load_dotenv()  # load .env file if present, so env vars can be set there instead of the shell

PORT = int(os.environ.get("APP_PORT", "5050"))
REDIRECT_URI = f"http://localhost:{PORT}/oauth/callback"
