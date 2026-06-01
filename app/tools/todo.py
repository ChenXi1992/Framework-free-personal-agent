"""Todo tool — lightweight task list stored in data/todo.md.

Items are plain Markdown checkboxes:
    - [ ] Buy groceries
    - [x] Call dentist  *(done 2026-05-15)*

The file is human-readable and editable in any text editor.

All writes are staged — the user must confirm before any change lands on disk.

Design notes:
  - Indices are 1-based and refer to the position in the *full* list
    (including done items) so they are stable within a single session.
    Always call todo_list first, show the user the numbered list, then
    call todo_done / todo_delete with the correct index.
  - When "done" is ambiguous (user says "mark workout done" and there are
    multiple workout items), call todo_list, present the numbered choices
    to the user, and ask to confirm the number before calling todo_done.
"""
from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from typing import Any

from .pending import stage_action
from .registry import tool

log = logging.getLogger(__name__)

_DATA_DIR  = Path(__file__).parent.parent.parent / "data"
_TODO_PATH = _DATA_DIR / "todo.md"

_OPEN_RE = re.compile(r"^- \[ \] (.+)$")
_DONE_RE = re.compile(r"^- \[x\] (.+?)(?:  \*\(done [^)]+\)\*)?$", re.IGNORECASE)


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
    items = []
    for idx, line in enumerate(lines, start=1):
        m_open = _OPEN_RE.match(line)
        m_done = _DONE_RE.match(line)
        if m_open:
            items.append({"index": idx, "done": False, "text": m_open.group(1)})
        elif m_done:
            items.append({"index": idx, "done": True, "text": m_done.group(1)})
    return items


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Add a new open task to the todo list. "
        "STAGED — nothing is written until the user confirms. "
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
            "user_id": {"type": "integer"},
        },
        "required": ["item", "user_id"],
    },
    destructive=True,
)
def todo_add(item: str, user_id: int) -> dict[str, Any]:
    """Stage adding one open checkbox item to todo.md."""
    item = item.strip()
    lines = _read_lines()
    new_index = len(lines) + 1
    preview = f"Add todo [{new_index}]: {item!r}"
    action_id = stage_action(
        user_id=user_id,
        tool_name="todo_add",
        arguments={"item": item},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


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
        "STAGED — nothing is written until the user confirms. "
        "Get the index from todo_list first. "
        "If the user's request is ambiguous (multiple matching items), "
        "call todo_list, present the numbered choices, and wait for confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "1-based index of the item as returned by todo_list.",
            },
            "user_id": {"type": "integer"},
        },
        "required": ["index", "user_id"],
    },
    destructive=True,
)
def todo_done(index: int, user_id: int) -> dict[str, Any]:
    """Stage marking item at `index` as done."""
    lines = _read_lines()
    items = _parse_items(lines)
    match = next((i for i in items if i["index"] == index), None)
    if match is None:
        return {"ok": False, "error": f"No todo item at index {index}."}
    if match["done"]:
        return {"ok": False, "error": f"Item {index} is already done: {match['text']}"}

    preview = f"Mark done [{index}]: {match['text']!r}"
    action_id = stage_action(
        user_id=user_id,
        tool_name="todo_done",
        arguments={"index": index, "item_text": match["text"]},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


@tool(
    description=(
        "Permanently delete a todo item by its index number. "
        "STAGED — nothing is written until the user confirms. "
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
            "user_id": {"type": "integer"},
        },
        "required": ["index", "user_id"],
    },
    destructive=True,
)
def todo_delete(index: int, user_id: int) -> dict[str, Any]:
    """Stage deleting item at `index` from todo.md."""
    lines = _read_lines()
    items = _parse_items(lines)
    match = next((i for i in items if i["index"] == index), None)
    if match is None:
        return {"ok": False, "error": f"No todo item at index {index}."}

    preview = f"Delete todo [{index}]: {match['text']!r}"
    action_id = stage_action(
        user_id=user_id,
        tool_name="todo_delete",
        arguments={"index": index, "item_text": match["text"]},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


# ---------------------------------------------------------------------------
# Executor (called by /confirm handler in main.py)
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a staged todo action after user confirmation."""
    try:
        if tool_name == "todo_add":
            item = arguments["item"].strip()
            lines = _read_lines()
            lines.append(f"- [ ] {item}")
            _write_lines(lines)
            log.debug("Todo added: %s", item)
            return {"ok": True, "item": item, "index": len(lines)}

        if tool_name == "todo_done":
            index = arguments["index"]
            lines = _read_lines()
            items = _parse_items(lines)
            match = next((i for i in items if i["index"] == index), None)
            if match is None:
                return {"ok": False, "error": f"Item at index {index} no longer exists."}
            today = datetime.date.today().isoformat()
            lines[index - 1] = f"- [x] {match['text']}  *(done {today})*"
            _write_lines(lines)
            log.debug("Todo done: [%d] %s", index, match["text"])
            return {"ok": True, "index": index, "item": match["text"], "done_date": today}

        if tool_name == "todo_delete":
            index = arguments["index"]
            lines = _read_lines()
            items = _parse_items(lines)
            match = next((i for i in items if i["index"] == index), None)
            if match is None:
                return {"ok": False, "error": f"Item at index {index} no longer exists."}
            deleted_text = match["text"]
            del lines[index - 1]
            _write_lines(lines)
            log.debug("Todo deleted: [%d] %s", index, deleted_text)
            return {"ok": True, "index": index, "deleted": deleted_text}

        return {"ok": False, "error": f"Unknown tool: {tool_name}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("execute_confirmed failed for %s", tool_name)
        return {"ok": False, "error": str(exc)}
