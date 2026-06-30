"""SQLite persistence layer.

Uses WAL mode so concurrent reads/writes from the bot and any background
tasks don't block each other.

Every table stores two timestamp representations:
  ts         INTEGER  — Unix epoch seconds (fast to sort/index, compact)
  created_at TEXT     — 'YYYY-MM-DD HH:MM:SS' UTC (human-readable in DB Browser)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' UTC
    user_id     INTEGER NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content     TEXT    NOT NULL,
    meta_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_user_ts
    ON messages(user_id, ts);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' UTC
    raw_text    TEXT    NOT NULL,
    category    TEXT    NOT NULL,          -- agent name or 'uncategorized' — no CHECK constraint so new agents work automatically
    summary     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_category_ts
    ON notes(category, ts);


CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' UTC
    agent       TEXT    NOT NULL,
    sentiment   TEXT    NOT NULL CHECK (sentiment IN ('positive','negative','neutral')),
    note        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    agent       TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    days        TEXT    NOT NULL,  -- comma-separated: "wednesday,friday" or "daily"
    fire_time   TEXT    NOT NULL,  -- "HH:MM" in user's local timezone
    active      INTEGER NOT NULL DEFAULT 1,
    last_fired  TEXT               -- "YYYY-MM-DD" — prevents double-fire on same day
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT    NOT NULL UNIQUE,   -- one active summary per agent
    ts_from     INTEGER NOT NULL,          -- unix ts of oldest turn covered
    ts_to       INTEGER NOT NULL,          -- unix ts of newest turn covered
    turns_count INTEGER NOT NULL,          -- how many turns were summarised
    summary     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
"""

