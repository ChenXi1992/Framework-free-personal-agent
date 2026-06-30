"""Diary tool — append-only journal stored in data/diary.md.

Each entry is written with a datestamp header and optional category tags
(#workout, #lifestyle, #career, etc.).  The file is intentionally plain
Markdown so it can be read, searched, and exported without any tooling.

All writes are staged — the user must confirm before any entry lands on disk.

Design notes:
  - diary.md is append-only from the tool's perspective; no editing or
    deletion of past entries is supported.
  - The agent is responsible for extracting structured data (notes, todos,
    goals) from diary entries using the appropriate tools *after* confirming.
  - A diary entry is always a note conceptually, but the diary file is the
    primary record.  The notes DB contains extracted highlights, not the
    full narrative.
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

from .pending import stage_action
from .registry import tool

log = logging.getLogger(__name__)

_DATA_DIR   = Path(__file__).parent.parent.parent / "data"
_DIARY_PATH = _DATA_DIR / "diary.md"


def _today_header(categories: list[str]) -> str:
    """Build the Markdown heading for one diary entry.

    Example:
        ## 2026-05-16 14:23  #workout #lifestyle
    """
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    tags = "  " + " ".join(f"#{c.lstrip('#')}" for c in categories) if categories else ""
    return f"## {stamp}{tags}"


@tool(
    description=(
        "Append a diary / journal entry to diary.md. "
        "STAGED — nothing is written until the user confirms. "
        "Call this whenever the user shares personal reflections, daily logs, "
        "or any narrative they want kept in their diary. "
        "After the user confirms, call todo_add or goal-related tools to extract "
        "any structured items embedded in the text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "entry": {
                "type": "string",
                "description": "The full diary entry text to record, in the user's own words.",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Zero or more category tags to attach to this entry "
                    "(e.g. ['workout', 'lifestyle']). Used for filtering later."
                ),
            },
            "user_id": {"type": "integer"},
        },
        "required": ["entry", "user_id"],
    },
    destructive=True,
)
def diary_add(entry: str, user_id: int, categories: list[str] | None = None) -> dict[str, Any]:
    """Stage appending one dated entry to data/diary.md."""
    cats = [c.strip().lower() for c in (categories or []) if c.strip()]
    header = _today_header(cats)
    block = f"{header}\n\n{entry.strip()}\n\n---\n\n"

    tag_str = "  " + " ".join(f"#{c}" for c in cats) if cats else ""
    preview = (
        f"Diary entry{tag_str}:\n"
        f"  {entry[:200]!r}{'…' if len(entry) > 200 else ''}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="diary_add",
        arguments={"block": block, "categories": cats},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


@tool(
    description=(
        "Read recent diary entries, optionally filtered by a category tag. "
        "Returns entries in reverse-chronological order (newest first). "
        "Use this when the user asks to review their diary or when you need "
        "context from past entries."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "Filter to entries tagged with this category "
                    "(e.g. 'workout'). Omit or pass '' to return all entries."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of entries to return (default 5, max 20).",
            },
        },
        "required": [],
    },
)
def diary_recent(category: str = "", limit: int = 5) -> dict[str, Any]:
    """Return the most recent diary entries, newest first, optionally tag-filtered."""
    limit = min(max(1, limit), 20)

    if not _DIARY_PATH.exists():
        return {"entries": [], "total": 0}

    raw = _DIARY_PATH.read_text(encoding="utf-8")
    blocks = [b.strip() for b in raw.split("\n---\n") if b.strip()]

    entries = []
    for block in reversed(blocks):
        lines = block.strip().splitlines()
        if not lines or not lines[0].startswith("## "):
            continue
        header_line = lines[0]
        body = "\n".join(lines[1:]).strip()
        tags = [
            w[1:].lower()
            for w in header_line.split()
            if w.startswith("#") and len(w) > 1 and w[1:2].isalpha()
        ]
        if category and category.lower() not in tags:
            continue
        entries.append({"header": header_line[3:].strip(), "tags": tags, "text": body})
        if len(entries) >= limit:
            break

    return {"entries": entries, "total": len(entries)}


# ---------------------------------------------------------------------------
# Executor (called by /confirm handler in main.py)
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a staged diary action after user confirmation."""
    try:
        if tool_name == "diary_add":
            block = arguments["block"]
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _DIARY_PATH.open("a", encoding="utf-8") as f:
                f.write(block)
            log.debug("Diary entry confirmed (%d chars)", len(block))
            return {"ok": True, "bytes": len(block)}

        return {"ok": False, "error": f"Unknown tool: {tool_name}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("execute_confirmed failed for %s", tool_name)
        return {"ok": False, "error": str(exc)}
