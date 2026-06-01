"""Agent router: classification and context assembly.

Pass 1 — Router LLM call (route()):
    Single LLM call classifies the user message into {type, agent, category, summary}.
    Types: note | diary | chat.
    For notes, summary is a router-generated one-liner stored directly in the notes table.

Pass 2 — Context assembly (_build_agent_context()):
    Injects today's date, recent notes, personal profile, and agent-specific goals
    into the agent system prompt before the main tool-calling loop runs.
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from openai import OpenAI, OpenAIError

from ..config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    DEBUG_LOG_ROUTER, CONVERSATION_SUMMARY_THRESHOLD,
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

SKILLS_DIR = PROMPTS_DIR / "skills"

_SKILL_FILES = {"grill", "notion"}  # files that live in prompts/skills/, not prompts/


def _load_prompt(name: str) -> str:
    """Read a prompt file and return its text, or '' if missing.

    Agent prompts live in data/prompts/<name>.md.
    Skill files (grill, notion, …) live in data/prompts/skills/<name>.md.
    The router prompt has its {{AGENTS}} placeholder substituted at load time.
    """
    if name in _SKILL_FILES:
        p = SKILLS_DIR / f"{name}.md"
    else:
        p = PROMPTS_DIR / f"{name}.md"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if not text:
        log.warning("Prompt file missing or empty: %s", p)
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
    system: str, user: str, retries: int = _ROUTER_RETRIES,
    prior_turns: list[dict] | None = None,
) -> tuple[dict, str, dict | None, int]:
    """Call the LLM expecting JSON output; retries on empty or unparseable responses.

    Uses deterministic settings (_ROUTER_TEMPERATURE=0.0) so routing
    decisions are consistent across identical inputs.

    `prior_turns` is an optional list of {"role": ..., "content": ...} dicts
    (recent conversation history) inserted between the system prompt and the
    classification request. This gives the router enough context to resolve
    corrections ("that's wrong"), follow-ups, and agent-switch signals that
    are ambiguous without knowing what was just discussed.

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
    messages = [{"role": "system", "content": system}]
    if prior_turns:
        messages.extend(prior_turns)
    for attempt in range(retries + 1):
        resp = _client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages + [{"role": "user", "content": user}],
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


