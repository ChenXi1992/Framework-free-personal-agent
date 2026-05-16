"""Append-only audit log of everything the bot does.

Format: one pretty-printed JSON block per event, separated by a blank line.
This is readable in any text editor while remaining fully jq-compatible:

  # All failed tool calls
  jq 'select(.event=="tool_call" and .ok==false)' data/agent.log.json

  # LLM text output for a specific agent
  jq 'select(.event=="llm_response" and .agent=="workout") | {ts, text}' data/agent.log.json

  # Total tokens today
  jq -s '[.[] | select(.event=="llm_response") | .usage.total] | add' data/agent.log.json

  # Live tail (compact output)
  tail -f data/agent.log.json | jq -c '.'

The path is configurable via AUDIT_LOG_PATH. Set it to '' (empty string) to
disable logging entirely. Parent directories are created on first write.

Design notes:
- Append-only. We never read, parse, or rewrite the file from the bot.
- Lock-protected so concurrent writes from different async handlers don't
  interleave writes mid-block.
- Long string fields are truncated to _MAX_FIELD_CHARS. Full text lives in
  the SQLite messages table.
- Errors are swallowed — logging must never crash the bot.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Cap any single field value so a 10MB email body can't bloat one log record.
_MAX_FIELD_CHARS = 2000

# Read once; empty string explicitly disables the log.
# os.environ.get() with a default always returns a non-empty string, so we
# must check whether the var is explicitly set to "" rather than using bool().
_audit_path_raw = os.environ.get("AUDIT_LOG_PATH", "./data/agent.log.json")
_LOG_PATH = Path(_audit_path_raw) if _audit_path_raw else Path("./data/agent.log.json")
_ENABLED  = bool(_audit_path_raw)  # empty string → False, any path → True
_lock = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _truncate(v: Any) -> Any:
    """Recursively cap string fields to _MAX_FIELD_CHARS so single records stay small."""
    if isinstance(v, str):
        if len(v) > _MAX_FIELD_CHARS:
            return v[: _MAX_FIELD_CHARS - 3] + "..."
        return v
    if isinstance(v, dict):
        return {k: _truncate(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_truncate(x) for x in v]
    return v


def log_event(event: str, **fields: Any) -> None:
    """Append one pretty-printed JSON record to the audit log. Never raises.

    Each record is formatted with 2-space indentation and followed by a blank
    line so the file is easy to read in an editor while remaining valid for jq.
    """
    if not _ENABLED:
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _iso_now(), "event": event, **_truncate(fields)}
        block = json.dumps(record, indent=2, default=str, ensure_ascii=False)
        with _lock, _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(block + "\n\n")  # blank line between records
    except Exception:
        # Logging must never crash the bot.
        pass


def get_log_path() -> Path:
    """Where audit records are being written. For startup-log surfacing."""
    return _LOG_PATH
