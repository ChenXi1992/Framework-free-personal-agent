"""Two-pass agent router.

Pass 1 — Router LLM call:
    Classifies the user message into {type, agent, category, summary} using router.md.
    For notes, summary is a one-liner stored in the notes table.
    No separate classifier call needed — router handles both routing and summarisation.

Pass 2 — Agent LLM call (or direct reply for general):
    Builds context from per-agent history + relevant notes, injects persona prompt,
    runs the main tool-calling loop.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

from ..config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    DEBUG_LOG_ROUTER,
)
from .. import audit, db, debug_log
from ..db import _utc_now

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"


def _parse_json(raw: str) -> dict:
    """Extract and parse the first valid JSON object from a model response.

    Handles three common failure modes from DeepSeek:
      1. Markdown code fences  — ```json { … } ```
      2. Preamble / postamble  — "Sure! Here is the JSON: { … }"
      3. Truncated response    — {"type": "conver   (cut off by max_tokens)

    Strategy: try the most-specific extraction first, fall back progressively.
    Truncated JSON will still raise JSONDecodeError after all attempts — the
    caller (_llm_json) will retry the whole API call in that case.
    """
    candidates: list[str] = []

    # 1. Content inside ```...``` fences (most explicit signal from the model)
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            s = part.strip()
            if s.startswith("json"):
                s = s[4:].strip()
            if s.startswith("{"):
                candidates.append(s)

    # 2. Raw string (model obeyed the "no markdown" instruction)
    candidates.append(raw.strip())

    # 3. First {...} span — handles preamble/postamble text around the JSON
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start >= 0 and end > start:
        candidates.append(raw[start:end])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # All candidates failed — raise so the caller can decide whether to retry
    raise json.JSONDecodeError("No valid JSON object found in response", raw, 0)

_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


# ---------------------------------------------------------------------------
# Prompt loading helpers
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    """Read data/prompts/<name>.md and return its text, or '' if the file is missing.

    For the router prompt, replaces the {{AGENTS}} placeholder with the
    dynamically discovered agent list so new agents are picked up automatically.
    """
    p = PROMPTS_DIR / f"{name}.md"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if name == "router" and "{{AGENTS}}" in text:
        from .discovery import build_agents_block
        text = text.replace("{{AGENTS}}", build_agents_block())
    return text




# ---------------------------------------------------------------------------
# Pass 1: route
# ---------------------------------------------------------------------------

# Router-specific LLM constants — NOT in config.py because they should never
# be changed by the user. The router must be deterministic (temperature=0.0)
# and only needs a tiny output (max_tokens=200). Making these configurable
# would invite accidentally breaking the routing logic.
_ROUTER_TEMPERATURE = 0.0   # must stay 0 — random routing would be catastrophic
_ROUTER_MAX_TOKENS  = 500   # pure JSON fits in ~50 tokens, but models sometimes add preamble/thinking
                             # before the { — 500 gives enough room without wasting quota
_ROUTER_RETRIES     = 2     # DeepSeek occasionally returns empty or truncated on first attempt


def _llm_json(
    system: str, user: str, retries: int = _ROUTER_RETRIES
) -> tuple[dict, str, dict | None, int]:
    """Call the LLM expecting JSON output; retries on empty or unparseable responses.

    Uses deterministic settings (_ROUTER_TEMPERATURE=0.0) so routing
    decisions are consistent across identical inputs.

    Returns (parsed_result, raw_text, usage_dict_or_None, attempt_count).
    Callers unpack only what they need; the extra fields are used by debug
    logging in route().

    Retries on:
      - empty response (model returned nothing)
      - JSONDecodeError (model returned text that couldn't be parsed, e.g. truncated JSON)
    Does NOT retry on OpenAIError — those are API-level failures the caller handles.
    """
    last_raw = ""
    last_usage: dict | None = None
    for attempt in range(retries + 1):
        resp = _client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=_ROUTER_TEMPERATURE,
            max_tokens=_ROUTER_MAX_TOKENS,
        )
        last_raw = (resp.choices[0].message.content or "").strip()
        u = getattr(resp, "usage", None)
        last_usage = (
            {"prompt": u.prompt_tokens, "completion": u.completion_tokens, "total": u.total_tokens}
            if u else None
        )
        if not last_raw:
            log.warning("Router: empty response on attempt %d/%d, retrying", attempt + 1, retries + 1)
            continue
        try:
            return _parse_json(last_raw), last_raw, last_usage, attempt + 1
        except json.JSONDecodeError as e:
            log.warning(
                "Router: JSON parse failed on attempt %d/%d (%s), retrying — raw: %.120r",
                attempt + 1, retries + 1, e, last_raw,
            )
    raise ValueError(f"Router gave no valid JSON after {retries + 1} attempts. Last raw: {last_raw!r:.200}")


def route(user_message: str, recent_context: str = "") -> dict[str, str]:
    """Classify a user message. Returns {type, agent, category, summary}.

    `recent_context` is an optional hint (e.g. "The previous conversation was
    with the workout agent.") appended to the user message so the router can
    resolve short, ambiguous replies like "Yes" or "Sounds good" that carry no
    agent signal on their own.
    """
    prompt_text = user_message
    if recent_context:
        prompt_text = f"{user_message}\n\n[Context: {recent_context}]"

    router_system = _load_prompt("router")
    raw = ""
    usage: dict | None = None
    attempts = 0
    try:
        result, raw, usage, attempts = _llm_json(router_system, prompt_text)
    except (OpenAIError, json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("Router failed (%s: %s), defaulting to general/none", type(e).__name__, e)
        result = {"type": "chat", "agent": "none", "category": "none"}

    # Always: log the routing decision (summary) to the audit log.
    audit.log_event(
        "route_decision",
        input_preview=prompt_text[:200],
        result=result,
        had_context_hint=bool(recent_context),
    )

    # Debug: full router prompt + raw LLM response + token counts.
    if DEBUG_LOG_ROUTER:
        debug_log.log(
            "debug_router_call",
            system_prompt=router_system,
            user_input=prompt_text,
            raw_response=raw,
            parsed_result=result,
            usage=usage,
            attempts=attempts,
        )

    return result


# ---------------------------------------------------------------------------
# Context assembly for agents
# ---------------------------------------------------------------------------

def _build_agent_context(agent: str, category: str) -> str:
    """Build the context block injected into the agent system prompt.

    Always starts with today's date so the LLM uses the correct year for any
    date arithmetic or calendar calls.
    """
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    parts: list[str] = [
        f"## Today's date\n{today}",
        (
            "## Tool honesty rule — CRITICAL\n"
            "Your text responses NEVER change any data. Only tool calls do.\n"
            "NEVER say something was saved, added, updated, or logged unless you actually "
            "called the relevant tool in this same response and it returned ok.\n"
            "If you want to add a todo → call todo_add. "
            "If you want to save a note → call note_add. "
            "If you want to write a file → call file_write or file_append. "
            "Saying 'Done!' without calling the tool is a lie — do not do it.\n\n"
            "## Tool use rules\n"
            "Destructive tools (calendar_create_event, gmail_send_message, etc.) stage "
            "the action — nothing executes until the user confirms. "
            "Call the tool NOW in your current turn; never describe what you will do and "
            "skip the call. For multiple items (e.g. 3 calendar events), call the tool "
            "once per item in the same turn, then summarise what was staged.\n\n"
            "## Context files\n"
            "Background files about Xi (profile, goals, plans, etc.) are NOT loaded "
            "automatically. Call context_list() to see what exists, then "
            "context_load(filename) for any file relevant to the question. "
            "Skip files that aren't needed — only load what helps you answer."
        ),
    ]

    # Recent notes for this agent's domain.
    # The router returns a fine-grained `category` (e.g. "running", "sleep").
    # If that maps to a valid notes category we use it directly; otherwise we
    # fall back to the agent name itself (e.g. "workout") so we still get the
    # most relevant notes even when the category is vague or unrecognised.
    from .discovery import get_agents
    note_category = category if category not in ("none", "uncategorized") else agent
    if note_category in get_agents():
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT ts, summary, raw_text FROM notes "
                "WHERE category = ? ORDER BY ts DESC LIMIT 10",
                (note_category,),
            ).fetchall()
        if rows:
            note_lines = "\n".join(
                f"- [{r[0]}] {r[1]}" for r in rows
            )
            parts.append(f"## Recent {note_category} notes\n{note_lines}")

    # Auto-inject personal profile — always available so agents never start cold.
    # Silently skipped if the file doesn't exist (first-time setup / new user).
    data_dir = PROMPTS_DIR.parent
    profile_path = data_dir / "personal_profile.md"
    if profile_path.exists():
        parts.append(f"## Personal profile\n{profile_path.read_text(encoding='utf-8').strip()}")

    # Auto-inject agent-specific goals file (data/goals/<agent>.md) if it exists.
    # Each agent only sees its own domain goals — no cross-domain noise.
    goals_path = data_dir / "goals" / f"{agent}.md"
    if goals_path.exists():
        parts.append(f"## {agent.capitalize()} goals\n{goals_path.read_text(encoding='utf-8').strip()}")

    # NOTE: conversation history is NOT injected here as a text block.
    # dispatch.py passes the actual agent_conversations rows as properly-formatted
    # OpenAI messages (role/content pairs) in the messages array sent to the LLM.
    # Injecting a truncated text digest here as well would duplicate the same data,
    # waste tokens on every call, and risk confusing the model with two versions.

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Agent history (OpenAI message format)
# ---------------------------------------------------------------------------

def agent_message_history(agent: str, limit: int = 20) -> list[dict[str, str]]:
    """Return recent agent turns as OpenAI-format messages."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM agent_conversations "
            "WHERE agent = ? ORDER BY ts DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Store agent turn
# ---------------------------------------------------------------------------

def store_turn(agent: str, role: str, content: str) -> None:
    """Append one turn (user or assistant) to the agent's conversation history."""
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO agent_conversations(ts, created_at, agent, role, content) VALUES (?,?,?,?,?)",
            (int(time.time()), _utc_now(), agent, role, content),
        )