# Migrations: ALTER TABLE statements to add new columns to databases that were
# created before the created_at / date_iso / datetime_utc columns existed.
# ALTER TABLE ADD COLUMN is idempotent in SQLite as long as we catch the
# "duplicate column" error. Each entry is (table, column_definition).
_MIGRATIONS = [
    ("messages",            "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("messages",            "agent       TEXT"),          # which specialist handled this turn
    ("notes",               "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("notes",               "role        TEXT NOT NULL DEFAULT 'user'"),  # 'user' or 'assistant'

    ("feedback",            "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    # One-time reminders (reminder_once tool)
    ("reminders",           "once        INTEGER NOT NULL DEFAULT 0"),  # 1 = one-time, 0 = recurring
    ("reminders",           "fire_at     INTEGER"),                     # Unix ts; only for once=1
]


def _utc_now() -> str:
    """Current UTC time as 'YYYY-MM-DD HH:MM:SS' — SQLite's native datetime format."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _connect() -> sqlite3.Connection:
    """Open the SQLite database in WAL + autocommit mode.

    WAL mode allows concurrent reads alongside a write, which matters
    because the bot's Telegram handler and any background tasks may
    hit the DB simultaneously.  isolation_level=None turns off Python's
    implicit transaction management so every statement commits immediately.
    """
    # Ensure the parent directory exists (matters when DB_PATH is /data/me.db
    # in the container and the volume is freshly mounted).
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None disables Python's implicit transaction management,
    # making every statement auto-commit immediately. This pairs with WAL mode:
    # without autocommit, a long-running read transaction would prevent WAL
    # checkpointing and cause the WAL file to grow unboundedly. The tradeoff
    # is that multi-statement operations are NOT atomic — if we need atomicity
    # we must wrap them in explicit BEGIN/COMMIT blocks.
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    # WAL (Write-Ahead Log) allows concurrent readers alongside a single writer.
    # Without WAL, any write locks out all readers — a problem when the Telegram
    # handler and a background migration run simultaneously.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # busy_timeout: when another connection holds the write lock, wait up to N ms
    # for it to free up instead of raising "database is locked" immediately. The
    # agent loop runs in a worker thread while the event loop also writes (logging,
    # tagging), so brief contention is normal — block-and-retry, don't fail.
    conn.execute("PRAGMA busy_timeout=5000;")  # 5 seconds
    return conn


def _migrate_notes_categories(conn: sqlite3.Connection) -> None:
    """Drop the hardcoded CHECK constraint from the notes.category column.

    The original schema had CHECK (category IN ('workout','lifestyle',...)) which
    breaks whenever a new agent is added. Since agents are now discovered
    dynamically, the constraint is removed entirely — any string is a valid
    category. This migration is a no-op once the constraint is gone.
    """
    notes_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes'"
    ).fetchone()
    old_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_notes_old'"
    ).fetchone()

    # Crash-recovery: a previous migration renamed notes → _notes_old but never
    # finished (e.g. process killed mid-script). Restore the backup first.
    if old_row and not notes_row:
        conn.execute("ALTER TABLE _notes_old RENAME TO notes")
        notes_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes'"
        ).fetchone()
        old_row = None  # consumed — no longer present

    # Both tables exist: _notes_old is leftover garbage from a previous crashed
    # migration that got partway through (renamed notes but crashed before DROP).
    # The notes table is already the new schema — just drop the stale backup.
    if old_row and notes_row:
        conn.execute("DROP TABLE _notes_old")
        old_row = None

    # If the CHECK constraint is gone already, nothing to do.
    if notes_row and "CHECK" not in notes_row[0]:
        return

    conn.executescript("""
        ALTER TABLE notes RENAME TO _notes_old;

        CREATE TABLE notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,
            created_at  TEXT    NOT NULL,
            raw_text    TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            summary     TEXT    NOT NULL
        );

        INSERT INTO notes SELECT * FROM _notes_old;
        DROP TABLE _notes_old;

        CREATE INDEX IF NOT EXISTS idx_notes_category_ts
            ON notes(category, ts);
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any missing timestamp columns and repair stale sentinel values.

    Step 1 — ALTER TABLE ADD COLUMN: adds the column if it doesn't exist yet.
    The column gets a literal-string DEFAULT ('1970-01-01 00:00:00') because
    SQLite's ALTER TABLE ADD COLUMN does not accept expression defaults like
    CURRENT_TIMESTAMP.

    Step 2 — backfill: runs on EVERY startup (not just when the column is
    newly added). This repairs rows that were inserted by old code after the
    column existed but before the Python INSERT statements were updated — they
    land with the '1970' sentinel and need to be fixed from the unix `ts`.
    """
    # Step 1: add columns that don't exist yet (safe to re-run; errors ignored).
    for table, col_def in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists — that's fine

    # Step 2: repair any '1970' sentinel rows — runs every boot.
    for table in ("messages", "notes", "feedback"):
        conn.execute(
            f"UPDATE {table} SET created_at = datetime(ts, 'unixepoch') "
            f"WHERE created_at IS NULL OR created_at = '1970-01-01 00:00:00'"
        )


def init() -> None:
    """Create tables and apply any pending schema migrations. Safe to call on every boot."""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_notes_categories(conn)
        _migrate(conn)
        _migrate_drop_agent_conversations(conn)


def _migrate_drop_agent_conversations(conn: sqlite3.Connection) -> None:
    """Retire the legacy agent_conversations table into messages, then drop it.

    Older turns lived only in agent_conversations (before messages.agent existed).
    Before dropping, copy any turn whose (role, content) isn't already in messages
    so the agent-history queries (which now read messages only) don't lose context.
    Idempotent: no-op once the table is gone.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversations'"
    ).fetchone()
    if not exists:
        return
    # Copy orphan turns (present in agent_conversations, absent from messages) into
    # messages with the agent tag preserved. user_id is unknown for these legacy
    # rows, so use 0 — they are only ever read back by agent (not by user_id).
    conn.execute(
        """
        INSERT INTO messages (ts, created_at, user_id, role, content, agent)
        SELECT ac.ts, ac.created_at, 0, ac.role, ac.content, ac.agent
        FROM agent_conversations ac
        WHERE NOT EXISTS (
            SELECT 1 FROM messages m
            WHERE m.role = ac.role AND m.content = ac.content
        )
        """
    )
    conn.execute("DROP TABLE IF EXISTS agent_conversations")
    log.info("Migrated and dropped legacy agent_conversations table")


def log_message(
    user_id: int,
    role: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> int:
    """Append one turn to the conversation log. Returns the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages(ts, created_at, user_id, role, content, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                _utc_now(),
                user_id,
                role,
                content,
                json.dumps(meta or {}),
            ),
        )
        return cur.lastrowid


def tag_last_exchange(user_id: int, agent: str) -> None:
    """Tag the most recent user+assistant pair in messages with the routing agent.

    Called from main.py after dispatch completes, when the agent name is known.
    Uses the two most-recently inserted rows for this user — which are always the
    current exchange since the bot processes one message at a time.

    All agent-history queries read from messages WHERE agent=?. This is the
    single source of truth — the legacy agent_conversations table was removed.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE messages SET agent = ?
            WHERE user_id = ?
              AND id IN (
                  SELECT id FROM messages
                  WHERE user_id = ?
                  ORDER BY ts DESC LIMIT 2
              )
            """,
            (agent, user_id, user_id),
        )


def tag_message(message_id: int, agent: str) -> None:
    """Tag exactly one message row with an agent. Used for system notes
    (e.g. confirmation receipts) where tagging the surrounding pair is wrong."""
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET agent = ? WHERE id = ?",
            (agent, message_id),
        )


def recent_history(user_id: int, limit: int = 20) -> list[dict[str, str]]:
    """Return the last `limit` turns in chronological order, OpenAI message format."""
    with _connect() as conn:
        rows: Iterable[tuple[str, str]] = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(list(rows))]


def log_note(category: str, role: str, text: str) -> int:
    """Append one turn to the notes table under the given agent category.

    Called automatically by main.py after every exchange — both the user
    message (role='user') and the agent response (role='assistant') are stored.
    Agents no longer need to call note_add for raw logging; the system does it.

    Returns the new row id.
    """
    summary = text[:120]
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes(ts, created_at, raw_text, category, summary, role) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), _utc_now(), text, category, summary, role),
        )
        return cur.lastrowid
