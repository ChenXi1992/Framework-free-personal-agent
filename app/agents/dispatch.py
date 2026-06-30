"""Agent dispatch: routes each user message to the right specialist agent.

Every message from main.py flows through handle() here. This module owns the
full pipeline from raw text to ChatResult:

  1. Router classification
     router.route(message) → {agent}
     - agent == "none" → general LLM with no specialist persona
     - agent == specialist → routed to that agent's persona + context

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
    SHOW_AGENT_NAME,
)
from ..db import _utc_now
from ..llm import ChatResult, ToolInvocation, chat
from . import router as _router

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"

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
        "You are Xi's general-purpose personal assistant. You are NOT a specialist "
        "agent — not workout, not lifestyle, not growth, not career, not dutch. "
        "Never claim to belong to any specialist domain, even if the recent "
        "conversation was in that domain.\n\n"
        "When Xi says a message should go to a specialist agent, do NOT call "
        "agent_handoff — that is an async note, not a re-route. Instead, tell Xi "
        "to re-send their log clearly (e.g. '7h sleep' or '70.5kg') so the system "
        "routes it automatically to the right specialist.\n\n"
        "Be concise, direct, and never sycophantic.\n"
        "\n"
        "When asked what tools you have, call tool_need() or check the structured tool "
        "list available to you — never claim you have no tools or make up capabilities. "
        "When asked about personal data (emails, calendar, Notion pages, files), "
        "call the relevant tool instead of guessing or refusing.\n"
        "\n"
        "## Response format\n"
        "Keep replies conversational, not documentary.\n"
        "- Default: plain prose or short bullets. No ## section headers, no --- dividers, "
        "no markdown tables in a normal reply.\n"
        "- Structure (tables, headers, breakdowns) only when Xi explicitly asks for an "
        "analysis, plan, or report.\n"
        "- Length: ≤250 words for a typical reply. Expand only when the question genuinely "
        "requires it — not by habit.\n"
        "- No preamble: start with the answer. Drop openers like 'Here's what I found:' "
        "or 'Let me break this down.'\n"
        "\n"
        "## Tool honesty rule — CRITICAL\n"
        "Your text responses NEVER change any data. Only tool calls do.\n"
        "NEVER say something was saved, added, updated, or logged unless you actually "
        "called the relevant tool in this same response and it returned ok.\n"
        "If you want to add a todo item → call todo_add. "
        "If you want to write a file → call file_write or file_append. "
        "Saying 'Done!' without calling the tool is a lie — do not do it.\n"
        "\n"
        "Destructive tools (calendar_create_event, gmail_send_message, etc.) stage the "
        "action and return a preview — nothing happens until the user confirms. "
        "IMPORTANT: call the tool NOW in your current turn — never say 'reply and I'll "
        "create it' and skip the tool call. For multiple items (e.g. 3 calendar events), "
        "call the tool once per item in the same turn. After staging, tell the user what "
        "was staged and end with: 'Reply confirm to execute or cancel to discard.'\n"
        "\n"
        "## Declare missing capabilities BEFORE acting — CRITICAL\n"
        "Before producing any plan, proposal, or analysis that depends on a tool, "
        "call that tool first. If the tool returns an error or auth failure, say so "
        "explicitly at the top of your response BEFORE the plan: "
        "'⚠️ [tool] unavailable ([reason]) — proceeding without it.'\n"
        "NEVER silently omit a constraint you can't verify. Concretely:\n"
        "- Planning a workout schedule? Call calendar_list_events() first. "
        "If it fails, state '⚠️ Calendar unavailable — I can't check your real schedule. "
        "Tell me your blocked days and I'll plan around those.'\n"
        "- Making a recommendation based on history? Call notes_recent() first. "
        "If there are no notes, say so — don't fabricate a history.\n"
        "This rule also applies to specialist agents standing in for each other.\n"
        "\n"
        # The general agent handles generic reminders ("remind me to get grocery
        # at 5pm Tuesday") that carry no specialist signal, so it needs the same
        # alerts guidance the specialists get. Injected dynamically rather than
        # duplicated inline so there's a single source of truth in skills/alerts.md.
        + _router._load_prompt("alerts")
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

# Capability group → set of agents allowed to use it. Agents absent from a
# group's set have that group's tools excluded.
_GROUP_ALLOW: dict[str, frozenset[str]] = {
    "gmail":  frozenset({"career", "general"}),
    "notion": frozenset({"career", "general"}),
}
_GROUP_TOOLS: dict[str, frozenset[str]] = {
    "gmail":  _GMAIL_TOOLS,
    "notion": _NOTION_TOOLS,
}
_GROUP_PARTIAL: dict[tuple[str, str], frozenset[str]] = {}


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


# Only read back recent agent history within this window — prevents a
# workout conversation from this morning polluting an unrelated evening session.
_RECENT_AGENT_WINDOW = 3600  # 1 hour


def _last_used_agent() -> str | None:
    """Return the most recently active specialist agent name, or None.

    Excludes 'general' — it's a pass-through, not a specialist.
    Returns None when no recent conversation exists or the last one is
    older than _RECENT_AGENT_WINDOW (1 hour).
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
        return None
    agent, ts = row
    if (time.time() - ts) > _RECENT_AGENT_WINDOW:
        return None
    return agent


def handle(
    user_message: str,
    user_id: int,
    global_history: list[dict[str, str]],
    on_chunk: Callable[[list[ToolInvocation], str, str], None] | None = None,
) -> tuple[ChatResult, str]:
    """Process one user message through the routing + agent system.

    Returns (ChatResult, agent_name). agent_name is 'none' for general messages.
    The caller persists turns to db.messages and tags them via tag_last_exchange().

    The router receives the last 3 conversation exchanges as history so it can
    resolve follow-ups, corrections, and topic switches on its own.
    """
    last_agent = _last_used_agent()

    # Pass the last 3 turns from the messages table as structured history so
    # the router can resolve corrections ("that's wrong"), follow-ups, and
    # mid-conversation topic switches that carry no domain signal on their own.
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

    route = _router.route(user_message, history=router_history)
    agent = route.get("agent", "none")

    log.debug("Route: agent=%s", agent)

    audit.log_event(
        "dispatch",
        user_id=user_id,
        agent=agent,
        last_agent=last_agent,
    )

    if DEBUG_LOG_DISPATCH:
        _domain_excluded = _excluded_tools_for(agent)
        debug_log.log(
            "debug_dispatch",
            user_message=user_message[:200],
            resolved_agent=agent,
            last_agent=last_agent,
            tools_excluded=sorted(_domain_excluded),
            tools_excluded_count=len(_domain_excluded),
        )

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
    # General agent is excluded: its history is too broad to summarise usefully.
    did_summarise = False if agent == "general" else _router.summarise_if_needed(agent)

    persona_prompt = _build_persona(agent)
    agent_history = _router.agent_message_history(agent, limit=20)

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

    # Exclude domain-specific tools irrelevant to this agent (reduces schema size + noise).
    excluded = _excluded_tools_for(agent)

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


def _build_persona(agent: str) -> str:
    """Combine an agent's persona file with its injected context block.

    The persona file (e.g. data/prompts/workout.md) provides the agent's
    voice and standing instructions.  The context block adds goals, recent
    notes, and conversation history — all dynamic, assembled each call.
    """
    agent_prompt = _load_prompt(agent)
    context = _router._build_agent_context(agent)

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
