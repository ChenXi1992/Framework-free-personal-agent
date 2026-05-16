"""Two-pass agent router.

Pass 1 — Router LLM call:
    Classifies the user message into {type, agent, category} using router.md prompt.

Pass 2 — Agent LLM call (or direct reply for general):
    Builds context from per-agent history + relevant notes, injects persona prompt,
    runs the main tool-calling loop.

Note intake side-effect:
    When type == "note", the router first classifies it with classifier.md,
    stores it in the notes table, then hands off to the relevant agent for a response.
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
    DEBUG_LOG_ROUTER, DEBUG_LOG_CLASSIFIER,
)
from .. import audit, db, debug_log
from ..db import _utc_now

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"


def _parse_json(raw: str) -> dict:
    """Extract and parse the first valid JSON object from a model response.

    DeepSeek sometimes wraps its JSON in markdown code fences (```json … ```).
    We scan all fence segments first, then fall back to the raw string.
    """
    # Strip code fences like ```json ... ```
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            s = part.strip()
            if s.startswith("json"):
                s = s[4:].strip()
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    # Try the raw string directly
    return json.loads(raw)

_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


# ---------------------------------------------------------------------------
# Prompt loading helpers
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    """Read data/prompts/<name>.md and return its text, or '' if the file is missing."""
    p = PROMPTS_DIR / f"{name}.md"
    return p.read_text() if p.exists() else ""




# ---------------------------------------------------------------------------
# Pass 1: route
# ---------------------------------------------------------------------------

# Router-specific LLM constants — NOT in config.py because they should never
# be changed by the user. The router must be deterministic (temperature=0.0)
# and only needs a tiny output (max_tokens=200). Making these configurable
# would invite accidentally breaking the routing logic.
_ROUTER_TEMPERATURE = 0.0   # must stay 0 — random routing would be catastrophic
_ROUTER_MAX_TOKENS  = 200   # a JSON object like {"type":"note","agent":"workout",...} fits in ~50 tokens
_ROUTER_RETRIES     = 2     # DeepSeek occasionally returns empty on first attempt under load


def _llm_json(
    system: str, user: str, retries: int = _ROUTER_RETRIES
) -> tuple[dict, str, dict | None, int]:
    """Call the LLM expecting JSON output; retries on empty response.

    Uses deterministic settings (_ROUTER_TEMPERATURE=0.0) so routing
    decisions are consistent across identical inputs.

    Returns (parsed_result, raw_text, usage_dict_or_None, attempt_count).
    Callers unpack only what they need; the extra fields are used by debug
    logging in route() and classify_note().
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
        if last_raw:
            return _parse_json(last_raw), last_raw, last_usage, attempt + 1
        log.warning("Router: empty response on attempt %d/%d, retrying", attempt + 1, retries + 1)
    raise ValueError("Empty response after retries")


def route(user_message: str, recent_context: str = "") -> dict[str, str]:
    """Classify a user message. Returns {type, agent, category}.

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
        result = {"type": "conversation", "agent": "none", "category": "none"}

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
# Note classification
# ---------------------------------------------------------------------------

def classify_note(text: str) -> dict[str, str]:
    """Run the note classifier. Returns {category, summary}."""
    classifier_system = _load_prompt("classifier")
    raw = ""
    usage: dict | None = None
    attempts = 0
    try:
        result, raw, usage, attempts = _llm_json(classifier_system, text)
    except (OpenAIError, json.JSONDecodeError, ValueError) as e:
        log.warning("Classifier failed (%s: %s), defaulting to uncategorized", type(e).__name__, e)
        result = {"category": "uncategorized", "summary": text[:80]}

    audit.log_event("note_classified", input_preview=text[:200], result=result)

    if DEBUG_LOG_CLASSIFIER:
        debug_log.log(
            "debug_classifier_call",
            system_prompt=classifier_system,
            user_input=text,
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
    note_category = category if category not in ("none", "uncategorized") else agent
    if note_category in ("workout", "lifestyle", "career"):
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

    # Agent conversation memory
    with db._connect() as conn:
        turns = conn.execute(
            "SELECT role, content FROM agent_conversations "
            "WHERE agent = ? ORDER BY ts DESC LIMIT 20",
            (agent,),
        ).fetchall()
    if turns:
        history_lines = "\n".join(
            f"{r[0].upper()}: {r[1][:200]}" for r in reversed(turns)
        )
        parts.append(f"## Recent conversation history with {agent} agent\n{history_lines}")

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
