"""LLM-callable tools for notes, agent conversations, and feedback."""
from __future__ import annotations

import time
from typing import Any

from ..agents.discovery import get_agents
from ..db import _connect, _utc_now
from .registry import tool

# Build dynamic enum lists once at module load (per-process, matches discovered agents).
_AGENTS        = list(get_agents())
_CATEGORIES    = _AGENTS + ["uncategorized"]
_CATEGORIES_EX = _AGENTS + ["uncategorized", "all"]   # includes 'all' for filter tools
_AGENTS_EX     = _AGENTS + ["all"]                    # includes 'all' for feedback_recent


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Store a note with a category and summary. Returns the new note id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text":     {"type": "string", "description": "The original raw text of the note."},
            "category": {"type": "string", "enum": _CATEGORIES,
                         "description": "Agent domain this note belongs to, or 'uncategorized'."},
            "summary":  {"type": "string", "description": "One-sentence summary, max 20 words."},
        },
        "required": ["text", "category", "summary"],
    },
)
def note_add(text: str, category: str, summary: str) -> dict[str, Any]:
    """Insert a classified note into the database and return its new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes(ts, created_at, raw_text, category, summary) VALUES (?,?,?,?,?)",
            (int(time.time()), _utc_now(), text, category, summary),
        )
    return {"ok": True, "id": cur.lastrowid, "category": category}


@tool(
    description=(
        "Retrieve recent notes, optionally filtered by category. "
        "Returns notes in reverse-chronological order."
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
    """Return the most recent notes, optionally filtered to a single category."""
    limit = min(max(1, limit), 50)
    with _connect() as conn:
        if category == "all":
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE category = ? ORDER BY ts DESC LIMIT ?",
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
        "Full-text search across notes. Searches raw text and summaries. "
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
    """Keyword-search raw note text and summaries using SQLite LIKE."""
    limit = min(max(1, limit), 50)
    like = f"%{query}%"
    with _connect() as conn:
        if category == "all":
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE raw_text LIKE ? OR summary LIKE ? "
                "ORDER BY ts DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, category, summary, raw_text FROM notes "
                "WHERE category = ? AND (raw_text LIKE ? OR summary LIKE ?) "
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
        "Log one turn of an agent conversation for memory. "
        "Call after every user message and agent reply."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent":   {"type": "string", "enum": _AGENTS,
                        "description": "The agent this conversation belongs to."},
            "role":    {"type": "string", "enum": ["user", "assistant"]},
            "content": {"type": "string"},
        },
        "required": ["agent", "role", "content"],
    },
)
def conversation_add(agent: str, role: str, content: str) -> dict[str, Any]:
    """Persist one conversation turn for a specific agent."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_conversations(ts, created_at, agent, role, content) VALUES (?,?,?,?,?)",
            (int(time.time()), _utc_now(), agent, role, content),
        )
    return {"ok": True, "id": cur.lastrowid}


@tool(
    description=(
        "Retrieve recent conversation history for a specific agent. "
        "Returns turns in chronological order (oldest first)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": _AGENTS,
                      "description": "The agent whose history to retrieve."},
            "limit": {"type": "integer", "description": "Max turns to return (default 20)."},
        },
        "required": ["agent"],
    },
)
def conversation_recent(agent: str, limit: int = 20) -> dict[str, Any]:
    """Return the most recent conversation turns for an agent, oldest-first."""
    limit = min(max(1, limit), 100)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, role, content FROM agent_conversations "
            "WHERE agent = ? ORDER BY ts DESC LIMIT ?",
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
