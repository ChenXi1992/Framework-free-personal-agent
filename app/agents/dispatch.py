"""Agent dispatch: routes each user message to the right specialist agent.

Every message from main.py flows through handle() here. This module owns the
full pipeline from raw text to ChatResult:

  1. Router classification
     router.route(message) → {type, agent, category}
     - "note"  → store to DB immediately; agent responds without note_add tool
     - "diary" → agent handles everything (diary_add + note_add + response)
     - "chat"  → normal conversation with the matched specialist agent
     - "grill" → agent switches to examiner mode (one question at a time, feedback, weak-spot notes)
     - agent == "none" → general LLM with no specialist persona

  2. Sticky-agent fallback
     Short ambiguous messages (≤ 5 words: "Yes", "OK", "Go ahead") are
     forwarded to the last active agent when the router returns none,
     so follow-ups resolve in context rather than falling through to general.

  3. Conversation summarisation (no-op unless history is long)
     Fires before building the persona so any newly generated summary is
     already in the system prompt for this very call.

  4. Persona + context assembly
     Combines the agent's persona file (data/prompts/<agent>.md) with a
     dynamically built context block: today's date, recent notes, personal
     profile, goals, and the rolling conversation summary.

  5. LLM call (llm.chat) using the agent's own message history (last 20 turns).

  6. Persist the exchange and return (ChatResult, agent_name).
"""
from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Callable

from .. import audit, db, debug_log
from ..config import (
    DEBUG_LOG_AGENT_PROMPT, DEBUG_LOG_DISPATCH,
    DIARY_DEFAULT_AGENT, GRILL_DEFAULT_AGENT,
    SHOW_AGENT_NAME,
)
from ..db import _utc_now
from ..llm import ChatResult, ToolInvocation, chat
from ..tools.notes import note_add as _note_add
from . import router as _router
from .discovery import get_agents as _get_agents

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
    """Build the system prompt for non-agent (general) messages."""
    return (
        f"Today is {_today()}. Use this for ALL date and calendar calculations — "
        "never guess the year from training data.\n\n"
        "You are Xi's personal assistant. Be concise, direct, and never sycophantic.\n"
        "\n"
        "When asked what tools you have, call tool_need() or check the structured tool "
        "list available to you — never claim you have no tools or make up capabilities. "
        "When asked about personal data (emails, calendar, Notion pages, files), "
        "call the relevant tool instead of guessing or refusing.\n"
        "\n"
        "## Tool honesty rule — CRITICAL\n"
        "Your text responses NEVER change any data. Only tool calls do.\n"
        "NEVER say something was saved, added, updated, or logged unless you actually "
        "called the relevant tool in this same response and it returned ok.\n"
        "If you want to add a todo item → call todo_add. "
        "If you want to save a note → call note_add. "
        "If you want to write a file → call file_write or file_append. "
        "Saying 'Done!' without calling the tool is a lie — do not do it.\n"
        "\n"
        "Destructive tools (calendar_create_event, gmail_send_message, etc.) stage the "
        "action and return a preview — nothing happens until the user confirms. "
        "IMPORTANT: call the tool NOW in your current turn — never say 'reply and I'll "
        "create it' and skip the tool call. For multiple items (e.g. 3 calendar events), "
        "call the tool once per item in the same turn. After staging, tell the user what "
        "was staged and end with: 'Reply confirm to execute or cancel to discard.'"
    )


# ---------------------------------------------------------------------------
# Per-agent tool exclusions
# ---------------------------------------------------------------------------

# Each specialist agent sees only the tools relevant to its domain. This cuts
# token usage, hallucinated calls, and context noise.
#
# Design: allow-list, not deny-list. The "sensitive" capability groups below
# are excluded for EVERY agent by default; an agent only sees a group if it is
# explicitly granted access in _GROUP_ALLOW. This means a newly-added agent is
# safe by default (no Gmail / Notion / health) until it's deliberately granted —
# rather than silently inheriting the full firehose.

_GMAIL_TOOLS = frozenset({
    "gmail_search", "gmail_get_message", "gmail_create_draft",
    "gmail_send_message", "gmail_trash_message",
})
_NOTION_TOOLS = frozenset({
    "notion_search", "notion_get_page", "notion_append_paragraph",
    "notion_create_page", "notion_archive_page",
})
_HEALTH_TOOLS = frozenset({
    "health_daily_summary", "health_sport_breakdown",
    "health_heart_rate", "health_workout_sessions",
})

