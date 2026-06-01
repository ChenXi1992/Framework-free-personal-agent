"""Section-based prompt editing — reliable self-improvement for agents.

Design rationale
----------------
Prompts are markdown, structured by `## headings`. Editing them reliably means
playing to the LLM's strength (generating complete text) and avoiding its
weakness (reproducing exact byte-for-byte substrings). So instead of "find this
exact old string and replace it", the model names a SECTION and writes the
COMPLETE new version of that section. The tool swaps the whole section in.

This confines every edit's blast radius to a single section — other sections are
physically untouchable — and removes verbatim-reproduction errors entirely.

Scoping
-------
`name` (the target prompt) is resolved by WHO is calling:
  - A specialist agent (workout, dutch, …) is LOCKED to its own prompt — the
    server forces name = the calling agent. It cannot edit another agent's file.
  - The general/admin agent may target ANY prompt (incl. `router`) via `name`.

Safety rails on every write (in execute_confirmed)
  1. Backup the current file to data/prompts/.backups/ before writing.
  2. Validate the result (non-empty; agent prompts keep their ## Routing).
  3. Roll back from the backup if validation fails after writing.

All writes are staged — nothing hits disk until the user confirms.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .registry import tool
from .pending import stage_action

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"
_BACKUP_DIR = PROMPTS_DIR / ".backups"
_KEEP_BACKUPS = 10

# Computed dynamically so new agent files are picked up on restart.
from ..agents.discovery import get_agents as _get_agents  # noqa: E402

_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _allowed_prompts() -> set[str]:
    """Prompts that may be edited: every discovered agent + the router."""
    return set(_get_agents()) | {"router"}


def _prompt_path(name: str) -> Path:
    if name not in _allowed_prompts():
        raise ValueError(
            f"Unknown prompt: {name!r}. Allowed: {sorted(_allowed_prompts())}"
        )
    return PROMPTS_DIR / f"{name}.md"


def _resolve_target(name: str | None, agent: str | None) -> str:
    """Resolve which prompt to edit, enforcing per-agent scoping.

    Specialists are locked to their own prompt; general must name a target.
    """
    if agent and agent != "general":
        return agent  # specialist: own prompt only, ignore any name the LLM sent
    if not name:
        raise ValueError(
            "name is required: specify which prompt to edit (e.g. 'dutch', 'router')."
        )
    return name


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

def _list_sections(content: str) -> list[str]:
    """Return the heading text of every section (## level and deeper)."""
    out = []
    for line in content.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            out.append(("#" * len(m.group(1))) + " " + m.group(2).strip())
    return out


def _find_section_span(content: str, heading: str) -> tuple[int, int, int, list[str]] | None:
    """Locate the section whose heading matches `heading`.

    Returns (start_line, end_line, level, lines) where the section is
    lines[start_line:end_line], or None if not found. The section ends at the
    next heading of the same or higher level (or EOF).
    """
    norm = heading.lstrip("#").strip().lower()
    lines = content.splitlines(keepends=True)
    start = level = None
    for i, line in enumerate(lines):
        m = _HEAD_RE.match(line)
        if m and m.group(2).strip().lower() == norm:
            start, level = i, len(m.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEAD_RE.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    return start, end, level, lines


def _apply_replace(content: str, heading: str, new_section: str) -> str | None:
    """Replace the named section with new_section. Returns new content or None."""
    span = _find_section_span(content, heading)
    if span is None:
        return None
    start, end, _level, lines = span
    new_block = new_section.rstrip() + "\n\n"
    return "".join(lines[:start]) + new_block + "".join(lines[end:])


def _apply_add(content: str, after_heading: str | None, new_section: str) -> str | None:
    """Insert new_section after the named section, or at EOF. Returns new content
    or None if after_heading was given but not found."""
    new_block = new_section.rstrip() + "\n\n"
    if not after_heading or after_heading.strip().lower() in ("", "end", "eof"):
        sep = "" if content.endswith("\n") else "\n"
        return content.rstrip() + "\n\n" + new_block
    span = _find_section_span(content, after_heading)
    if span is None:
        return None
    _start, end, _level, lines = span
    return "".join(lines[:end]) + new_block + "".join(lines[end:])


# ---------------------------------------------------------------------------
# Validation + backup
# ---------------------------------------------------------------------------

def _validate(name: str, content: str) -> str | None:
    """Return an error string if `content` is not a valid prompt, else None."""
    if len(content.strip()) < 20:
        return "resulting content is empty or near-empty"
    if name in _get_agents() and "## Routing" not in content:
        return "an agent prompt must keep its '## Routing' section"
    return None


def _backup(name: str, content: str) -> Path:
    """Snapshot `content` to .backups/ and prune to the most recent _KEEP_BACKUPS.

    The filename includes milliseconds + a short random suffix so multiple edits
    within the same second never overwrite each other's backup.
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    ts += f"-{uuid.uuid4().hex[:4]}"
    bpath = _BACKUP_DIR / f"{name}.{ts}.md"
    bpath.write_text(content, encoding="utf-8")
    backups = sorted(_BACKUP_DIR.glob(f"{name}.*.md"))
    for old in backups[:-_KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return bpath


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Read a prompt file and list its sections. Call this before proposing "
        "any edit so you know the exact section headings. Specialists read their "
        "own prompt by default; the general agent may pass `name` to read any "
        "prompt (an agent name or 'router')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Which prompt to read (general only; specialists read their own).",
            },
            "agent": {"type": "string"},  # injected server-side
        },
        "required": [],
    },
    agent_scoped=True,
)
def prompt_read(agent: str = "", name: str | None = None) -> dict[str, Any]:
    try:
        target = _resolve_target(name, agent)
        path = _prompt_path(target)
    except ValueError as e:
        return {"error": str(e)}
    if not path.exists():
        return {"error": f"Prompt file not found: {target}.md"}
    content = path.read_text(encoding="utf-8")
    return {"name": target, "content": content, "sections": _list_sections(content)}


# ---------------------------------------------------------------------------
# Replace a section (staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Replace one whole section of a prompt with a complete new version.\n\n"
        "Call prompt_read first to see the section headings. Then pass the section "
        "`heading` (e.g. 'Style' or '## Style') and `new_section` — the FULL new "
        "text of that section, INCLUDING its `## Heading` line. Write the complete "
        "desired section; do NOT try to reproduce the old text. Only that section "
        "is touched; everything else is preserved automatically.\n\n"
        "Specialists edit their own prompt only. The general agent passes `name` "
        "to target any prompt. Staged — the user confirms before anything is written."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Target prompt (general only; specialists edit their own).",
            },
            "heading": {
                "type": "string",
                "description": "Heading of the section to replace, e.g. 'Style' or '## Style'.",
            },
            "new_section": {
                "type": "string",
                "description": "Complete new section text, starting with its '## Heading' line.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this change — what you observed and what you're fixing.",
            },
            "user_id": {"type": "integer"},
            "agent": {"type": "string"},
        },
        "required": ["heading", "new_section", "rationale"],
    },
    destructive=True,
    agent_scoped=True,
)
def prompt_replace_section(
    heading: str,
    new_section: str,
    rationale: str,
    user_id: int,
    agent: str = "",
    name: str | None = None,
) -> dict[str, Any]:
    try:
        target = _resolve_target(name, agent)
        path = _prompt_path(target)
    except ValueError as e:
        return {"error": str(e)}

    current = path.read_text(encoding="utf-8") if path.exists() else ""
    span = _find_section_span(current, heading)
    if span is None:
        return {
            "error": (
                f"Section {heading!r} not found in {target}.md. "
                f"Call prompt_read to see exact headings. Available: "
                f"{_list_sections(current)}"
            )
        }
    new_content = _apply_replace(current, heading, new_section)
    err = _validate(target, new_content)
    if err:
        return {"error": f"Refused: {err}."}

    start, end, _level, lines = span
    old_block = "".join(lines[start:end]).rstrip()
    preview = (
        f"📝 Replace section \"{heading}\" in {target}.md\n\n"
        f"Rationale: {rationale}\n\n"
        f"--- BEFORE ---\n{old_block}\n\n"
        f"--- AFTER ---\n{new_section.rstrip()}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="prompt_replace_section",
        arguments={"name": target, "op": "replace",
                   "heading": heading, "new_section": new_section},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


# ---------------------------------------------------------------------------
# Add a new section (staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Add a brand-new section to a prompt. Use this for genuinely new content "
        "(a new rule block, a new capability) — NOT to edit an existing section "
        "(use prompt_replace_section for that).\n\n"
        "`new_section` is the complete section INCLUDING its `## Heading` line. "
        "`after_heading` names the section it should follow; omit it (or pass "
        "'end') to append at the end of the file.\n\n"
        "Specialists edit their own prompt only. Staged — user confirms first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Target prompt (general only; specialists edit their own).",
            },
            "new_section": {
                "type": "string",
                "description": "Complete new section, starting with its '## Heading' line.",
            },
            "after_heading": {
                "type": "string",
                "description": "Insert after this section's heading; omit or 'end' to append at EOF.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this new section is needed.",
            },
            "user_id": {"type": "integer"},
            "agent": {"type": "string"},
        },
        "required": ["new_section", "rationale"],
    },
    destructive=True,
    agent_scoped=True,
)
def prompt_add_section(
    new_section: str,
    rationale: str,
    user_id: int,
    after_heading: str = "",
    agent: str = "",
    name: str | None = None,
) -> dict[str, Any]:
    try:
        target = _resolve_target(name, agent)
        path = _prompt_path(target)
    except ValueError as e:
        return {"error": str(e)}

    current = path.read_text(encoding="utf-8") if path.exists() else ""
    new_content = _apply_add(current, after_heading, new_section)
    if new_content is None:
        return {
            "error": (
                f"after_heading {after_heading!r} not found in {target}.md. "
                f"Available: {_list_sections(current)}"
            )
        }
    err = _validate(target, new_content)
    if err:
        return {"error": f"Refused: {err}."}

    where = f"after \"{after_heading}\"" if after_heading else "at end of file"
    preview = (
        f"📝 Add section to {target}.md ({where})\n\n"
        f"Rationale: {rationale}\n\n"
        f"--- NEW SECTION ---\n{new_section.rstrip()}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="prompt_add_section",
        arguments={"name": target, "op": "add",
                   "after_heading": after_heading, "new_section": new_section},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


# ---------------------------------------------------------------------------
# Executor (called by /confirm handler in main.py)
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Apply a confirmed section edit, with backup + post-write validation + rollback."""
    name = arguments.get("name")
    op = arguments.get("op")
    if not name or op not in ("replace", "add"):
        return {"ok": False, "error": f"Unsupported prompt action: op={op!r}"}

    try:
        path = _prompt_path(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    current = path.read_text(encoding="utf-8") if path.exists() else ""

    # Recompute against the CURRENT file (handles any drift since staging).
    if op == "replace":
        new_content = _apply_replace(current, arguments["heading"], arguments["new_section"])
        if new_content is None:
            return {"ok": False, "error":
                    f"Section {arguments['heading']!r} no longer exists in {name}.md."}
    else:  # add
        new_content = _apply_add(current, arguments.get("after_heading", ""), arguments["new_section"])
        if new_content is None:
            return {"ok": False, "error":
                    f"after_heading no longer exists in {name}.md."}

    # Validate BEFORE writing — never put garbage on disk.
    err = _validate(name, new_content)
    if err:
        return {"ok": False, "error": f"Refused: {err}."}

    # Snapshot the current file, then write.
    backup = _backup(name, current)
    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"write failed: {e}"}

    # Post-write paranoia: re-read and validate; roll back on any problem.
    written = path.read_text(encoding="utf-8")
    err2 = _validate(name, written)
    if err2:
        path.write_text(current, encoding="utf-8")  # rollback
        return {"ok": False, "error": f"validation failed after write, rolled back: {err2}"}

    return {"ok": True, "name": name, "bytes": len(new_content), "backup": backup.name}


# ---------------------------------------------------------------------------
# Tool need observation (not staged — just logs to a file)
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
            "agent":       {"type": "string", "enum": list(_get_agents()) + ["general"]},
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
    with needs_file.open("a", encoding="utf-8") as f:
        f.write(entry)
    return {"ok": True, "recorded": title}
