"""Todo tool — lightweight task list stored in data/todo.md.

Items are plain Markdown checkboxes:
    - [ ] Buy groceries
    - [x] Call dentist  *(done 2026-05-15)*

The file is human-readable and editable in any text editor.

Design notes:
  - Indices are 1-based and refer to the position in the *full* list
    (including done items) so they are stable within a single session.
    Always call todo_list first, show the user the numbered list, then
    call todo_done / todo_delete with the correct index.
  - When "done" is ambiguous (user says "mark workout done" and there are
    multiple workout items), call todo_list, show the results with indices,
    and ask the user to confirm the number before calling todo_done.
"""
from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from typing import Any

from .registry import tool

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_TODO_PATH = _DATA_DIR / "todo.md"

# Patterns
_OPEN_RE   = re.compile(r"^- \[ \] (.+)$")
_DONE_RE   = re.compile(r"^- \[x\] (.+?)(?:  \*\(done [^)]+\)\*)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_lines() -> list[str]:
    if not _TODO_PATH.exists():
        return []
    return _TODO_PATH.read_text(encoding="utf-8").splitlines()


def _write_lines(lines: list[str]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _TODO_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _parse_items(lines: list[str]) -> list[dict[str, Any]]:
    """Return structured items with their 1-based index in the file."""
    items = []
    for idx, line in enumerate(lines, start=1):
        m_open = _OPEN_RE.match(line)
        m_done = _DONE_RE.match(line)
        if m_open:
            items.append({"index": idx, "done": False, "text": m_open.group(1)})
        elif m_done:
            items.append({"index": idx, "done": True,  "text": m_done.group(1)})
    return items


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Add a new open task to the todo list. "
        "Call this whenever the user mentions something that needs to be done, "
        "or when extracting action items from diary entries / notes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "item": {
                "type": "string",
                "description": "The task description, written as an action (e.g. 'Book dentist appointment').",
            },
        },
        "required": ["item"],
    },
)
def todo_add(item: str) -> dict[str, Any]:
    """Append one open checkbox item to todo.md."""
    item = item.strip()
    lines = _read_lines()
    lines.append(f"- [ ] {item}")
    _write_lines(lines)
    log.debug("Todo added: %s", item)
    # index = 1-based line number of the newly appended item (matches todo_list indices)
    return {"ok": True, "item": item, "index": len(lines)}


@tool(
    description=(
        "List all todo items with their 1-based index numbers. "
        "Always call this before todo_done or todo_delete so you know the "
        "correct index. Pass include_done=true to also show completed items."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_done": {
                "type": "boolean",
                "description": "Include already-completed items (default false).",
            },
        },
        "required": [],
    },
)
def todo_list(include_done: bool = False) -> dict[str, Any]:
    """Return todo items with their 1-based line indices."""
    lines = _read_lines()
    items = _parse_items(lines)
    if not include_done:
        items = [i for i in items if not i["done"]]
    return {"items": items, "total": len(items)}


@tool(
    description=(
        "Mark a todo item as done by its index number. "
        "Get the index from todo_list first. "
        "If the user's request is ambiguous (multiple matching items), "
        "call todo_list, present the numbered choices to the user, and wait "
        "for confirmation before calling todo_done."
    ),
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "1-based index of the item as returned by todo_list.",
            },
        },
        "required": ["index"],
    },
)
def todo_done(index: int) -> dict[str, Any]:
    """Mark the item at position `index` as done with today's date."""
    lines = _read_lines()
    items = _parse_items(lines)
    match = next((i for i in items if i["index"] == index), None)
    if match is None:
        return {"ok": False, "error": f"No todo item at index {index}."}
    if match["done"]:
        return {"ok": False, "error": f"Item {index} is already done: {match['text']}"}

    today = datetime.date.today().isoformat()
    lines[index - 1] = f"- [x] {match['text']}  *(done {today})*"
    _write_lines(lines)
    log.debug("Todo done: [%d] %s", index, match["text"])
    return {"ok": True, "index": index, "item": match["text"], "done_date": today}


@tool(
    description=(
        "Permanently delete a todo item by its index number. "
        "Get the index from todo_list first. "
        "Use this to remove cancelled or irrelevant tasks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "1-based index of the item as returned by todo_list.",
            },
        },
        "required": ["index"],
    },
)
def todo_delete(index: int) -> dict[str, Any]:
    """Remove the item at position `index` from todo.md entirely."""
    lines = _read_lines()
    items = _parse_items(lines)
    match = next((i for i in items if i["index"] == index), None)
    if match is None:
        return {"ok": False, "error": f"No todo item at index {index}."}

    deleted_text = match["text"]
    del lines[index - 1]
    _write_lines(lines)
    log.debug("Todo deleted: [%d] %s", index, deleted_text)
    return {"ok": True, "index": index, "deleted": deleted_text}