# Capability group → set of agents allowed to use it. Agents absent from a
# group's set have that group's tools excluded.
_GROUP_ALLOW: dict[str, frozenset[str]] = {
    "gmail":  frozenset({"career", "general"}),
    "notion": frozenset({"career", "general"}),
    "health": frozenset({"workout", "general"}),
}
_GROUP_TOOLS: dict[str, frozenset[str]] = {
    "gmail":  _GMAIL_TOOLS,
    "notion": _NOTION_TOOLS,
    "health": _HEALTH_TOOLS,
}
# Agents granted only a subset of a group's tools (finer than group-level).
# Lifestyle tracks daily activity but doesn't need the full sports breakdown.
_GROUP_PARTIAL: dict[tuple[str, str], frozenset[str]] = {
    ("health", "lifestyle"): frozenset({"health_daily_summary"}),
}


def _excluded_tools_for(agent: str) -> frozenset[str]:
    """Return the set of tool names hidden from `agent`.

    Allow-list semantics: a capability group is excluded unless the agent is in
    _GROUP_ALLOW for that group. Partial grants (_GROUP_PARTIAL) let an agent
    keep just a few tools from an otherwise-excluded group.
    """
    excluded: set[str] = set()
    for group, tools in _GROUP_TOOLS.items():
        if agent in _GROUP_ALLOW.get(group, frozenset()):
            continue  # full access granted
        partial = _GROUP_PARTIAL.get((group, agent))
        if partial is not None:
            excluded |= (tools - partial)  # keep the partial grant
        else:
            excluded |= tools              # exclude the whole group
    return frozenset(excluded)


# Only inherit the last agent if a conversation happened within this window.
# Prevents stale inheritance — e.g. a workout conversation from this morning
# should not pull in an unrelated "yes" response this evening.
_STICKY_MAX_AGE_SECONDS = 3600  # 1 hour


def _last_used_agent() -> tuple[str | None, str]:
    """Return (agent_name, 'chat') for the most recently active conversation.

    Reads from messages.agent. Excludes 'general' — it's a pass-through, not a
    specialist. Returns (None, 'chat') when no recent conversation exists or the
    last one is older than _STICKY_MAX_AGE_SECONDS (1 hour).
    """
    with db._connect() as conn:
        row = conn.execute(
            """
            SELECT agent, MAX(ts) as ts
            FROM messages
            WHERE agent IS NOT NULL AND agent != 'general'
            GROUP BY agent
            ORDER BY MAX(ts) DESC LIMIT 1
            """
        ).fetchone()
    if not row:
        return None, "chat"
    agent, ts = row
    if (time.time() - ts) > _STICKY_MAX_AGE_SECONDS:
        return None, "chat"
    return agent, "chat"  # grill is now an agent skill — no special msg_type needed


