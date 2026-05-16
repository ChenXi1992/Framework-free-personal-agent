"""One-time Gmail OAuth setup.

Run this ONCE on a machine with a browser:

    python -m app.tools.gmail_auth

It will:
1. Read `./data/gmail_credentials.json` (OAuth client config from Google Cloud).
2. Open a browser tab for you to grant access.
3. Save `./data/gmail_token.json` (refresh token + scopes).

After that, copy `gmail_token.json` to your NAS's `./data/` directory along
with `gmail_credentials.json`. The bot will refresh the token automatically
on every run; it never needs the browser again.

To re-auth (e.g., scope changed, refresh token revoked): delete
`gmail_token.json` and run this script again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Broad scope: read, send, modify, delete. Matches the user's "full read/write"
# requirement. Narrow this later if you want a tighter blast radius.
SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

DATA_DIR = Path(os.environ.get("GMAIL_DATA_DIR", "./data"))
CREDENTIALS_FILE = DATA_DIR / "gmail_credentials.json"
TOKEN_FILE = DATA_DIR / "gmail_token.json"


def load_credentials() -> Credentials | None:
    """Return cached creds, refreshed if needed. None if first-run setup is required."""
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        except Exception:  # noqa: BLE001
            # Scope change or revoked token — force a fresh OAuth flow.
            TOKEN_FILE.unlink(missing_ok=True)
            return None
    return creds if creds and creds.valid else None


def run_oauth_flow() -> Credentials:
    """Interactive browser flow. Prints next steps on completion."""
    if not CREDENTIALS_FILE.exists():
        sys.exit(
            f"Missing {CREDENTIALS_FILE}.\n"
            "Download an OAuth client (type: Desktop app) from Google Cloud Console:\n"
            "  https://console.cloud.google.com/apis/credentials\n"
            f"and save it as {CREDENTIALS_FILE}."
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"\nSaved {TOKEN_FILE}.")
    print("Copy this file (and gmail_credentials.json) to your NAS's ./data/ directory.")
    return creds


def main() -> None:
    creds = load_credentials()
    if creds:
        print(f"Existing token in {TOKEN_FILE} is still valid. Nothing to do.")
        return
    run_oauth_flow()


if __name__ == "__main__":
    main()
