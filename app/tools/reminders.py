"""Reminder tools — let agents schedule push notifications.

The agent calls reminder_set() during a conversation to register a recurring
reminder. At the scheduled time, the bot checks whether a meaningful session
already happened that day (LLM-judged) and pushes a smart message if not.

`agent` is injected server-side (agent_scoped=True) — the LLM never sees it
in the schema, consistent with how `user_id` is handled for destructive tools.

reminder_set is STAGED (destructive=True): it calls stage_action() and returns
a preview for the user to confirm with /confirm before the reminder is saved.
"""
from __future__ import annotations

import time
from typing import Any

from ..db import _connect, _utc_now
from .pending import stage_action
from .registry import tool

_VALID_DAYS = {
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "daily",
}


@tool(
    description=(
        "Schedule a recurring push notification reminder. STAGED — requires confirmation. "
        "Use this when the user asks to be reminded about something on specific days. "
        "The bot will check whether a meaningful session already happened that day "
        "before pushing — so it won't interrupt if you've already done it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent":   {"type": "string"},   # injected — hidden from LLM
            "user_id": {"type": "integer"},  # injected — hidden from LLM
            "label": {
                "type": "string",
                "description": "Short name for this reminder, e.g. 'Dutch practice' or 'Evening run'.",
            },
            "days": {
                "type": "string",
                "description": (
                    "Comma-separated days to fire: monday, tuesday, wednesday, thursday, "
                    "friday, saturday, sunday. Use 'daily' for every day. "
                    "Example: 'wednesday,friday' or 'daily'."
                ),
            },
            "fire_time": {
                "type": "string",
                "description": "Time to send the reminder in HH:MM format (24h), e.g. '20:00'.",
            },
        },
        "required": ["agent", "user_id", "label", "days", "fire_time"],
    },
    destructive=True,
    agent_scoped=True,
)
def reminder_set(
    agent: str,
    user_id: int,
    label: str,
    days: str,
    fire_time: str,
) -> dict[str, Any]:
    """Stage a recurring reminder — waits for /confirm before saving."""
    # Validate days
    day_list = [d.strip().lower() for d in days.split(",") if d.strip()]
    invalid = [d for d in day_list if d not in _VALID_DAYS]
    if invalid:
        return {"error": f"Invalid day(s): {invalid}. Use: {sorted(_VALID_DAYS)}"}

    # Validate fire_time HH:MM
    parts = fire_time.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return {"error": f"Invalid fire_time {fire_time!r}. Use HH:MM format, e.g. '20:00'."}
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return {"error": f"Invalid fire_time {fire_time!r}. Hours 0–23, minutes 0–59."}

    days_stored = ",".join(day_list)
    days_display = ", ".join(d.capitalize() for d in day_list)
    preview = (
        f"Set reminder: \"{label}\"\n"
        f"  Agent:     {agent}\n"
        f"  Days:      {days_display}\n"
        f"  Fire time: {fire_time.strip()}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="reminder_set",
        arguments={
            "agent":     agent,
            "label":     label,
            "days":      days_stored,
            "fire_time": fire_time.strip(),
        },
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Called by /confirm handler to actually insert the reminder into the DB."""
    if tool_name != "reminder_set":
        return {"error": f"Unknown tool for reminders executor: {tool_name}"}

    agent     = arguments["agent"]
    label     = arguments["label"]
    days      = arguments["days"]
    fire_time = arguments["fire_time"]

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders(ts, created_at, agent, label, days, fire_time, active) "
            "VALUES (?,?,?,?,?,?,1)",
            (int(time.time()), _utc_now(), agent, label, days, fire_time),
        )
    day_list = days.split(",")
    return {
        "ok":        True,
        "id":        cur.lastrowid,
        "label":     label,
        "days":      day_list,
        "fire_time": fire_time,
        "agent":     agent,
    }


@tool(
    description="Cancel an active reminder by its ID. Use reminder_list to find IDs.",
    parameters={
        "type": "object",
        "properties": {
            "reminder_id": {
                "type": "integer",
                "description": "The ID of the reminder to cancel (from reminder_list).",
            },
        },
        "required": ["reminder_id"],
    },
)
def reminder_cancel(reminder_id: int) -> dict[str, Any]:
    """Deactivate a reminder. It will no longer fire."""
    with _connect() as conn:
        rowcount = conn.execute(
            "UPDATE reminders SET active = 0 WHERE id = ?",
            (reminder_id,),
        ).rowcount
    if rowcount == 0:
        return {"error": f"No reminder found with id={reminder_id}."}
    return {"ok": True, "cancelled_id": reminder_id}


@tool(
    description=(
        "List all active reminders for this agent. "
        "Use this to check what reminders are set, or to get IDs before cancelling one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string"},  # injected — hidden from LLM
        },
        "required": ["agent"],
    },
    agent_scoped=True,
)
def reminder_list(agent: str) -> dict[str, Any]:
    """Return all active reminders for the calling agent."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, label, days, fire_time, last_fired "
            "FROM reminders WHERE agent = ? AND active = 1 "
            "ORDER BY fire_time",
            (agent,),
        ).fetchall()
    return {
        "reminders": [
            {
                "id": r[0],
                "label": r[1],
                "days": r[2].split(","),
                "fire_time": r[3],
                "last_fired": r[4],
            }
            for r in rows
        ]
    }
