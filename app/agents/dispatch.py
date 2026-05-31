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
from ..config import DEBUG_LOG_AGENT_PROMPT, DIARY_DEFAULT_AGENT, GRILL_DEFAULT_AGENT
from ..db import _utc_now
from ..llm import ChatResult, ToolInvocation, chat
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


# Only inherit the last agent if a conversation happened within this window.
# Prevents stale inheritance — e.g. a workout conversation from this morning
# should not pull in an unrelated "yes" response this evening.
_STICKY_MAX_AGE_SECONDS = 3600  # 1 hour


def _last_used_agent() -> tuple[str | None, str]:
    """Return (agent_name, msg_type) for the most recently active conversation.

    Returns (None, 'chat') when no recent conversation exists or the last one
    is older than _STICKY_MAX_AGE_SECONDS (1 hour).

    `msg_type` is used by the sticky fallback to detect ongoing grill sessions:
    grill answers can be long, so the normal 5-word limit must be bypassed for
    the duration of the session.
    """
    with db._connect() as conn:
        # Exclude "general" — it is a pass-through, not a specialist, so it
        # must not block a real specialist from being inherited. Without this,
        # a general exchange between two specialist turns would reset the sticky
        # context and lose the specialist chain.
        row = conn.execute(
            "SELECT agent, ts, msg_type FROM agent_conversations "
            "WHERE agent != 'general' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None, "chat"
    agent, ts, msg_type = row
    if (time.time() - ts) > _STICKY_MAX_AGE_SECONDS:
        return None, "chat"
    return agent, (msg_type or "chat")


def handle(
    user_message: str,
    user_id: int,
    global_history: list[dict[str, str]],
    on_chunk: Callable[[list[ToolInvocation], str], None] | None = None,
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
    last_agent, last_msg_type = _last_used_agent()
    context_hint = (
        f"The previous conversation was with the {last_agent} agent."
        if last_agent else ""
    )
    route = _router.route(user_message, recent_context=context_hint)
    msg_type = route.get("type", "chat")
    agent = route.get("agent", "none")
    category = route.get("category", "none")

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
        is_grill_followup = last_msg_type == "grill"
        if is_grill_followup or len(user_message.split()) <= _STICKY_WORD_LIMIT:
            log.debug(
                "Sticky agent fallback: '%s' → continuing %s agent (grill_followup=%s)",
                user_message, last_agent, is_grill_followup,
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
        note_category = route.get("category", "uncategorized")
        note_summary = route.get("summary") or user_message[:80]

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

    # --- Grill intake ---
    # "Grill me" messages switch the agent from assistant → examiner mode.
    # The agent uses its domain notes + goals to generate targeted challenge questions,
    # gives feedback on each answer, and tracks weak spots via note_add.
    # When no specific agent is identified, fall back to GRILL_DEFAULT_AGENT (growth)
    # since "grill me" with no context is most naturally a self-improvement challenge.
    if msg_type == "grill":
        if agent == "none":
            agent = GRILL_DEFAULT_AGENT
            log.debug("Grill with no specific agent — defaulting to %s agent", agent)

    # --- General (no specific agent) ---
    if agent == "none":
        result = chat(global_history, user_id=user_id,
                      system_prompt=_build_general_prompt(), agent="general",
                      on_chunk=on_chunk)
        # Persist the general conversation so conversation_recent(agent="general")
        # can retrieve it. Specialist agents store their turns at the bottom of
        # this function — the general path was missing these calls entirely.
        _router.store_turn("general", "user", user_message, "chat")
        _router.store_turn("general", "assistant", result.text, "chat")
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
    elif msg_type == "grill":
        # Switch the agent into examiner mode. The agent's existing context block
        # (notes + goals) already contains the domain knowledge to draw from.
        # The instructions are injected as a user-turn prefix so the agent sees
        # them as an explicit directive, not background system context.
        content_for_agent = (
            "[GRILL MODE — you are now an examiner, not an assistant. Rules:\n"
            "1. Review your notes and goals context to identify areas to probe.\n"
            "2. Ask ONE focused, challenging question to start. Do not ask multiple at once.\n"
            "3. After the user answers: give direct feedback — what they got right, "
            "what is missing or wrong, and why it matters.\n"
            "4. Call note_add(category=<your domain>, summary='Grill: weak on <topic>') "
            "for any significant knowledge gap or repeated mistake, so future sessions "
            "can target those weak spots.\n"
            "5. Then ask the next question. Continue until the user signals they want to stop.\n"
            "6. Be tough but constructive — this is deliberate practice, not a lecture.\n"
            "Start now with your first question.]\n\n"
            + user_message
        )
    else:
        # type == "note": note already stored by dispatch; note_add excluded below.
        # type == "chat": plain conversation, no special wrapping needed.
        content_for_agent = user_message

    # Merge: start from agent's own history, append the new user message.
    messages_for_agent = [*agent_history, {"role": "user", "content": content_for_agent}]

    # For note-type messages the note is already stored by dispatch — exclude
    # note_add so the agent physically cannot create a duplicate.
    excluded = frozenset({"note_add"}) if msg_type == "note" else frozenset()

    result = chat(messages_for_agent, user_id=user_id,
                  system_prompt=persona_prompt, agent=agent,
                  exclude_tools=excluded, on_chunk=on_chunk)

    # Determine what type to record for this turn.
    # Grill type propagates forward through the session: even though the router
    # classifies follow-up answers as "chat", we keep storing "grill" so the
    # next call's sticky fallback still skips the word-count limit.
    # The session naturally exits when the user switches agent or the 1-hour
    # sticky window expires — no explicit "stop grill" bookkeeping needed.
    store_type = msg_type
    if msg_type == "chat" and last_msg_type == "grill" and agent == last_agent:
        store_type = "grill"

    # Persist this turn in agent conversation memory
    _router.store_turn(agent, "user", user_message, store_type)
    _router.store_turn(agent, "assistant", result.text, store_type)

    # If summarisation fired this turn, notify the user.
    # When on_chunk is active all content goes through that channel — we send
    # the note via on_chunk so it reaches Telegram immediately. When on_chunk
    # is absent (fallback path) we append to result.text instead.
    if did_summarise:
        summary_data = _router.get_agent_summary(agent)
        count = summary_data["turns_count"] if summary_data else "?"
        note = f"_📝 Summarised {count} turns into memory._"
        if on_chunk:
            on_chunk([], note)
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
    """Read data/prompts/<name>.md and return its text, or '' if it doesn't exist."""
    p = PROMPTS_DIR / f"{name}.md"
    return p.read_text() if p.exists() else ""
