"""Extract Notion session token from your browser and save it for the agent.

This uses the unofficial `token_v2` cookie that Notion's own web app uses.
No integration registration needed — works even in locked-down company workspaces.

Run once:
    python -m app.tools.notion_cookie_auth

It will:
1. Try to read the token directly from your Chrome/Safari/Firefox cookie store.
2. If that fails, open the Notion website and print step-by-step instructions
   to copy the token manually.
3. Save the token to .env as NOTION_TOKEN_PERSONAL (or _WORK with --workspace=work).

The agent uses this token exactly like an integration token — notion_client
accepts both formats.

Caveats
-------
- token_v2 is a session cookie. It expires when you log out of Notion in
  your browser, or after ~90 days of inactivity. Re-run this script to refresh.
- This is unofficial. It works today (2025) but Notion could change it.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Cookie extraction helpers
# ---------------------------------------------------------------------------

def _chrome_cookie_db() -> Path | None:
    candidates = [
        Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies",
        Path.home() / "Library/Application Support/Google/Chrome/Profile 1/Cookies",
        Path.home() / ".config/google-chrome/Default/Cookies",
        Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Cookies",  # Windows
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _firefox_cookie_db() -> Path | None:
    profiles_dir = Path.home() / "Library/Application Support/Firefox/Profiles"
    if not profiles_dir.exists():
        profiles_dir = Path.home() / ".mozilla/firefox"
    if profiles_dir.exists():
        for p in profiles_dir.iterdir():
            db = p / "cookies.sqlite"
            if db.exists():
                return db
    return None


def _read_sqlite_cookie(db_path: Path, host: str, name: str) -> str | None:
    """Copy the db to a temp file first (Chrome locks the original)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(str(tmp_path))
        # Chrome schema
        try:
            row = conn.execute(
                "SELECT value FROM cookies WHERE host_key LIKE ? AND name = ?",
                (f"%{host}%", name),
            ).fetchone()
            if row:
                return row[0]
        except sqlite3.OperationalError:
            pass
        # Firefox schema
        try:
            row = conn.execute(
                "SELECT value FROM moz_cookies WHERE host LIKE ? AND name = ?",
                (f"%{host}%", name),
            ).fetchone()
            if row:
                return row[0]
        except sqlite3.OperationalError:
            pass
        conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)
    return None


def _try_extract_from_browser() -> str | None:
    """Attempt automatic extraction from Chrome or Firefox cookie store."""
    for db_fn in (_chrome_cookie_db, _firefox_cookie_db):
        db = db_fn()
        if db is None:
            continue
        val = _read_sqlite_cookie(db, "notion.so", "token_v2")
        if val:
            # Chrome on macOS encrypts cookie values — if the value starts
            # with "v10" or "v11" it's encrypted and we can't decode it here
            # without Keychain access. Return None so we fall back to manual.
            if val.startswith(("v10", "v11")):
                print(
                    f"  Found token_v2 in {db} but it is encrypted by Chrome.\n"
                    "  Falling back to manual instructions below."
                )
                return None
            return val
    return None


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------

ENV_PATH = Path(".env")


def _write_to_env(key: str, value: str) -> None:
    """Insert or replace `key=value` in .env."""
    if not ENV_PATH.exists():
        ENV_PATH.write_text(f"{key}={value}\n")
        print(f"Created .env with {key}.")
        return

    lines = ENV_PATH.read_text().splitlines(keepends=True)
    new_lines = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"\n{key}={value}\n")

    ENV_PATH.write_text("".join(new_lines))
    print(f"{'Updated' if replaced else 'Added'} {key} in .env")


# ---------------------------------------------------------------------------
# Manual fallback instructions
# ---------------------------------------------------------------------------

MANUAL_INSTRUCTIONS = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Manual token extraction (30 seconds)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. The Notion website should have opened in your browser.
   If not, go to: https://www.notion.so

2. Make sure you are logged in.

3. Open DevTools:
   • Chrome/Edge: Cmd+Option+I  (Mac)  or  F12  (Win/Linux)
   • Firefox:     Cmd+Option+I  (Mac)  or  F12  (Win/Linux)

4. Click the "Application" tab  (Chrome/Edge)
   or the "Storage" tab  (Firefox)

5. In the left panel expand:
   Cookies → https://www.notion.so

6. Click the row named  token_v2

7. Copy the full value from the bottom panel (it's long).

8. Paste it below when prompted.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def _prompt_manual() -> str:
    print(MANUAL_INSTRUCTIONS)
    webbrowser.open("https://www.notion.so")
    token = input("Paste your token_v2 value here: ").strip()
    if not token:
        sys.exit("No token entered. Aborting.")
    # Browsers copy the cookie value URL-encoded (%3A instead of :, etc.).
    # Decode it so notion_client receives a plain token string.
    return urllib.parse.unquote(token)


# ---------------------------------------------------------------------------
# Verify the token works
# ---------------------------------------------------------------------------

def _verify(token: str) -> bool:
    try:
        from notion_client import Client
        c = Client(auth=token)
        result = c.search(query="", page_size=1)
        return isinstance(result, dict)
    except Exception as e:  # noqa: BLE001
        print(f"  Verification failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Save Notion browser token to .env")
    parser.add_argument(
        "--workspace",
        choices=["personal", "work"],
        default="personal",
        help="Which workspace slot to populate (default: personal)",
    )
    args = parser.parse_args()
    env_key = f"NOTION_TOKEN_{args.workspace.upper()}"

    print(f"\nNotion token extractor  (workspace: {args.workspace})\n")

    # 1. Try automatic extraction
    print("Trying automatic extraction from browser cookies…")
    token = _try_extract_from_browser()

    if token:
        print(f"  Found token_v2 automatically.")
    else:
        print("  Automatic extraction not available — switching to manual.")
        token = _prompt_manual()

    # 2. Verify
    print("\nVerifying token against Notion API…")
    if _verify(token):
        print("  Token works.")
    else:
        print(
            "  Could not verify (notion_client may not be installed, or the token is wrong).\n"
            "  Saving anyway — check by restarting the bot."
        )

    # 3. Save
    _write_to_env(env_key, token)
    print(
        f"\nDone. Restart the bot — it will pick up {env_key} from .env automatically.\n"
        "Note: this token expires when you log out of Notion in your browser.\n"
        "Re-run this script if the bot starts returning auth errors."
    )


if __name__ == "__main__":
    main()
