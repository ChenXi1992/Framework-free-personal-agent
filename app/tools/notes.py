"""LLM-callable tools for notes, agent conversations, and feedback."""
from __future__ import annotations

import time
from typing import Any

from ..agents.discovery import get_agents
from ..db import _connect, _utc_now
from .registry import tool

# Build dynamic enum lists once at module load (per-process, matches discovered agents).
_AGENTS        = list(get_agents())
_CATEGORIES_EX = _AGENTS + ["uncategorized", "all"]   # includes 'all' for filter tools
_AGENTS_EX     = _AGENTS + ["all"]                    # includes 'all' for feedback_recent


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Retrieve recent notes for a domain, optionally filtered by category. "
        "Returns user-logged entries and cross-agent handoff notes in reverse-chronological order. "
        "Agent responses are excluded — use conversation_recent for full dialogue history."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": _CATEGORIES_EX,
                "description": "Filter by category, or 'all' for every category.",
            },
            "limit": {"type": "integer", "description": "Max notes to return (default 10, max 50)."},
        },
        "required": ["category"],
    },
)
def notes_recent(category: str = "all", limit: int = 10) -> dict[str, Any]:
    """Return the most recent user-logged notes, optionally filtered to a single category.

    Excludes assistant responses (role='assistant') — those are in agent_message_history.
    Includes user messages (role='user'), cross-agent handoffs (role='system'),
    and legacy rows (role IS NULL) created before the role column was added.
    """
    limit = min(max(1, limit), 50)
    with _connect() as conn:
        if category == "all":
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE role != 'assistant' OR role IS NULL "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE category = ? AND (role != 'assistant' OR role IS NULL) "
                "ORDER BY ts DESC LIMIT ?",
                (category, limit),
            ).fetchall()
    return {
        "notes": [
            {"id": r[0], "ts": r[1], "category": r[2], "summary": r[3], "text": r[4]}
            for r in rows
        ]
    }


@tool(
    description=(
        "Full-text search across notes. Searches user-logged entries and cross-agent handoffs. "
        "Agent responses are excluded from search results. "
        "Optionally filter by category."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query":    {"type": "string", "description": "Keywords to search for."},
            "category": {
                "type": "string",
                "enum": _CATEGORIES_EX,
                "description": "Filter by category, or 'all' to search everything.",
            },
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
        "required": ["query"],
    },
)
def notes_search(query: str, category: str = "all", limit: int = 10) -> dict[str, Any]:
    """Keyword-search user-logged notes using SQLite LIKE.

    Excludes assistant responses (role='assistant').
    """
    limit = min(max(1, limit), 50)
    like = f"%{query}%"
    with _connect() as conn:
        if category == "all":
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE (role != 'assistant' OR role IS NULL) "
                "AND (raw_text LIKE ? OR summary LIKE ?) "
                "ORDER BY ts DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE category = ? AND (role != 'assistant' OR role IS NULL) "
                "AND (raw_text LIKE ? OR summary LIKE ?) "
                "ORDER BY ts DESC LIMIT ?",
                (category, like, like, limit),
            ).fetchall()
    return {
        "notes": [
            {"id": r[0], "ts": r[1], "category": r[2], "summary": r[3], "text": r[4]}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Agent conversations
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Retrieve recent conversation history for this agent. "
        "Returns turns in chronological order (oldest first)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string"},  # injected server-side — hidden from LLM
            "limit": {"type": "integer", "description": "Max turns to return (default 20)."},
        },
        "required": ["agent"],
    },
    agent_scoped=True,
)
def conversation_recent(agent: str, limit: int = 20) -> dict[str, Any]:
    """Return the most recent conversation turns for an agent, oldest-first.

    Reads from messages. GROUP BY (role, content) collapses any duplicates;
    MAX(id)/MAX(ts) pick the most recent occurrence.
    """
    limit = min(max(1, limit), 100)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT MAX(id) as id, MAX(ts) as ts, role, content
            FROM messages
            WHERE agent = ?
            GROUP BY role, content
            ORDER BY MAX(ts) DESC LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    turns = [{"id": r[0], "ts": r[1], "role": r[2], "content": r[3]} for r in reversed(rows)]
    return {"agent": agent, "turns": turns}


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Record feedback about an agent interaction. Use this when the user "
        "explicitly rates the response, or when the agent detects implicit "
        "signals (very short dismissive reply, 'not helpful', etc)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent":     {"type": "string", "enum": _AGENTS,
                          "description": "The agent this feedback is about."},
            "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
            "note":      {"type": "string", "description": "What happened — quote the user or describe the signal."},
        },
        "required": ["agent", "sentiment", "note"],
    },
)
def feedback_add(agent: str, sentiment: str, note: str) -> dict[str, Any]:
    """Record a feedback signal (positive/negative/neutral) for an agent response."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO feedback(ts, created_at, agent, sentiment, note) VALUES (?,?,?,?,?)",
            (int(time.time()), _utc_now(), agent, sentiment, note),
        )
    return {"ok": True, "id": cur.lastrowid}


@tool(
    description="Retrieve recent feedback entries for an agent.",
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": _AGENTS_EX,
                      "description": "Agent name, or 'all' for every agent."},
            "limit": {"type": "integer", "description": "Max entries (default 20)."},
        },
        "required": ["agent"],
    },
)
def feedback_recent(agent: str = "all", limit: int = 20) -> dict[str, Any]:
    """Return recent feedback entries for one agent or all agents."""
    limit = min(max(1, limit), 100)
    with _connect() as conn:
        if agent == "all":
            rows = conn.execute(
                "SELECT id, ts, agent, sentiment, note FROM feedback "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, agent, sentiment, note FROM feedback "
                "WHERE agent = ? ORDER BY ts DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
    return {
        "feedback": [
            {"id": r[0], "ts": r[1], "agent": r[2], "sentiment": r[3], "note": r[4]}
            for r in rows
        ]
    }
