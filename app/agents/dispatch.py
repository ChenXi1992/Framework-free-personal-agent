"""Main dispatch entry point.

Replaces the direct `llm.chat()` call in main.py for messages that go through
the self-help agent system. Falls back to the default llm.chat() for 'general'
messages with no specific agent.

Flow:
    1. router.route(message) → {type, agent, category}
    2a. type == "note": classify → store note → optionally respond
    2b. type in {conversation, task} with agent: build persona context → agent llm.chat()
    2c. type == "general" or agent == "none": default llm.chat()
"""
from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Any

from .. import audit, db, debug_log
from ..config import DEBUG_LOG_AGENT_PROMPT
from ..db import _utc_now
from ..llm import ChatResult, chat
from . import router as _router

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"

# Messages at or below this word count are considered "ambiguous" and trigger
# the sticky-agent fallback. Not in config — it's a routing logic detail, not
# a user-facing tuning knob.
_STICKY_WORD_LIMIT = 5


def _today() -> str:
    """Return today's date as a human-readable string, e.g. 'Tuesday, May 13, 2026'.

    Injected into every system prompt so the LLM never has to guess the year
    when constructing calendar queries or date ranges.
    """
    return datetime.date.today().strftime("%A, %B %d, %Y")


def _build_general_prompt() -> str:
    """Build the system prompt for non-agent (general) messages.

    Injects the live tool list so the LLM can accurately answer meta-questions
    like "what can you do?" without hallucinating missing or extra capabilities.
    """
    from ..tools.registry import REGISTRY
    tool_names = ", ".join(REGISTRY.keys())
    return (
        f"Today is {_today()}. Use this for ALL date and calendar calculations — "
        "never guess the year from training data.\n\n"
        "You are Xi's personal assistant. Be concise, direct, and never sycophantic.\n"
        "\n"
        f"Your available tools right now: {tool_names}\n"
        "\n"
        "When asked what tools you have, list them from the line above — never claim "
        "you have no tools or make up capabilities you don't have. "
        "When asked about personal data (emails, calendar, Notion pages, files), "
        "call the relevant tool instead of guessing or refusing.\n"
        "\n"
        "Destructive tools (calendar_create_event, gmail_send_message, etc.) stage the "
        "action and return a preview — nothing happens until the user confirms. "
        "IMPORTANT: call the tool NOW in your current turn — never say 'reply and I'll "
        "create it' and skip the tool call. For multiple items (e.g. 3 calendar events), "
        "call the tool once per item in the same turn. After staging, tell the user what "
        "was staged and end with: 'Reply confirm to execute or cancel to discard.'"
    )


