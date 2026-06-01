"""Audit log — operational events, always written.

Thin wrapper around app.logger. Adds level="info" to every record.
All output goes to agent.log.json.

Query examples:
  jq 'select(.level=="info" and .event=="tool_call" and .ok==false)' data/agent.log.json
  jq 'select(.event=="llm_response") | {ts, agent, .usage.total}' data/agent.log.json
  tail -f data/agent.log.json | jq -c '{ts, level, event}'
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .logger import log, LOG_PATH


def log_event(event: str, **fields: Any) -> None:
    """Append one operational record (level=info, truncated). Never raises."""
    log(event, level="info", **fields)


def get_log_path() -> Path:
    """Where records are being written. For startup-log surfacing."""
    return LOG_PATH or Path("./data/agent.log.json")
