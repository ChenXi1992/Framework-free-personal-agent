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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import TIMEZONE
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


@tool(
    description=(
        "Schedule a one-time Telegram push notification at a specific date and time. "
        "STAGED — requires confirmation. "
        "Use when Xi asks to be reminded about something once at a specific time. "
        "For recurring reminders ('every Tuesday'), use reminder_set instead. "
        "When Xi asks for an alert/reminder: also call calendar_create_event to add "
        "it to Google Calendar in the same turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent":   {"type": "string"},   # injected — hidden from LLM
            "user_id": {"type": "integer"},  # injected — hidden from LLM
            "label": {
                "type": "string",
                "description": "What to remind Xi about, e.g. 'Get grocery' or 'Call mum'.",
            },
            "fire_at": {
                "type": "string",
                "description": (
                    "When to send the reminder, as a local datetime string: "
                    "'YYYY-MM-DD HH:MM'. Must be in the future. "
                    "Use today's date from context to resolve relative terms like "
                    "'tomorrow' or 'Tuesday'."
                ),
            },
        },
        "required": ["agent", "user_id", "label", "fire_at"],
    },
    destructive=True,
    agent_scoped=True,
)
def reminder_once(
    agent: str,
    user_id: int,
    label: str,
    fire_at: str,
) -> dict[str, Any]:
    """Stage a one-time reminder — waits for /confirm before saving."""
    tz = ZoneInfo(TIMEZONE)
    fire_at_clean = fire_at.strip().replace(" ", "T")
    # Accept YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS
    fmt = "%Y-%m-%dT%H:%M:%S" if len(fire_at_clean) >= 19 else "%Y-%m-%dT%H:%M"
    try:
        dt = datetime.strptime(fire_at_clean[:19] if len(fire_at_clean) >= 19 else fire_at_clean, fmt)
        dt = dt.replace(tzinfo=tz)
    except ValueError:
        return {"error": f"Cannot parse fire_at={fire_at!r}. Use 'YYYY-MM-DD HH:MM'."}

    fire_ts = int(dt.timestamp())
    if fire_ts <= int(time.time()):
        return {"error": f"fire_at={fire_at!r} is in the past. Provide a future date/time."}

    display = dt.strftime("%A, %b %d at %H:%M")
    preview = (
        f"Set one-time reminder: \"{label}\"\n"
        f"  Fires: {display}\n"
        f"  Agent: {agent}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="reminder_once",
        arguments={"agent": agent, "label": label, "fire_at": fire_ts},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Called by /confirm handler to actually insert the reminder into the DB."""
    if tool_name == "reminder_set":
        agent     = arguments["agent"]
        label     = arguments["label"]
        days      = arguments["days"]
        fire_time = arguments["fire_time"]

        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO reminders(ts, created_at, agent, label, days, fire_time, active, once) "
                "VALUES (?,?,?,?,?,?,1,0)",
                (int(time.time()), _utc_now(), agent, label, days, fire_time),
            )
        return {
            "ok":        True,
            "id":        cur.lastrowid,
            "label":     label,
            "days":      days.split(","),
            "fire_time": fire_time,
            "agent":     agent,
        }

    if tool_name == "reminder_once":
        agent   = arguments["agent"]
        label   = arguments["label"]
        fire_at = arguments["fire_at"]  # Unix timestamp (int)
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO reminders(ts, created_at, agent, label, days, fire_time, active, once, fire_at) "
                "VALUES (?,?,?,?,?,?,1,1,?)",
                (int(time.time()), _utc_now(), agent, label, "", "", fire_at),
            )
        return {"ok": True, "id": cur.lastrowid, "label": label, "fire_at": fire_at, "agent": agent}

    return {"error": f"Unknown tool for reminders executor: {tool_name}"}


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
            "SELECT id, label, days, fire_time, last_fired, "
            "COALESCE(once, 0), fire_at "
            "FROM reminders WHERE agent = ? AND active = 1 "
            "ORDER BY COALESCE(fire_at, 0), fire_time",
            (agent,),
        ).fetchall()

    items = []
    for r in rows:
        rid, label, days, fire_time, last_fired, once, fire_at = r
        if once:
            # One-time: show the human-readable fire datetime
            tz = ZoneInfo(TIMEZONE)
            fire_display = (
                datetime.fromtimestamp(fire_at, tz=tz).strftime("%Y-%m-%d %H:%M")
                if fire_at else "unknown"
            )
            items.append({
                "id": rid,
                "label": label,
                "type": "once",
                "fires_at": fire_display,
                "last_fired": last_fired,
            })
        else:
            items.append({
                "id": rid,
                "label": label,
                "type": "recurring",
                "days": [d for d in days.split(",") if d],
                "fire_time": fire_time,
                "last_fired": last_fired,
            })
    return {"reminders": items}
