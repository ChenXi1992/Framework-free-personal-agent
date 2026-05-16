"""Google Calendar tools.

Auth: shared with Gmail — the same gmail_token.json covers both services.
If you haven't authenticated yet, or your existing token predates the Calendar
scope, delete data/gmail_token.json and run:

    python -m app.tools.gmail_auth

Read tools execute immediately.
`calendar_create_event` and `calendar_delete_event` are staged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .gmail_auth import load_credentials
from .pending import stage_action
from .registry import tool

log = logging.getLogger(__name__)

_service = None


def _get_service():
    global _service
    if _service is None:
        creds = load_credentials()
        if creds is None:
            raise RuntimeError(
                "Google Calendar not authenticated. "
                "Delete data/gmail_token.json and run "
                "`python -m app.tools.gmail_auth` to re-authenticate."
            )
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_event(e: dict[str, Any]) -> dict[str, Any]:
    start = e.get("start", {})
    end   = e.get("end", {})
    return {
        "id":       e.get("id"),
        "title":    e.get("summary", "(no title)"),
        "start":    start.get("dateTime") or start.get("date"),
        "end":      end.get("dateTime")   or end.get("date"),
        "location": e.get("location", ""),
        "link":     e.get("htmlLink", ""),
        "status":   e.get("status", ""),
        "organizer": (e.get("organizer") or {}).get("email", ""),
        "attendees": [
            a.get("email") for a in e.get("attendees", [])
            if not a.get("self")
        ],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@tool(
    description=(
        "List upcoming Google Calendar events. "
        "Returns events sorted by start time. "
        "Use `calendar_id='primary'` for the main calendar. "
        "Dates must be RFC3339, e.g. '2025-06-01T00:00:00Z'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID. Use 'primary' for the main calendar.",
                "default": "primary",
            },
            "time_min": {
                "type": "string",
                "description": "Start of range (RFC3339). Defaults to now.",
            },
            "time_max": {
                "type": "string",
                "description": "End of range (RFC3339). Optional.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
            },
            "query": {
                "type": "string",
                "description": "Optional free-text search over event titles and descriptions.",
            },
        },
        "required": [],
    },
)
def calendar_list_events(
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 10,
    query: str = "",
) -> dict[str, Any]:
    try:
        svc = _get_service()
        kwargs: dict[str, Any] = {
            "calendarId":   calendar_id,
            "timeMin":      time_min or _now_iso(),
            "maxResults":   max_results,
            "singleEvents": True,
            "orderBy":      "startTime",
        }
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query
        resp = svc.events().list(**kwargs).execute()
        events = resp.get("items", [])
        return {"events": [_fmt_event(e) for e in events], "count": len(events)}
    except (HttpError, RuntimeError) as e:
        return {"error": str(e)}


@tool(
    description=(
        "Find free time slots in your Google Calendar between two times. "
        "Returns busy blocks and the free gaps between them. "
        "Useful for scheduling: 'when am I free tomorrow afternoon?'"
    ),
    parameters={
        "type": "object",
        "properties": {
            "time_min": {
                "type": "string",
                "description": "Start of window to check (RFC3339), e.g. '2025-06-01T09:00:00Z'.",
            },
            "time_max": {
                "type": "string",
                "description": "End of window to check (RFC3339), e.g. '2025-06-01T18:00:00Z'.",
            },
            "calendar_id": {
                "type": "string",
                "default": "primary",
            },
        },
        "required": ["time_min", "time_max"],
    },
)
def calendar_find_free_slots(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    try:
        svc  = _get_service()
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items":   [{"id": calendar_id}],
        }
        resp = svc.freebusy().query(body=body).execute()
        busy = resp.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        return {"busy": busy, "time_min": time_min, "time_max": time_max}
    except (HttpError, RuntimeError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Write tools (staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Create a new Google Calendar event. STAGED — call this tool immediately "
        "for every event you want to create; do NOT wait for the user to confirm first. "
        "Calling the tool stages the action (nothing is created yet); the user then "
        "confirms all staged actions at once. For multiple events, call this tool once "
        "per event in the same turn.\n\n"
        "For timed events: start/end must be RFC3339 datetime strings, "
        "e.g. '2026-06-01T14:00:00+08:00'.\n"
        "For all-day events: set all_day=true and pass start/end as 'YYYY-MM-DD' "
        "date strings (end should be the day AFTER the last day, per Google Calendar "
        "convention — e.g. a single day on 2026-05-18 uses end='2026-05-19')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title":       {"type": "string"},
            "start":       {
                "type": "string",
                "description": (
                    "RFC3339 datetime (e.g. '2026-06-01T14:00:00+08:00') for timed events, "
                    "or 'YYYY-MM-DD' date string for all-day events."
                ),
            },
            "end":         {
                "type": "string",
                "description": (
                    "RFC3339 datetime for timed events, or 'YYYY-MM-DD' for all-day events. "
                    "For a single all-day event, use the following day as end."
                ),
            },
            "all_day":     {
                "type": "boolean",
                "description": "Set to true for an all-day event. start/end must be 'YYYY-MM-DD'.",
                "default": False,
            },
            "description": {"type": "string", "default": ""},
            "location":    {"type": "string", "default": ""},
            "attendees":   {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of attendee email addresses.",
                "default": [],
            },
            "calendar_id": {"type": "string", "default": "primary"},
            "user_id":     {"type": "integer"},
        },
        "required": ["title", "start", "end", "user_id"],
    },
    destructive=True,
)
def calendar_create_event(
    title: str,
    start: str,
    end: str,
    user_id: int,
    all_day: bool = False,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Stage a calendar event creation for user confirmation.

    For all-day events, start/end must be 'YYYY-MM-DD' strings and all_day must
    be True. The executor uses `{"date": ...}` for all-day vs `{"dateTime": ...}`
    for timed events — Google Calendar treats these differently.
    """
    attendees = attendees or []
    day_label = " (all day)" if all_day else ""
    preview = (
        f"Create calendar event: \"{title}\"\n"
        f"  Start: {start}{day_label}\n"
        f"  End:   {end}{day_label}\n"
        + (f"  Location: {location}\n" if location else "")
        + (f"  Attendees: {', '.join(attendees)}\n" if attendees else "")
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="calendar_create_event",
        arguments={
            "title": title, "start": start, "end": end,
            "all_day": all_day,
            "description": description, "location": location,
            "attendees": attendees, "calendar_id": calendar_id,
        },
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


@tool(
    description=(
        "Delete a Google Calendar event by its ID. STAGED — requires confirmation. "
        "Get event IDs from calendar_list_events."
    ),
    parameters={
        "type": "object",
        "properties": {
            "event_id":    {"type": "string", "description": "Event ID from calendar_list_events."},
            "calendar_id": {"type": "string", "default": "primary"},
            "user_id":     {"type": "integer"},
        },
        "required": ["event_id", "user_id"],
    },
    destructive=True,
)
def calendar_delete_event(
    event_id: str,
    user_id: int,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    try:
        svc   = _get_service()
        event = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        title = event.get("summary", "(no title)")
        start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date", "")
    except (HttpError, RuntimeError) as e:
        return {"error": str(e)}

    preview = f"Delete calendar event: \"{title}\" on {start}"
    action_id = stage_action(
        user_id=user_id,
        tool_name="calendar_delete_event",
        arguments={"event_id": event_id, "calendar_id": calendar_id},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


# ---------------------------------------------------------------------------
# Executor for /confirm handler
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        svc = _get_service()

        if tool_name == "calendar_create_event":
            all_day = arguments.get("all_day", False)
            if all_day:
                # Google Calendar all-day events use {"date": "YYYY-MM-DD"}.
                # The end date is exclusive (day after the last day).
                start_block = {"date": arguments["start"]}
                end_block   = {"date": arguments["end"]}
            else:
                start_block = {"dateTime": arguments["start"]}
                end_block   = {"dateTime": arguments["end"]}

            body: dict[str, Any] = {
                "summary": arguments["title"],
                "start":   start_block,
                "end":     end_block,
            }
            if arguments.get("description"):
                body["description"] = arguments["description"]
            if arguments.get("location"):
                body["location"] = arguments["location"]
            if arguments.get("attendees"):
                body["attendees"] = [{"email": e} for e in arguments["attendees"]]
            event = svc.events().insert(
                calendarId=arguments.get("calendar_id", "primary"),
                body=body,
            ).execute()
            return {"ok": True, "id": event.get("id"), "link": event.get("htmlLink")}

        if tool_name == "calendar_delete_event":
            svc.events().delete(
                calendarId=arguments.get("calendar_id", "primary"),
                eventId=arguments["event_id"],
            ).execute()
            return {"ok": True}

        return {"ok": False, "error": f"unknown tool: {tool_name}"}
    except (HttpError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}
