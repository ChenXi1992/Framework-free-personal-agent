"""Pending-action queue for destructive tool calls.

Pattern: a destructive tool (send_email, archive_page, etc.) does NOT execute
when the LLM calls it. Instead it stores the intended action in the
`pending_actions` table and returns the new row's ID + a human-readable
preview. The bot surfaces the preview to the user; the user types
`/confirm <id>` (or `/cancel <id>`) and the action either executes or is
discarded.

This keeps "I want the LLM to be able to send mail" and "I never want the LLM
to send mail without me seeing it" simultaneously true.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from ..db import _utc_now

from ..db import _connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id          TEXT PRIMARY KEY,           -- short uuid4 hex prefix
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,           -- 'YYYY-MM-DD HH:MM:SS' UTC
    user_id     INTEGER NOT NULL,
    tool_name   TEXT NOT NULL,
    arguments   TEXT NOT NULL,              -- json
    preview     TEXT NOT NULL,              -- human summary the user reads
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','confirmed','cancelled','executed','failed')),
    result      TEXT
);
"""


def init() -> None:
    """Create the pending_actions table and repair any missing timestamp values."""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # Add created_at if the table predates it (safe to re-run; error ignored).
        try:
            conn.execute(
                "ALTER TABLE pending_actions ADD COLUMN "
                "created_at TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"
            )
        except Exception:
            pass  # column already exists
        # Repair sentinel values — runs every boot so rows inserted by old code
        # between migrations are also fixed.
        conn.execute(
            "UPDATE pending_actions SET created_at = datetime(ts, 'unixepoch') "
            "WHERE created_at IS NULL OR created_at = '1970-01-01 00:00:00'"
        )


def stage_action(
    *,
    user_id: int,
    tool_name: str,
    arguments: dict[str, Any],
    preview: str,
) -> str:
    """Record a pending action and return its short ID."""
    action_id = uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pending_actions(id, ts, created_at, user_id, tool_name, arguments, preview) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                int(time.time()),
                _utc_now(),
                user_id,
                tool_name,
                json.dumps(arguments),
                preview,
            ),
        )
    return action_id


def get(action_id: str) -> dict[str, Any] | None:
    """Fetch a pending action by its short ID, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, user_id, tool_name, arguments, preview, status, result "
            "FROM pending_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "tool_name": row[2],
        "arguments": json.loads(row[3]),
        "preview": row[4],
        "status": row[5],
        "result": row[6],
    }


def mark(action_id: str, status: str, result: str | None = None) -> None:
    """Update the status (and optional result) of a pending action in place."""
    with _connect() as conn:
        conn.execute(
            "UPDATE pending_actions SET status = ?, result = ? WHERE id = ?",
            (status, result, action_id),
        )


def latest_pending_for(user_id: int) -> dict[str, Any] | None:
    """Return the most recently-staged still-pending action for this user.

    Used by /confirm and /cancel with no arguments — saves the user from
    typing the 8-char action_id when only one thing is awaiting confirmation
    (the common case).
    """
    with _connect() as conn:
        # rowid is SQLite's monotonic insertion counter, so it tie-breaks
        # actions staged within the same wall-clock second deterministically.
        row = conn.execute(
            "SELECT id, user_id, tool_name, arguments, preview, status, result "
            "FROM pending_actions WHERE user_id = ? AND status = 'pending' "
            "ORDER BY ts DESC, rowid DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "tool_name": row[2],
        "arguments": json.loads(row[3]),
        "preview": row[4],
        "status": row[5],
        "result": row[6],
    }
