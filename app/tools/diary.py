"""Diary tool — append-only journal stored in data/diary.md.

Each entry is written with a datestamp header and optional category tags
(#workout, #lifestyle, #career, etc.).  The file is intentionally plain
Markdown so it can be read, searched, and exported without any tooling.

Design notes:
  - diary.md is append-only from the tool's perspective; no editing or
    deletion of past entries is supported.
  - The agent is responsible for extracting structured data (notes, todos,
    goals) from diary entries using the appropriate tools *after* calling
    diary_add.
  - A diary entry is always a note conceptually, but the diary file is the
    primary record.  The notes DB contains extracted highlights, not the
    full narrative.
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

from .registry import tool

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
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
        "Call this whenever the user shares personal reflections, daily logs, "
        "or any narrative they want kept in their diary. "
        "After writing the entry you SHOULD also call note_add, todo_add, or "
        "goal-related tools to extract any structured items embedded in the text "
        "(e.g. a workout logged in a diary entry should also go into notes)."
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
                    "(e.g. ['workout', 'lifestyle']).  Used for filtering later."
                ),
            },
        },
        "required": ["entry"],
    },
)
def diary_add(entry: str, categories: list[str] | None = None) -> dict[str, Any]:
    """Append one dated entry to data/diary.md and return a confirmation."""
    cats = [c.strip().lower() for c in (categories or []) if c.strip()]
    header = _today_header(cats)
    block = f"{header}\n\n{entry.strip()}\n\n---\n\n"

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _DIARY_PATH.open("a", encoding="utf-8") as f:
        f.write(block)

    log.debug("Diary entry appended (%d chars, tags=%s)", len(entry), cats)
    return {
        "ok": True,
        "path": str(_DIARY_PATH),
        "categories": cats,
        "entry_preview": entry[:120] + ("…" if len(entry) > 120 else ""),
    }


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
                    "(e.g. 'workout').  Omit or pass '' to return all entries."
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

    # Split on the separator; each block starts with "## YYYY-MM-DD HH:MM"
    blocks = [b.strip() for b in raw.split("\n---\n") if b.strip()]

    entries = []
    for block in reversed(blocks):
        lines = block.strip().splitlines()
        if not lines or not lines[0].startswith("## "):
            continue
        header_line = lines[0]
        body = "\n".join(lines[1:]).strip()
        # Extract category tags like #workout, #lifestyle from the header line.
        # Skip the "##" heading marker (len==2, second char is "#" not alpha)
        # and date tokens like "#2026" (second char is a digit).
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
