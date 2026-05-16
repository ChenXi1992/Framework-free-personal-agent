"""LLM-callable tools for reading and proposing changes to agent prompt files.

Agents can read their own prompt, propose a diff-style update when they judge
it appropriate, and record tool-need observations. All writes go through the
staged-action flow so the user confirms before anything changes on disk.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .registry import tool
from .pending import stage_action

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"
ALLOWED_PROMPTS = {"workout", "lifestyle", "career", "router", "classifier"}


def _prompt_path(name: str) -> Path:
    if name not in ALLOWED_PROMPTS:
        raise ValueError(f"Unknown prompt: {name!r}. Allowed: {ALLOWED_PROMPTS}")
    return PROMPTS_DIR / f"{name}.md"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Read the current content of an agent prompt file. "
        "Use this before proposing any changes so you base the diff on the real current text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": list(ALLOWED_PROMPTS),
                "description": "Which prompt to read.",
            },
        },
        "required": ["name"],
    },
)
def prompt_read(name: str) -> dict[str, Any]:
    path = _prompt_path(name)
    if not path.exists():
        return {"error": f"Prompt file not found: {path}"}
    return {"name": name, "content": path.read_text()}


# ---------------------------------------------------------------------------
# Propose change (staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Propose a change to an agent prompt file. "
        "This does NOT apply immediately — it stages the change for user confirmation. "
        "Provide the complete new file content (not just a diff). "
        "Include a brief rationale explaining what behavioral pattern triggered this and what you changed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": list(ALLOWED_PROMPTS),
                "description": "Which prompt to update.",
            },
            "new_content": {
                "type": "string",
                "description": "The complete new content for the prompt file.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this change — what pattern you observed and what you changed.",
            },
            "user_id": {"type": "integer"},
        },
        "required": ["name", "new_content", "rationale", "user_id"],
    },
    destructive=True,
)
def prompt_propose(name: str, new_content: str, rationale: str, user_id: int) -> dict[str, Any]:
    path = _prompt_path(name)
    old_content = path.read_text() if path.exists() else ""

    diff_lines = _make_diff(old_content, new_content, name)
    preview = (
        f"📝 Prompt update: {name}.md\n\n"
        f"Rationale: {rationale}\n\n"
        f"--- diff ---\n{diff_lines}"
    )

    action_id = stage_action(
        user_id=user_id,
        tool_name="prompt_propose",
        arguments={"name": name, "content": new_content},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Called by main.py when the user confirms a prompt_propose action."""
    name = arguments["name"]
    content = arguments["content"]
    path = _prompt_path(name)
    path.write_text(content)
    return {"ok": True, "name": name, "bytes": len(content)}


# ---------------------------------------------------------------------------
# Tool need observation (not staged — just logs to notes)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Record a tool need observation. Use when you could help the user better "
        "if a specific capability existed (e.g. access to Strava, a certain API, "
        "push notifications). This surfaces it for the developer to review — "
        "you are NOT writing code, just describing what would be useful."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title":       {"type": "string", "description": "Short name for the needed capability."},
            "description": {"type": "string", "description": "What it would do and why it would help."},
            "agent":       {"type": "string", "enum": ["workout", "lifestyle", "career", "general"]},
            "example":     {"type": "string", "description": "Concrete example: 'When user asks X, I could Y.'"},
        },
        "required": ["title", "description", "agent"],
    },
)
def tool_need(title: str, description: str, agent: str, example: str = "") -> dict[str, Any]:
    needs_file = PROMPTS_DIR.parent / "tool_needs.md"
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    entry = f"\n## [{ts}] {title} ({agent})\n{description}\n"
    if example:
        entry += f"**Example:** {example}\n"
    with needs_file.open("a") as f:
        f.write(entry)
    return {"ok": True, "recorded": title}


# ---------------------------------------------------------------------------
# Internal diff helper
# ---------------------------------------------------------------------------

def _make_diff(old: str, new: str, name: str) -> str:
    """Simple unified-style diff between two strings."""
    import difflib
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{name}.md",
        tofile=f"b/{name}.md",
        n=3,
    )
    result = "".join(diff)
    return result if result else "(no changes)"
