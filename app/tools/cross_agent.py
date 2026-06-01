"""Cross-agent communication tool.

Lets one specialist agent push context directly into another agent's note stream.
The receiving agent sees the handoff note in its "Recent <agent> notes" section
on its very next activation — no polling, no extra query, zero new infrastructure.

How it works:
  agent_handoff writes a note tagged with `category = to_agent`.
  _build_agent_context() in router.py already queries
      SELECT ... FROM notes WHERE category = <agent> ORDER BY ts DESC LIMIT 10
  so the note surfaces automatically the next time that agent is called.

Example scenario that motivated this:
  Growth agent assigns a storytelling exercise.
  User goes to workout agent and writes a practice narrative.
  Without handoff: workout agent treats it as a real injury report.
  With handoff:    workout agent sees "[Handoff from growth] User is practising
                   storytelling — treat creative narratives as writing exercises,
                   not real events." and responds correctly.
"""
from __future__ import annotations

import time
from typing import Any

from ..agents.discovery import get_agents, get_routing_description
from ..db import _connect, _utc_now
from .registry import tool

# Build agent list and descriptions once at module load.
_AGENTS = list(get_agents())


def _to_agent_description() -> str:
    """Build a to_agent parameter description that names every agent + its domain.

    Evaluated once at startup so the LLM always sees an accurate, up-to-date
    roster.  Example output:

        Which agent to notify. Agents and their domains:
          • workout  — Physical training, exercise, sport, fitness, body, recovery.
          • growth   — Personal development, habits, mindset, learning.
          • dutch    — Dutch language learning, vocabulary, grammar, practice.
          • career   — Work, job, career, professional development, networking.
          • lifestyle — Daily life, routines, health, relationships, balance.
    """
    lines = ["Which agent to notify. Agents and their domains:"]
    for agent in _AGENTS:
        desc = get_routing_description(agent)
        lines.append(f"  • {agent} — {desc}")
    return "\n".join(lines)


_TO_AGENT_DESC = _to_agent_description()


@tool(
    description=(
        "Send a context note directly into another agent's memory. "
        "Use this when you assign homework, make plans, or take an action that "
        "another specialist should know about on the user's next interaction with them.\n"
        "Examples:\n"
        "• You (growth) assign a storytelling exercise → handoff to 'workout' so it "
        "knows the user is practising a narrative, not reporting a real event.\n"
        "• You (dutch) set a vocabulary goal → handoff to 'growth' for accountability.\n"
        "• You (career) schedule a networking task → handoff to 'lifestyle' for planning.\n"
        "The target agent will see this note the next time it is activated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                # Injected server-side — hidden from the LLM schema.
                # The LLM never needs to know its own name; injecting it prevents
                # hallucination and keeps the from_agent label accurate.
            },
            "to_agent": {
                "type": "string",
                "enum": _AGENTS,
                "description": _TO_AGENT_DESC,
            },
            "message": {
                "type": "string",
                "description": (
                    "What to tell the other agent. Be specific — include context "
                    "they need: what was assigned, what period, what to watch for, "
                    "or how to interpret the user's next messages in that domain."
                ),
            },
        },
        "required": ["agent", "to_agent", "message"],
    },
    agent_scoped=True,
)
def agent_handoff(agent: str, to_agent: str, message: str) -> dict[str, Any]:
    """Write a cross-agent context note into the target agent's note stream.

    The note appears under `category = to_agent` so _build_agent_context()
    surfaces it automatically on the next call to that agent — no schema
    changes, no extra queries.
    """
    if to_agent == agent:
        return {"error": "to_agent must be different from the current agent."}

    summary = f"[Handoff from {agent}] {message}"
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes(ts, created_at, raw_text, category, summary) "
            "VALUES (?,?,?,?,?)",
            (int(time.time()), _utc_now(), summary, to_agent, summary),
        )
    return {"ok": True, "to_agent": to_agent, "id": cur.lastrowid}
