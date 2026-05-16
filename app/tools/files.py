"""Local file tools.

Allows the agent to read, write, and edit plain-text files on the local
filesystem (.md, .txt, .rst, .csv, .json, .yaml, etc.).

Write and edit operations go through the pending-action staging flow — the
user must confirm before any bytes hit disk.

Allowed root: `FILES_ROOT` env var (defaults to ~/Documents). Every path is
resolved relative to this root and validated against path traversal.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .pending import stage_action
from .registry import tool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety: restrict all file access to a single root directory
# ---------------------------------------------------------------------------

# Default root is the project's own data/ directory so every agent reads and
# writes to the same location and nothing leaks onto the host filesystem.
# Override with FILES_ROOT in .env if you want a different path.
_PROJECT_DATA = Path(__file__).parent.parent.parent / "data"
FILES_ROOT = Path(os.environ.get("FILES_ROOT", _PROJECT_DATA)).expanduser().resolve()

_ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".html", ".xml",
}


def _safe_path(relative: str) -> Path:
    """Resolve `relative` inside FILES_ROOT; raise if it escapes the root."""
    target = (FILES_ROOT / relative).resolve()
    if not str(target).startswith(str(FILES_ROOT)):
        raise ValueError(
            f"Path {relative!r} resolves outside FILES_ROOT ({FILES_ROOT}). "
            "Only paths within the allowed root are accessible."
        )
    return target


def _check_extension(path: Path) -> None:
    if path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Extension {path.suffix!r} is not allowed. "
            f"Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}."
        )


# ---------------------------------------------------------------------------
# Read-only tools (no staging needed)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Read the full text content of a local file. "
        f"All paths are relative to FILES_ROOT ({FILES_ROOT}). "
        "Returns the file content as a string. "
        "Allowed extensions: .md .txt .rst .csv .json .yaml .yml .toml .ini "
        ".cfg .log .html .xml."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path from FILES_ROOT, e.g. 'notes/todo.md'. "
                    "Subdirectories are allowed."
                ),
            },
        },
        "required": ["path"],
    },
)
def file_read(*, path: str) -> str:
    target = _safe_path(path)
    _check_extension(target)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not target.is_file():
        raise ValueError(f"{path!r} is not a file.")
    content = target.read_text(encoding="utf-8")
    log.debug("file_read: %s (%d chars)", target, len(content))
    return content


@tool(
    description=(
        "List files and subdirectories inside a directory. "
        f"All paths are relative to FILES_ROOT ({FILES_ROOT}). "
        "Pass '.' to list the root itself. "
        "Directories are shown with a trailing '/'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path of the directory to list. Use '.' for the root.",
            },
        },
        "required": [],
    },
)
def file_list(*, path: str = ".") -> str:
    target = _safe_path(path or ".")
    if not target.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not target.is_dir():
        raise ValueError(f"{path!r} is not a directory.")
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = [(e.name + "/" if e.is_dir() else e.name) for e in entries]
    return "\n".join(lines) if lines else "(empty directory)"


# ---------------------------------------------------------------------------
# Write tools (all staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Write (overwrite or create) a local plain-text file. "
        f"All paths are relative to FILES_ROOT ({FILES_ROOT}). "
        "STAGED — does not execute until the user types 'confirm'. "
        "Allowed extensions: .md .txt .rst .csv .json .yaml .yml .toml .ini "
        ".cfg .log .html .xml."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path for the file, e.g. 'notes/todo.md'.",
            },
            "content": {
                "type": "string",
                "description": "Full text content to write to the file.",
            },
            "user_id": {"type": "integer"},
        },
        "required": ["path", "content", "user_id"],
    },
    destructive=True,
)
def file_write(*, path: str, content: str, user_id: int) -> str:
    target = _safe_path(path)
    _check_extension(target)
    preview = (
        f"Write file: {path}\n"
        f"  Size: {len(content)} characters\n"
        f"  Preview: {content[:120]!r}{'…' if len(content) > 120 else ''}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="file_write",
        arguments={"path": path, "content": content},
        preview=preview,
    )
    return f"staged {action_id}"


@tool(
    description=(
        "Replace an exact string in an existing local file. "
        f"All paths are relative to FILES_ROOT ({FILES_ROOT}). "
        "STAGED — does not execute until the user types 'confirm'. "
        "`old_text` must appear exactly once in the file. "
        "Allowed extensions: .md .txt .rst .csv .json .yaml .yml .toml .ini "
        ".cfg .log .html .xml."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path of the file to edit.",
            },
            "old_text": {
                "type": "string",
                "description": "The exact substring to find and replace. Must be unique in the file.",
            },
            "new_text": {
                "type": "string",
                "description": "The string to substitute in place of old_text.",
            },
            "user_id": {"type": "integer"},
        },
        "required": ["path", "old_text", "new_text", "user_id"],
    },
    destructive=True,
)
def file_edit(*, path: str, old_text: str, new_text: str, user_id: int) -> str:
    target = _safe_path(path)
    _check_extension(target)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {path}")
    content = target.read_text(encoding="utf-8")
    count = content.count(old_text)
    if count == 0:
        raise ValueError(f"old_text not found in {path!r}.")
    if count > 1:
        raise ValueError(
            f"old_text appears {count} times in {path!r}. "
            "Provide a longer, unique context string."
        )
    preview = (
        f"Edit file: {path}\n"
        f"  Replace: {old_text[:80]!r}{'…' if len(old_text) > 80 else ''}\n"
        f"  With:    {new_text[:80]!r}{'…' if len(new_text) > 80 else ''}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="file_edit",
        arguments={"path": path, "old_text": old_text, "new_text": new_text},
        preview=preview,
    )
    return f"staged {action_id}"


@tool(
    description=(
        "Append text to the end of an existing local file. "
        f"All paths are relative to FILES_ROOT ({FILES_ROOT}). "
        "STAGED — does not execute until the user types 'confirm'. "
        "Allowed extensions: .md .txt .rst .csv .json .yaml .yml .toml .ini "
        ".cfg .log .html .xml."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path of the file to append to.",
            },
            "text": {
                "type": "string",
                "description": "Text to append at the end of the file (verbatim).",
            },
            "user_id": {"type": "integer"},
        },
        "required": ["path", "text", "user_id"],
    },
    destructive=True,
)
def file_append(*, path: str, text: str, user_id: int) -> str:
    target = _safe_path(path)
    _check_extension(target)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {path}")
    preview = (
        f"Append to file: {path}\n"
        f"  Text: {text[:120]!r}{'…' if len(text) > 120 else ''}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="file_append",
        arguments={"path": path, "text": text},
        preview=preview,
    )
    return f"staged {action_id}"


# ---------------------------------------------------------------------------
# Executor (called by /confirm handler in main.py)
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a previously staged file action after user confirmation."""
    try:
        if tool_name == "file_write":
            target = _safe_path(arguments["path"])
            _check_extension(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(arguments["content"], encoding="utf-8")
            return {"ok": True, "bytes": len(arguments["content"])}

        if tool_name == "file_edit":
            target = _safe_path(arguments["path"])
            _check_extension(target)
            content = target.read_text(encoding="utf-8")
            new_content = content.replace(arguments["old_text"], arguments["new_text"], 1)
            target.write_text(new_content, encoding="utf-8")
            return {"ok": True}

        if tool_name == "file_append":
            target = _safe_path(arguments["path"])
            _check_extension(target)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(arguments["text"])
            return {"ok": True}

        return {"ok": False, "error": f"unknown tool: {tool_name}"}
    except Exception as exc:  # noqa: BLE001
        log.exception("execute_confirmed failed for %s", tool_name)
        return {"ok": False, "error": str(exc)}
