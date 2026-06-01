"""Debug log — verbose diagnostic events, only written when LOG_LEVEL=DEBUG.

Thin wrapper around app.logger. Adds level="debug" and never truncates fields.
All output goes to the same agent.log.json as audit events.

Query examples:
  jq 'select(.level=="debug")' data/agent.log.json
  jq 'select(.event=="debug_tool_call") | {ts, agent, name, args, ok, duration_ms}' data/agent.log.json
  jq 'select(.event=="debug_dispatch")' data/agent.log.json
"""
from __future__ import annotations

from typing import Any

import app.logger as _logger


def log(event: str, **fields: Any) -> None:
    """Append one debug record (level=debug, full fidelity). No-op unless LOG_LEVEL=DEBUG."""
    _logger.log(event, level="debug", **fields)
