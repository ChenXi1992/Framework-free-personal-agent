"""Agent-as-tool: synchronous agent querying.

Allows one specialist agent to query another for synthesized domain knowledge
and use the response as input for its own reasoning in the same turn.

Design decisions:
- Sub-agent runs with full persona + read-only tools (no writes, no recursion).
- Sub-agent conversation is ephemeral — not written to the messages table.
- user_id is fetched from DB at call time (single-user system).
- Recursion is prevented by excluding query_agent from the sub-agent's tool set.
- All imports of dispatch/router/llm are lazy (inside the function) to avoid
  circular import at module load time:
    tools/__init__ → agent_query → dispatch → llm → tools.registry
  registry is always loaded before _try() runs (line 13 of __init__.py), so
  the chain is safe, but lazy imports make the dependency explicit.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..agents.discovery import get_agents, get_routing_description
from .registry import tool, REGISTRY

log = logging.getLogger(__name__)

# Evaluated once at module load — agent list doesn't change at runtime.
_AGENTS = list(get_agents())

# Non-destructive tools that still write to the DB.
# Excluded from sub-agent calls in addition to all destructive=True tools.
_SUB_AGENT_WRITE_EXCLUDED = frozenset({
    "query_agent",      # recursion guard — sub-agent cannot call other agents
    "agent_handoff",    # writes to notes table
    "feedback_add",     # writes to feedback table
    "reminder_cancel",  # mutates reminders table
})


def _sub_agent_exclusions() -> frozenset[str]:
    """Build the exclusion set for sub-agent calls.

    Called at call time (not module load) so the registry is fully populated.
    Combines all destructive tools with the fixed write-non-destructive set.
    """
    destructive = frozenset(name for name, t in REGISTRY.items() if t.destructive)
    return destructive | _SUB_AGENT_WRITE_EXCLUDED


def _get_user_id() -> int:
    """Fetch the current user_id from the most recent user message.

    This is a single-user system — there is always exactly one user_id.
    Falls back to 1 if the messages table is empty (first-run / test).
    """
    with db._connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM messages WHERE role = 'user' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else 1


def _build_to_agent_description() -> str:
    """Build the to_agent parameter description with each agent's domain."""
    lines = ["Which agent to query. Agents and their domains:"]
    for agent in _AGENTS:
        desc = get_routing_description(agent)
        lines.append(f"  • {agent} — {desc}")
    return "\n".join(lines)


_TO_AGENT_DESC = _build_to_agent_description()


@tool(
    description=(
        "Query another specialist agent and get its synthesized response as input "
        "for your own reasoning. The target agent runs with its full context, memory, "
        "and read tools — it reasons over its domain data and returns a focused answer.\n\n"
        "Use this when you need another domain's synthesized judgment, not just raw notes:\n"
        "• Before building a multi-day plan that depends on cross-domain data\n"
        "• When a pattern you are analysing has a root in another domain\n"
        "• When notes_recent gives you data but you need interpretation\n\n"
        "Do NOT use for simple or short exchanges. Only call when the other agent's "
        "response would materially change your own output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                # Injected server-side — hidden from the LLM schema.
            },
            "to_agent": {
                "type": "string",
                "enum": _AGENTS,
                "description": _TO_AGENT_DESC,
            },
            "question": {
                "type": "string",
                "description": (
                    "What to ask the other agent. Be specific — name the decision "
                    "you are making and what information would help. The target agent "
                    "will reason over its full context to answer."
                ),
            },
        },
        "required": ["agent", "to_agent", "question"],
    },
    agent_scoped=True,
)
def query_agent(agent: str, to_agent: str, question: str) -> dict[str, Any]:
    """Run a sub-agent call synchronously and return its synthesized response.

    The sub-agent receives its full persona and read-only tools. Its conversation
    is ephemeral — it is not stored in the messages table.
    """
    if to_agent == agent:
        return {"error": "to_agent must be different from the calling agent."}

    if to_agent not in _AGENTS:
        return {"error": f"Unknown agent: {to_agent!r}. Valid agents: {_AGENTS}"}

    # Lazy imports — see module docstring for why.
    from ..agents.dispatch import _build_persona, _excluded_tools_for
    from ..agents.router import agent_message_history
    from ..llm import chat

    user_id = _get_user_id()

    # Full sub-agent system prompt: persona file + injected context block
    # (notes, profile, goals, summary — same as a normal specialist call).
    persona = _build_persona(to_agent)

    # Sub-agent's own recent history — last 10 turns is enough context without
    # ballooning token usage. The sub-agent doesn't need the full 20-turn window.
    history = agent_message_history(to_agent, limit=10)

    # Frame the question so the sub-agent knows it is answering another agent,
    # not the user. This shifts the response from conversational to synthesized.
    framed_question = (
        f"[You are being queried by the {agent} agent, not the user directly. "
        f"Give a focused, factual answer that the {agent} agent can use as input "
        f"for its own reasoning. Do not address the user. Do not ask follow-up "
        f"questions. If you lack relevant data, say so plainly.]\n\n"
        f"{question}"
    )

    messages = [*history, {"role": "user", "content": framed_question}]

    # Combine base read-only exclusions with this agent's domain exclusions
    # (e.g. lifestyle doesn't get Gmail tools even as a sub-agent).
    exclude = _sub_agent_exclusions() | _excluded_tools_for(to_agent)

    # Run the sub-agent loop. No on_chunk — sub-agent response is ephemeral
    # and returned as a tool result to the calling agent, not streamed to the user.
    result = chat(
        history=messages,
        user_id=user_id,
        system_prompt=persona,
        agent=to_agent,
        exclude_tools=exclude,
        on_chunk=None,
    )

    log.info(
        "query_agent: %s → %s completed (%d chars)",
        agent, to_agent, len(result.text),
    )

    return {"agent": to_agent, "response": result.text, "ok": True}
