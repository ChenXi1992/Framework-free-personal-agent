"""Per-component debug log — only written when LOG_LEVEL=DEBUG.

Unlike audit.py (which always runs and truncates long fields), this log:
  - Is completely disabled unless LOG_LEVEL=DEBUG
  - Applies NO truncation — fields are stored full-fidelity
  - Lives in a separate file (data/debug.log.json) so agent.log.json stays clean

Each component is controlled by its own env var in config.py:
  DEBUG_LOG_ROUTER        → debug_router_call events
  DEBUG_LOG_AGENT_PROMPT  → debug_agent_prompt events
  DEBUG_LOG_LLM_RESPONSE  → debug_llm_response events
  DEBUG_LOG_REASONING     → debug_reasoning events
  DEBUG_LOG_FINISH_REASON → debug_finish_reason events
  DEBUG_LOG_MESSAGES      → debug_messages events (can be large)

Format: same as audit.py — pretty-printed JSON blocks separated by blank lines,
fully jq-compatible:

  # All debug events for a session
  jq '.' data/debug.log.json

  # See every system prompt that was sent
  jq 'select(.event=="debug_agent_prompt") | {agent, system_prompt}' data/debug.log.json

  # Trace one full conversation (all event types)
  jq 'select(.event | startswith("debug_"))' data/debug.log.json
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Resolved once at import time.  Checks the env var directly rather than
# importing from config to avoid a circular import (config imports nothing
# from app; debug_log is imported by modules that also import config).
_ENABLED = os.environ.get("LOG_LEVEL", "INFO").upper() == "DEBUG"

# Always written to data/debug.log.json — no separate path config so there's
# no way to accidentally leave debug logging writing to production paths.
_LOG_PATH = Path(os.environ.get("DEBUG_LOG_PATH", "./data/debug.log.json"))
_lock = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(event: str, **fields: Any) -> None:
    """Append one full-fidelity JSON record to the debug log. Never raises.

    Unlike audit.log_event, no truncation is applied — the entire field value
    is stored. This means a single record can be large (especially for
    debug_messages or debug_agent_prompt with long context blocks).

    Only writes when LOG_LEVEL=DEBUG. All calls are no-ops otherwise.
    """
    if not _ENABLED:
        return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _iso_now(), "event": event, **fields}
        block = json.dumps(record, indent=2, default=str, ensure_ascii=False)
        with _lock, _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(block + "\n\n")
    except Exception:
        # Debug logging must never crash the bot.
        pass