def route(
    user_message: str,
    recent_context: str = "",
    history: list[dict] | None = None,
) -> dict[str, str]:
    """Classify a user message. Returns {type, agent, category, summary}.

    `history` is an optional list of recent turns (last N messages as
    {"role": "user"/"assistant", "content": "..."} dicts). When provided,
    they are passed to the LLM before the classification request so the
    router can resolve corrections, follow-ups, and mid-conversation topic
    switches that carry no agent signal on their own.

    `recent_context` (legacy string hint) is still supported; it is appended
    to the user message when no history is provided.
    """
    # Build the classification prompt — append the legacy hint when no
    # structured history is available (backward compat).
    prompt_text = user_message
    if recent_context and not history:
        prompt_text = f"{user_message}\n\n[Context: {recent_context}]"

    router_system = _load_prompt("router")
    raw = ""
    usage: dict | None = None
    attempts = 0
    try:
        result, raw, usage, attempts = _llm_json(
            router_system, prompt_text, prior_turns=history or None
        )
    except (OpenAIError, json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("Router failed (%s: %s), defaulting to general/none", type(e).__name__, e)
        result = {"type": "chat", "agent": "none", "category": "none"}

    # Always: log the routing decision (summary) to the audit log.
    audit.log_event(
        "route_decision",
        input_preview=prompt_text[:200],
        result=result,
        had_context_hint=bool(recent_context),
        history_turns=len(history) if history else 0,
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
            "## Never fabricate — search first — CRITICAL\n"
            "When Xi refers to something earlier that you cannot see in your current "
            "context (a past statement, a number, a 'version' he wrote, a previous plan), "
            "you MUST call notes_search / notes_recent / conversation_recent to find the "
            "real content BEFORE responding. "
            "Never invent Xi's past words, quotes, numbers, logs, or examples. Do not "
            "reconstruct them from memory or 'fill in' what he probably said. "
            "If after searching you still cannot find it, say so plainly ('I can't find "
            "that in your history — can you paste it?') — never make it up.\n\n"
            "## Don't act on practice/hypothetical as if real\n"
            "If a message looks like it could be a practice sample, a hypothetical, a quote, "
            "or part of an ongoing exercise (e.g. a storytelling draft, a 'what if') rather "
            "than a real log or request, ask ONE clarifying question before treating it as "
            "real. Don't convert a narrative or practice message into domain-action mode "
            "(e.g. reading an injury into a storytelling sample).\n\n"
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
            "Skip files that aren't needed — only load what helps you answer.\n\n"
            "## Cross-agent handoffs\n"
            "If you assign homework, make a plan, or take any action that another "
            "specialist agent should know about, call agent_handoff(to_agent=..., "
            "message=...) BEFORE replying. "
            "Example: you (growth) assign a storytelling exercise → "
            "agent_handoff(to_agent='workout', message='User is practising storytelling "
            "— treat narrative writing messages as creative exercises, not real events.'). "
            "The other agent will see your note on their very next activation.\n\n"
            + _load_prompt("grill")
        ),
    ]

    # Recent notes for this agent's domain.
    # The router can return a fine-grained category (e.g. "running", "sleep")
    # that is not an agent name.  Notes are only stored under agent names
    # (note_add constrains to _AGENTS; agent_handoff writes to the target
    # agent name).  So we always need to query by the agent name — use the
    # fine-grained category only when it happens to be a valid agent name
    # itself, and fall back to the agent name otherwise.
    from .discovery import get_agents
    _agents = get_agents()
    note_category = category if category in _agents else agent
    if note_category in _agents:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT ts, summary, raw_text FROM notes "
                "WHERE category = ? ORDER BY ts DESC LIMIT 20",
                (note_category,),
            ).fetchall()
        if rows:
            note_lines = "\n".join(
                f"- [{datetime.datetime.fromtimestamp(r[0]).strftime('%a %b %d')}] {r[1]}"
                for r in rows
            )
            parts.append(f"## Recent {note_category} notes\n{note_lines}")

    # For the workout agent: add a pre-extracted weight table so the LLM doesn't
    # need to parse weights out of session notes. Weight is owned by the workout
    # domain (router classifies weight → workout), so the log reads category='workout'.
    if agent == "workout":
        with db._connect() as conn:
            w_rows = conn.execute(
                "SELECT ts, raw_text, summary FROM notes "
                "WHERE category = 'workout' "
                "AND (raw_text LIKE '%kg%' OR summary LIKE '%kg%') "
                "ORDER BY ts ASC",
            ).fetchall()
        if w_rows:
            w_lines = "\n".join(
                f"- [{datetime.datetime.fromtimestamp(r[0]).strftime('%b %d')}] {r[2]}"
                for r in w_rows
            )
            parts.append(f"## Weight log (all entries, chronological)\n{w_lines}")

    # Auto-inject Notion workspace knowledge (notion.md) for agents that handle
    # Notion tasks. Career always uses it; general handles cross-domain Notion
    # requests and also needs workspace IDs, page structure, and write rules.
    # Injected here so the agent never starts blind on Notion operations —
    # the alternative (context_load on-demand) requires the agent to know the
    # file exists, which it can't guarantee without prior context.
    if agent in ("career", "none"):
        notion_path = SKILLS_DIR / "notion.md"
        if notion_path.exists():
            parts.append(
                f"## Notion workspace\n"
                f"{notion_path.read_text(encoding='utf-8').strip()}"
            )

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

    # Inject any stored conversation summary (older turns compressed into a paragraph).
    # Recent raw turns are passed as OpenAI messages by dispatch.py; the summary
    # covers everything older than the rolling window so nothing is silently lost.
    summary_data = get_agent_summary(agent)
    if summary_data and summary_data["summary"]:
        ts_from = datetime.datetime.utcfromtimestamp(summary_data["ts_from"]).strftime("%b %d")
        ts_to   = datetime.datetime.utcfromtimestamp(summary_data["ts_to"]).strftime("%b %d")
        parts.append(
            f"## Conversation history\n"
            f"Summary of earlier conversations ({summary_data['turns_count']} turns, {ts_from}–{ts_to}):\n"
            f"{summary_data['summary']}"
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Agent history (OpenAI message format)
# ---------------------------------------------------------------------------

# /reset writes a system sentinel "--- conversation reset ---". All agent history
# and summarisation reads only consider turns AFTER the most recent reset, so a
# reset gives every specialist a clean slate too (not just the general agent).
# Single-user system, so the cutoff is global.
_RESET_CUTOFF_SQL = (
    "(SELECT COALESCE(MAX(ts), 0) FROM messages "
    "WHERE role = 'system' AND content LIKE '%conversation reset%')"
)


def last_user_message(agent: str) -> str:
    """Return the most recent user message stored for this agent, or ''."""
    with db._connect() as conn:
        row = conn.execute(
            "SELECT content FROM messages "
            "WHERE agent = ? AND role = 'user' ORDER BY ts DESC LIMIT 1",
            (agent,),
        ).fetchone()
    return row[0] if row else ""


def agent_message_history(agent: str, limit: int = 20) -> list[dict[str, str]]:
    """Return the most recent `limit` agent turns as OpenAI-format messages.

    Reads from messages WHERE agent=?, only turns after the last /reset.
    GROUP BY (role, content) collapses accidental duplicates; MAX(ts) orders.

    Any non-user/assistant rows (e.g. [CONFIRMED] system notes tagged to this
    agent) are mapped to the 'assistant' role so they don't appear as a second
    mid-conversation 'system' message — which some models mishandle.
    """
    with db._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT role, content
            FROM messages
            WHERE agent = ? AND ts > {_RESET_CUTOFF_SQL}
            GROUP BY role, content
            ORDER BY MAX(ts) DESC LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    return [
        {"role": (r[0] if r[0] in ("user", "assistant") else "assistant"),
         "content": r[1]}
        for r in reversed(rows)
    ]


# ---------------------------------------------------------------------------
# Conversation summarisation
# ---------------------------------------------------------------------------

def get_agent_summary(agent: str) -> dict | None:
    """Return the stored conversation summary for this agent, or None if none exists."""
    with db._connect() as conn:
        row = conn.execute(
            "SELECT summary, ts_from, ts_to, turns_count FROM conversation_summaries WHERE agent = ?",
            (agent,),
        ).fetchone()
    if not row:
        return None
    return {"summary": row[0], "ts_from": row[1], "ts_to": row[2], "turns_count": row[3]}


def summarise_if_needed(agent: str) -> bool:
    """Summarise agent history at every 20-turn boundary above the threshold.

    Returns True if a new summary was generated this call, False otherwise.
    This is a no-op (single COUNT query) when below the threshold or not
    at a 20-turn boundary.

    Trigger: total % 20 == 0 AND total >= CONVERSATION_SUMMARY_THRESHOLD.
    Fires at turns 40, 60, 80, 100…

    What the summary covers: everything from turn 1 to current total, so it
    always reflects the full conversation history at the time of each trigger.

    Implementation: rolling — feeds (previous summary + latest 20 turns) to
    the LLM rather than all raw turns. This keeps the input small regardless
    of how long the conversation grows.
    """
    _RECENT_TURNS = 20

    with db._connect() as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT role, content FROM messages
                WHERE agent = ? AND ts > {_RESET_CUTOFF_SQL}
                GROUP BY role, content
            )
            """,
            (agent,),
        ).fetchone()[0]

    existing = get_agent_summary(agent)

    # Determine whether to fire:
    # - No existing summary + total >= threshold → fire immediately (first-ever or
    #   after a summary was cleared). Fetches all turns so nothing is lost.
    # - Existing summary + at a 20-turn boundary → rolling update.
    # - Anything else → no-op.
    if not existing and total >= CONVERSATION_SUMMARY_THRESHOLD:
        pass  # fall through to first-trigger path
    elif existing and total >= CONVERSATION_SUMMARY_THRESHOLD and total % _RECENT_TURNS == 0:
        pass  # fall through to rolling path
    else:
        return False

    if existing:
        # Subsequent triggers: rolling — previous summary + latest 20 delta turns.
        # Keeps LLM input small regardless of total history length.
        with db._connect() as conn:
            delta_rows = conn.execute(
                f"""
                SELECT MAX(ts) as ts, role, content
                FROM messages
                WHERE agent = ? AND ts > {_RESET_CUTOFF_SQL}
                GROUP BY role, content
                ORDER BY MAX(ts) DESC LIMIT ?
                """,
                (agent, _RECENT_TURNS),
            ).fetchall()
        delta_rows = list(reversed(delta_rows))
        if not delta_rows:
            return False
        turns_text = (
            f"Previous summary:\n{existing['summary']}\n\nNew turns:\n"
            + "\n".join(f"{r[1].upper()}: {r[2]}" for r in delta_rows)
        )
        ts_from = existing["ts_from"]
        ts_to   = delta_rows[-1][0]
    else:
        # First trigger: no prior summary — fetch ALL turns so nothing is lost.
        with db._connect() as conn:
            all_rows = conn.execute(
                f"""
                SELECT MAX(ts) as ts, role, content
                FROM messages
                WHERE agent = ? AND ts > {_RESET_CUTOFF_SQL}
                GROUP BY role, content
                ORDER BY MAX(ts) ASC
                """,
                (agent,),
            ).fetchall()
        if not all_rows:
            return False
        turns_text = "\n".join(f"{r[1].upper()}: {r[2]}" for r in all_rows)
        ts_from = all_rows[0][0]
        ts_to   = all_rows[-1][0]

    summary_text = _llm_summarise(agent, turns_text, total)

    if not summary_text:
        # Store a placeholder so existing is non-None on the next call — prevents
        # an infinite retry loop where every message re-triggers the LLM call.
        # The rolling path will overwrite this placeholder at the next 20-turn boundary.
        log.warning("Empty summary from LLM for %s agent — storing placeholder", agent)
        summary_text = "(Summary temporarily unavailable — will update at next boundary.)"

    with db._connect() as conn:
        conn.execute(
            """INSERT INTO conversation_summaries(agent, ts_from, ts_to, turns_count, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent) DO UPDATE SET
                   ts_from=excluded.ts_from,
                   ts_to=excluded.ts_to,
                   turns_count=excluded.turns_count,
                   summary=excluded.summary,
                   created_at=excluded.created_at""",
            (agent, ts_from, ts_to, total, summary_text, _utc_now()),
        )

    log.info("Updated conversation summary for %s agent (total %d turns)", agent, total)
    return True


def _llm_summarise(agent: str, turns_text: str, count: int) -> str:
    """Call the LLM to summarise a block of conversation turns into one paragraph.

    Uses temperature=0.0 for deterministic output and a small max_tokens cap —
    the summary should be concise (3–6 sentences), not a verbatim transcript.
    """
    system = (
        f"You are maintaining a running summary of a conversation with the {agent} agent "
        "for a personal AI assistant. You will receive either raw conversation turns, or a "
        "previous summary followed by new turns. Produce an updated single paragraph that "
        "captures the full history — incorporating the previous summary and merging in new "
        "facts, decisions, goals, numbers, and commitments. "
        "Be specific — keep dates, numbers, and names. "
        "Write in second person ('You have been…', 'You agreed…'). Plain text only, no bullets."
    )
    user = f"Update the summary to cover all {count} turns:\n\n{turns_text}"
    try:
        resp = _client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=0.0,
            max_tokens=2000,  # reasoning model consumes tokens before content — needs headroom
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("Conversation summarisation failed: %s", e)
        return f"(Summary unavailable: {e})"
