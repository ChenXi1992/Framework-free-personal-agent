"""Unified event logger — single file, two levels.

All bot events go to one file (agent.log.json by default).
Two levels are supported:

  level="info"   — operational events, always written, fields truncated at
                   MAX_FIELD_CHARS so no single record becomes huge.
                   Examples: llm_call, tool_call, dispatch, confirm.

  level="debug"  — diagnostic events, written only when LOG_LEVEL=DEBUG,
                   full-fidelity (no truncation).
                   Examples: debug_messages, debug_tool_call, debug_reasoning.

Query examples using jq:

  # Everything
  jq '.' data/agent.log.json

  # Operational only
  jq 'select(.level=="info")' data/agent.log.json

  # Debug only
  jq 'select(.level=="debug")' data/agent.log.json

  # All tool calls (info)
  jq 'select(.event=="tool_call")' data/agent.log.json

  # Debug tool calls with args
  jq 'select(.event=="debug_tool_call") | {ts, agent, name, args, ok, duration_ms}' data/agent.log.json

  # Failed tool calls
  jq 'select(.event=="tool_call" and .ok==false)' data/agent.log.json

  # Token usage per call
  jq 'select(.event=="llm_response") | {ts, agent, .usage.total}' data/agent.log.json

  # Live tail (compact)
  tail -f data/agent.log.json | jq -c '{ts, level, event}'

Design notes:
- Append-only. Never read or rewrite from the bot.
- Lock-protected: concurrent async writes don't interleave.
- Debug level is a no-op unless LOG_LEVEL=DEBUG.
- Errors are swallowed — logging must never crash the bot.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Load .env before reading any configuration — logger is imported early in the
# module chain (before config.py runs load_dotenv), so we must load it here
# ourselves to ensure AUDIT_LOG_PATH and LOG_LEVEL are available.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass  # dotenv not installed — env vars must come from the shell

# Single output file — configurable via AUDIT_LOG_PATH env var.
# Empty string disables all logging.
_log_path_raw = os.environ.get("AUDIT_LOG_PATH", "./data/agent.log.json")
LOG_PATH   = Path(_log_path_raw) if _log_path_raw else None
_ENABLED   = bool(_log_path_raw)

# Debug level is gated behind LOG_LEVEL=DEBUG
_IS_DEBUG  = os.environ.get("LOG_LEVEL", "INFO").strip().upper() == "DEBUG"

# Info-level fields are capped so no single record grows unbounded.
# Full content is always in the SQLite messages table.
MAX_FIELD_CHARS = 2000

_lock = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _truncate(v: Any) -> Any:
    """Recursively cap string fields to MAX_FIELD_CHARS."""
    if isinstance(v, str):
        return v[:MAX_FIELD_CHARS - 3] + "..." if len(v) > MAX_FIELD_CHARS else v
    if isinstance(v, dict):
        return {k: _truncate(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_truncate(x) for x in v]
    return v


def log(event: str, level: str = "info", **fields: Any) -> None:
    """Append one JSON record to agent.log.json. Never raises.

    level="info"  → always written, fields truncated at MAX_FIELD_CHARS
    level="debug" → only written when LOG_LEVEL=DEBUG, no truncation
    """
    if not _ENABLED:
        return
    if level == "debug" and not _IS_DEBUG:
        return
    try:
        data = fields if level == "debug" else _truncate(fields)
        record = {"ts": _iso_now(), "level": level, "event": event, **data}
        block = json.dumps(record, indent=2, default=str, ensure_ascii=False)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock, LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(block + "\n\n")
    except Exception:
        pass
