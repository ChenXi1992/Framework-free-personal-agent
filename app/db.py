"""SQLite persistence layer.

Uses WAL mode so concurrent reads/writes from the bot and any background
tasks don't block each other.

Every table stores two timestamp representations:
  ts         INTEGER  — Unix epoch seconds (fast to sort/index, compact)
  created_at TEXT     — 'YYYY-MM-DD HH:MM:SS' UTC (human-readable in DB Browser)

Health tables use the same pattern:
  date       TEXT     — 'YYYYMMDD' (compact date format, used as primary key)
  date_iso   TEXT     — 'YYYY-MM-DD' (readable equivalent of date)
  datetime_utc TEXT   — 'YYYY-MM-DD HH:MM:SS' UTC (for heart_rate / sport_minute)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

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

CREATE TABLE IF NOT EXISTS agent_conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' UTC
    agent       TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('user','assistant')),
    content     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_conv_agent_ts
    ON agent_conversations(agent, ts);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' UTC
    agent       TEXT    NOT NULL,
    sentiment   TEXT    NOT NULL CHECK (sentiment IN ('positive','negative','neutral')),
    note        TEXT    NOT NULL
);

-- Health: daily summary (steps, distance, calories, HR, sleep)
CREATE TABLE IF NOT EXISTS health_daily (
    date            TEXT PRIMARY KEY,  -- YYYYMMDD
    date_iso        TEXT,              -- YYYY-MM-DD (human-readable)
    steps           INTEGER,
    distance_m      INTEGER,
    calories_kcal   REAL,
    duration_min    INTEGER,
    altitude_m      REAL,
    resting_hr      INTEGER,
    max_hr          INTEGER,
    min_hr          INTEGER,
    spo2_avg        REAL,
    sleep_total_min INTEGER,
    sleep_deep_min  INTEGER,
    sleep_light_min INTEGER,
    sleep_rem_min   INTEGER,
    sleep_awake_min INTEGER
);

-- Health: daily totals broken down by sport type
CREATE TABLE IF NOT EXISTS sport_daily (
    date          TEXT NOT NULL,   -- YYYYMMDD
    date_iso      TEXT,            -- YYYY-MM-DD
    sport_type    INTEGER NOT NULL,
    sport_name    TEXT NOT NULL,
    steps         INTEGER,
    distance_m    INTEGER,
    calories_kcal REAL,
    duration_min  INTEGER,
    PRIMARY KEY (date, sport_type)
);

-- Health: heart rate time series (one row per reading)
CREATE TABLE IF NOT EXISTS heart_rate (
    ts           INTEGER PRIMARY KEY,  -- unix seconds
    date         TEXT NOT NULL,        -- YYYYMMDD for fast date queries
    datetime_utc TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM:SS' UTC
    bpm          REAL NOT NULL,
    kind         TEXT NOT NULL         -- 'dynamic' | 'resting'
);
CREATE INDEX IF NOT EXISTS idx_hr_date ON heart_rate(date);

-- Health: per-minute activity breakdown
CREATE TABLE IF NOT EXISTS sport_minute (
    ts            INTEGER PRIMARY KEY,  -- unix seconds (start of minute)
    date          TEXT NOT NULL,        -- YYYYMMDD
    datetime_utc  TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM:SS' UTC
    sport_type    INTEGER NOT NULL,
    sport_name    TEXT NOT NULL,
    steps         INTEGER,
    distance_m    INTEGER,
    calories_kcal REAL,
    altitude_m    REAL,
    duration_min  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sm_date ON sport_minute(date);
"""

# Migrations: ALTER TABLE statements to add new columns to databases that were
# created before the created_at / date_iso / datetime_utc columns existed.
# ALTER TABLE ADD COLUMN is idempotent in SQLite as long as we catch the
# "duplicate column" error. Each entry is (table, column_definition).
_MIGRATIONS = [
    ("messages",            "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("notes",               "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("agent_conversations", "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("feedback",            "created_at  TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("health_daily",        "date_iso    TEXT"),
    ("sport_daily",         "date_iso    TEXT"),
    ("heart_rate",          "datetime_utc TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
    ("sport_minute",        "datetime_utc TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'"),
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
    for table in ("messages", "notes", "agent_conversations", "feedback"):
        conn.execute(
            f"UPDATE {table} SET created_at = datetime(ts, 'unixepoch') "
            f"WHERE created_at IS NULL OR created_at = '1970-01-01 00:00:00'"
        )
    for table in ("heart_rate", "sport_minute"):
        conn.execute(
            f"UPDATE {table} SET datetime_utc = datetime(ts, 'unixepoch') "
            f"WHERE datetime_utc IS NULL OR datetime_utc = '1970-01-01 00:00:00'"
        )
    # Convert stored YYYYMMDD → YYYY-MM-DD for the two health date columns.
    for table in ("health_daily", "sport_daily"):
        conn.execute(
            f"UPDATE {table} SET date_iso = "
            f"substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2) "
            f"WHERE date_iso IS NULL"
        )


def init() -> None:
    """Create tables and apply any pending schema migrations. Safe to call on every boot."""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_notes_categories(conn)
        _migrate(conn)


def log_message(
    user_id: int,
    role: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append one turn to the conversation log."""
    with _connect() as conn:
        conn.execute(
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


def recent_history(user_id: int, limit: int = 20) -> list[dict[str, str]]:
    """Return the last `limit` turns in chronological order, OpenAI message format."""
    with _connect() as conn:
        rows: Iterable[tuple[str, str]] = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(list(rows))]