def _last_used_agent() -> str | None:
    """Return the name of the most recently active specialist agent, or None.

    Used to provide routing context for short, ambiguous follow-up messages
    (e.g. "Yes", "Sounds good", "Let's do it") that carry no agent signal on
    their own — the router needs to know what we were just talking about.
    """
    with db._connect() as conn:
        row = conn.execute(
            "SELECT agent FROM agent_conversations ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None


def handle(
    user_message: str,
    user_id: int,
    global_history: list[dict[str, str]],
) -> tuple[ChatResult, str]:
    """Process one user message through the routing + agent system.

    Returns (ChatResult, agent_name). agent_name is 'none' for general messages.
    The caller is responsible for storing turns in db.messages and calling
    store_turn() for agent conversations.

    Routing strategy:
      1. Ask the router to classify the message, passing the last-used agent as
         a context hint so short replies ("Yes", "OK") resolve correctly.
      2. Sticky fallback: if the router still returns agent=none AND the message
         is ≤ 5 words AND there is a recent agent conversation, inherit that agent.
         This handles bare acknowledgements like "confirm" or "go ahead".
    """
    last_agent = _last_used_agent()
    context_hint = (
        f"The previous conversation was with the {last_agent} agent."
        if last_agent else ""
    )
    route = _router.route(user_message, recent_context=context_hint)
    msg_type = route.get("type", "conversation")
    agent = route.get("agent", "none")
    category = route.get("category", "none")

    # Sticky-agent fallback: short ambiguous messages (≤ _STICKY_WORD_LIMIT words)
    # that the router can't classify (e.g. "Yes", "OK", "Let's do it") are
    # inherited by the last active agent. 5 is the empirical boundary below which
    # a message rarely has enough signal to route independently.
    sticky_fired = False
    if agent == "none" and last_agent and len(user_message.split()) <= _STICKY_WORD_LIMIT:
        log.debug(
            "Sticky agent fallback: short message '%s' → continuing %s agent",
            user_message, last_agent,
        )
        agent = last_agent
        sticky_fired = True

    log.debug("Route: type=%s agent=%s category=%s", msg_type, agent, category)

    # Log the final dispatch decision (after sticky fallback) so the audit log
    # shows the complete picture: route_decision = raw router output,
    # dispatch = what actually ran.
    audit.log_event(
        "dispatch",
        user_id=user_id,
        type=msg_type,
        agent=agent,
        category=category,
        sticky_fallback=sticky_fired,
        # When sticky fallback fires, router_agent shows what the router
        # originally returned before the override — useful for diagnosing
        # whether the router was wrong or the fallback was correct.
        router_agent=route.get("agent", "none"),
        last_agent=last_agent,
    )

    # --- Note intake ---
    if msg_type == "note":
        classified = _router.classify_note(user_message)
        note_category = classified.get("category", "uncategorized")
        note_summary = classified.get("summary", user_message[:80])

        with db._connect() as conn:
            conn.execute(
                "INSERT INTO notes(ts, created_at, raw_text, category, summary) VALUES (?,?,?,?,?)",
                (int(time.time()), _utc_now(), user_message, note_category, note_summary),
            )
        audit.log_event(
            "note_stored",
            user_id=user_id,
            category=note_category,
            summary=note_summary,
        )

        # Notes can also contain an action (e.g. "schedule a workout tomorrow").
        # If there's an agent, route to it for a response; otherwise ack simply.
        if agent == "none":
            return ChatResult(
                text=f"Got it — logged as {note_category}: {note_summary}"
            ), "none"

    # --- General (no specific agent) ---
    if agent == "none":
        result = chat(global_history, user_id=user_id,
                      system_prompt=_build_general_prompt(), agent="general")
        return result, "none"

    # --- Agent-specific ---
    persona_prompt = _build_persona(agent, category)
    agent_history = _router.agent_message_history(agent, limit=20)

    # Merge: start from agent's own history, append the new user message.
    messages_for_agent = [*agent_history, {"role": "user", "content": user_message}]

    result = chat(messages_for_agent, user_id=user_id,
                  system_prompt=persona_prompt, agent=agent)

    # Persist this turn in agent conversation memory
    _router.store_turn(agent, "user", user_message)
    _router.store_turn(agent, "assistant", result.text)

    return result, agent


def _build_persona(agent: str, category: str) -> str:
    """Combine an agent's persona file with its injected context block.

    The persona file (e.g. data/prompts/workout.md) provides the agent's
    voice and standing instructions.  The context block adds goals, recent
    notes, and conversation history — all dynamic, assembled each call.
    """
    agent_prompt = _load_prompt(agent)
    context = _router._build_agent_context(agent, category)

    parts = [agent_prompt]
    if context:
        parts.append("\n\n---\n\n" + context)
    persona_prompt = "\n".join(parts)

    # Debug: log the full combined system prompt (persona + context block)
    # so you can see exactly what the agent received, including the date,
    # recent notes, and conversation history injected into the context block.
    if DEBUG_LOG_AGENT_PROMPT:
        debug_log.log(
            "debug_agent_prompt",
            agent=agent,
            system_prompt=persona_prompt,
        )

    return persona_prompt


def _load_prompt(name: str) -> str:
    """Read data/prompts/<name>.md and return its text, or '' if it doesn't exist."""
    p = PROMPTS_DIR / f"{name}.md"
    return p.read_text() if p.exists() else ""