def handle(
    user_message: str,
    user_id: int,
    global_history: list[dict[str, str]],
    on_chunk: Callable[[list[ToolInvocation], str, str], None] | None = None,
) -> tuple[ChatResult, str]:
    """Process one user message through the routing + agent system.

    Returns (ChatResult, agent_name). agent_name is 'none' for general messages.
    The caller persists turns to db.messages and tags them via tag_last_exchange().

    Routing strategy:
      1. Ask the router to classify the message, passing the last-used agent as
         a context hint so short replies ("Yes", "OK") resolve correctly.
      2. Sticky fallback: if the router still returns agent=none AND the message
         is ≤ 5 words AND there is a recent agent conversation, inherit that agent.
         This handles bare acknowledgements like "confirm" or "go ahead".
    """
    last_agent, last_msg_type = _last_used_agent()

    # Pass the last 3 turns from the messages table as structured history so
    # the router can resolve corrections ("that's wrong"), follow-ups, and
    # mid-conversation topic switches that carry no domain signal on their own.
    # The messages table includes the general agent's turns — the old one-line
    # hint only tracked the last
    # specialist, so corrections about general-agent responses went to the wrong agent.
    router_history: list[dict] = []
    with db._connect() as _conn:
        _rows = _conn.execute(
            """
            SELECT role, content FROM messages
            WHERE user_id = ?
              AND role IN ('user', 'assistant')
            ORDER BY ts DESC LIMIT 6
            """,
            (user_id,),
        ).fetchall()
    # Reverse to chronological order, keep at most last 3 exchanges (6 rows)
    for role, content in reversed(_rows):
        router_history.append({"role": role, "content": content[:400]})

    route = _router.route(user_message, history=router_history or None)
    msg_type = route.get("type", "chat")
    agent    = route.get("agent", "none")
    # Normalise category: router may return "none" for chat-type messages;
    # use "uncategorized" as the canonical unknown-category value throughout.
    category = route.get("category") or "uncategorized"

    # Sticky-agent fallback: short ambiguous messages (≤ _STICKY_WORD_LIMIT words)
    # that the router can't classify (e.g. "Yes", "OK", "Let's do it") are
    # inherited by the last active agent.
    #
    # Grill exception: during an active grill session answers can be long
    # ("I think the verb moves to the end because it's a subordinate clause").
    # When the last stored turn was grill we skip the word-count check entirely
    # so every answer — regardless of length — stays with the correct specialist.
    sticky_fired = False
    if agent == "none" and last_agent:
        if len(user_message.split()) <= _STICKY_WORD_LIMIT:
            log.debug(
                "Sticky agent fallback: '%s' → continuing %s agent",
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

    # Debug: full routing decision with all context so every dispatch is traceable.
    if DEBUG_LOG_DISPATCH:
        _domain_excluded = _excluded_tools_for(agent)
        debug_log.log(
            "debug_dispatch",
            user_message=user_message[:200],
            msg_type=msg_type,
            router_agent=route.get("agent", "none"),
            resolved_agent=agent,
            category=category,
            sticky_fired=sticky_fired,
            last_agent=last_agent,
            tools_excluded=sorted(_domain_excluded),
            tools_excluded_count=len(_domain_excluded),
        )

    # --- Note intake ---
    if msg_type == "note":
        # Use the router's category when it's a valid agent name.
        # Fall back to the (sticky-resolved) agent name when the router said
        # "none"/"uncategorized" — e.g. the router couldn't classify the agent
        # but sticky overrode it to "workout", so the note belongs to workout.
        _raw_cat = route.get("category") or "uncategorized"
        note_category = _raw_cat if _raw_cat in _get_agents() else (
            agent if agent != "none" else "uncategorized"
        )
        note_summary = route.get("summary") or user_message[:80]

        # Route through note_add() so the deduplication guard fires — prevents
        # double-storing the same message when the agent also calls note_add.
        _note_add(text=user_message, category=note_category, summary=note_summary)
        audit.log_event(
            "note_stored",
            user_id=user_id,
            category=note_category,
            summary=note_summary,
        )

        # Notes can also contain an action (e.g. "schedule a workout tomorrow").
        # If there's an agent, route to it for a response; otherwise ack simply.
        if agent == "none":
            ack = f"Got it — logged as {note_category}: {note_summary}"
            # Tagging handled by main.py via tag_last_exchange().
            return ChatResult(text=ack), "none"

    # --- Diary intake ---
    # Diary entries are routed to the relevant agent, which is responsible for:
    #   1. Calling diary_add (writes the raw narrative to diary.md)
    #   2. Optionally calling note_add, todo_add, etc. to extract structured items
    #   3. Responding to the user
    # We don't pre-store anything here — the agent drives the whole flow via tools.
    # When the router can't assign a specific agent (agent=="none"), we fall back to
    # the lifestyle agent, since diary entries are almost always personal narrative.
    if msg_type == "diary":
        if agent == "none":
            agent = DIARY_DEFAULT_AGENT
            log.debug("Diary entry with no specific agent — defaulting to %s agent", agent)

    # Grill mode is now an agent skill — every agent's context block contains
    # grill instructions and the agent decides when to switch modes.
    # No special routing needed here.

    # --- General (no specific agent) ---
    if agent == "none":
        # Emit the agent label so general replies are marked too — matches the
        # specialist path; without this, general answers had no 🤖 header.
        if on_chunk and SHOW_AGENT_NAME:
            on_chunk([], "🤖 general", "")
        result = chat(global_history, user_id=user_id,
                      system_prompt=_build_general_prompt(), agent="general",
                      on_chunk=on_chunk)
        # Tagging handled by main.py via tag_last_exchange().
        return result, "none"

    # --- Agent-specific ---

    # Summarise old turns if history is getting long (no-op if under threshold).
    # Must run BEFORE _build_persona so the summary is already stored when
    # _build_agent_context() calls get_agent_summary() to inject it into the
    # system prompt this same call.
    did_summarise = _router.summarise_if_needed(agent)

    persona_prompt = _build_persona(agent, category)
    agent_history = _router.agent_message_history(agent, limit=20)

    # For diary entries, prepend a hidden system note so the agent knows to call
    # diary_add. Without this, the agent receives the same input as a normal
    # conversation message and has no signal to write to diary.md.
    if msg_type == "diary":
        content_for_agent = (
            "[Diary entry detected — follow this order:\n"
            "1. Call diary_add() to write the narrative to diary.md.\n"
            "2. Call note_add(category=<most relevant category>, summary=<one sentence>) "
            "so the entry appears in your context on future calls. "
            "Without this, the diary entry won't be visible in future conversations.\n"
            "3. If any actionable items are present (todos, goals), call todo_add / "
            "file_append as appropriate.\n"
            "4. Then respond naturally.]\n\n"
            + user_message
        )
    else:
        # type == "note": note already stored by dispatch; note_add excluded below.
        # type == "chat": plain conversation, no special wrapping needed.
        content_for_agent = user_message

    # Recent cross-agent context: give this agent the last few turns that were
    # handled by OTHER agents (general or another specialist), so it knows what
    # was just discussed elsewhere. Turns from THIS agent are skipped — they're
    # already in agent_history below, so including them here would duplicate.
    # This subsumes the old switch-only "context bridge".
    #
    # The current user message is already logged by main.py (newest row), so we
    # fetch 7 and drop it to look at the turns *before* it.
    with db._connect() as _conn:
        _recent = _conn.execute(
            f"""
            SELECT role, content, agent FROM messages
            WHERE user_id = ? AND role IN ('user', 'assistant')
              AND ts > {_router._RESET_CUTOFF_SQL}
            ORDER BY ts DESC LIMIT 7
            """,
            (user_id,),
        ).fetchall()
    prior_turns = list(reversed(_recent[1:]))  # drop current msg, chronological
    # Keep only turns NOT already in this agent's own history (cross-agent only).
    cross_turns = [t for t in prior_turns if (t[2] or "general") != agent]
    if cross_turns:
        ctx_lines = [
            f"  [{('YOU' if role == 'user' else (turn_agent or 'general'))}]: {content[:200]}"
            for role, content, turn_agent in cross_turns
        ]
        content_for_agent = (
            "[Recent conversation handled by other agents:\n"
            + "\n".join(ctx_lines)
            + "\n---]\n\n"
            + content_for_agent
        )
        if DEBUG_LOG_DISPATCH:
            debug_log.log(
                "debug_recent_context",
                to_agent=agent,
                turns_injected=len(cross_turns),
                agents_seen=sorted({(t[2] or "general") for t in cross_turns}),
            )

    # Merge: start from agent's own history, append the new user message.
    messages_for_agent = [*agent_history, {"role": "user", "content": content_for_agent}]

    # Build the combined exclusion set:
    #   1. note_add excluded when the note is pre-stored by dispatch (avoids duplicate)
    #   2. Domain-specific tools irrelevant to this agent (reduces schema size + noise)
    note_excluded = frozenset({"note_add"}) if msg_type == "note" else frozenset()
    domain_excluded = _excluded_tools_for(agent)
    excluded = note_excluded | domain_excluded

    # Show agent label before the first chunk so the user knows which
    # specialist is responding — fires only when SHOW_AGENT_NAME=true.
    display_agent = agent if agent != "none" else "general"
    if on_chunk and SHOW_AGENT_NAME:
        on_chunk([], f"🤖 {display_agent}", "")

    result = chat(messages_for_agent, user_id=user_id,
                  system_prompt=persona_prompt, agent=agent,
                  exclude_tools=excluded, on_chunk=on_chunk)

    # Persist this turn — tagging is handled by main.py via tag_last_exchange()
    # which writes agent info directly into the messages table (single source of truth).

    # If summarisation fired this turn, notify the user.
    # When on_chunk is active all content goes through that channel — we send
    # the note via on_chunk so it reaches Telegram immediately. When on_chunk
    # is absent (fallback path) we append to result.text instead.
    if did_summarise:
        summary_data = _router.get_agent_summary(agent)
        count = summary_data["turns_count"] if summary_data else "?"
        note = f"_📝 Summarised {count} turns into memory._"
        if on_chunk:
            on_chunk([], note, "")
        result = ChatResult(
            text=result.text + f"\n\n{note}",
            invocations=result.invocations,
            interim_texts=result.interim_texts,
        )

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
    """Read data/prompts/<name>.md and return its text, or '' if it doesn't exist.

    Logs a warning when the file is missing or empty — an agent running with
    no persona prompt behaves like a generic LLM and ignores its domain role.
    This is what caused the workout agent to silently route everything to general
    when its prompt was accidentally cleared.
    """
    p = PROMPTS_DIR / f"{name}.md"
    if not p.exists():
        log.warning(
            "Agent prompt file missing: %s — agent '%s' will run with no persona",
            p, name,
        )
        return ""
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        log.warning(
            "Agent prompt file is empty: %s — agent '%s' will run with no persona",
            p, name,
        )
    return text
