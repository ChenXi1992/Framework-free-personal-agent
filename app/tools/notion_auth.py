"""One-time Notion OAuth setup via mcp.notion.com.

Run this ONCE:

    python -m app.tools.notion_auth

It will:
1. Register a fresh OAuth client with mcp.notion.com (no Notion account setup needed).
2. Open your browser — log in and authorise access to your Notion workspace.
3. Save the token to data/notion_mcp_token.json.

notion.py reads this file automatically. No integration registration, no admin
rights — the Notion MCP server handles everything.

To re-auth: delete data/notion_mcp_token.json and run again.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MCP_BASE        = "https://mcp.notion.com"
REGISTER_URL    = f"{MCP_BASE}/register"
AUTH_URL        = f"{MCP_BASE}/authorize"
TOKEN_URL       = f"{MCP_BASE}/token"
REDIRECT_URI    = "http://localhost:8765/callback"

DATA_DIR        = Path(os.environ.get("NOTION_DATA_DIR", "./data"))
TOKEN_FILE      = DATA_DIR / "notion_mcp_token.json"


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Step 1 — dynamic client registration
# ---------------------------------------------------------------------------

def _register() -> tuple[str, str]:
    """Register a fresh OAuth client; returns (client_id, client_secret)."""
    resp = requests.post(
        REGISTER_URL,
        json={
            "redirect_uris": [REDIRECT_URI],
            "client_name": "me-agent",
            "grant_types": ["authorization_code", "refresh_token"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["client_id"], data["client_secret"]


# ---------------------------------------------------------------------------
# Step 2 — browser OAuth flow
# ---------------------------------------------------------------------------

def _browser_flow(client_id: str, client_secret: str) -> dict:
    """Open browser → catch redirect → exchange code → return token payload."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = urllib.parse.urlencode({
        "client_id":             client_id,
        "response_type":         "code",
        "redirect_uri":          REDIRECT_URI,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
    })
    auth_full_url = f"{AUTH_URL}?{auth_params}"

    result: dict = {}
    server_ready = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                result["code"] = params["code"][0]
                body = b"<html><body><h2>Authorised! You can close this tab.</h2></body></html>"
                self.send_response(200)
            else:
                err = params.get("error", ["unknown"])[0]
                result["error"] = err
                body = f"<html><body><h2>Error: {err}</h2></body></html>".encode()
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    httpd = HTTPServer(("localhost", 8765), _Handler)
    ready = threading.Event()

    def _serve():
        ready.set()
        httpd.handle_request()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    ready.wait()

    print(f"\nOpening browser for Notion login…")
    print(f"If it doesn't open, visit:\n  {auth_full_url}\n")
    webbrowser.open(auth_full_url)
    t.join(timeout=120)
    httpd.server_close()

    if "error" in result:
        sys.exit(f"OAuth error: {result['error']}")
    if "code" not in result:
        sys.exit("Timed out waiting for browser callback. Run the script again.")

    # Exchange code for token
    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type":    "authorization_code",
            "code":          result["code"],
            "redirect_uri":  REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_token() -> dict | None:
    """Return saved token dict, or None if not set up yet."""
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def refresh_access_token() -> str | None:
    """Use the saved refresh_token to get a new access_token.

    Updates the token file in place and returns the new access_token,
    or None if the refresh fails (e.g. refresh token also expired).
    """
    data = load_token()
    if not data:
        return None
    refresh_token = data.get("refresh_token")
    client_id     = data.get("_client_id")
    client_secret = data.get("_client_secret")
    if not all([refresh_token, client_id, client_secret]):
        return None

    try:
        resp = requests.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        return None

    new_data = resp.json()
    # Carry forward client credentials and any new refresh token
    new_data["_client_id"]     = client_id
    new_data["_client_secret"] = client_secret
    if "refresh_token" not in new_data:
        new_data["refresh_token"] = refresh_token  # some servers reuse the same one

    TOKEN_FILE.write_text(json.dumps(new_data, indent=2))
    return new_data.get("access_token")


def main() -> None:
    if TOKEN_FILE.exists():
        print(f"Token already saved at {TOKEN_FILE}.")
        print("Delete it and re-run to re-authenticate.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Registering OAuth client with mcp.notion.com…")
    client_id, client_secret = _register()
    print(f"  client_id: {client_id}")

    token_data = _browser_flow(client_id, client_secret)
    # Persist client credentials alongside the token so we can refresh later
    token_data["_client_id"]     = client_id
    token_data["_client_secret"] = client_secret

    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    print(f"\nSaved to {TOKEN_FILE}")
    print("Restart the bot — Notion tools are ready.")


if __name__ == "__main__":
    main()
