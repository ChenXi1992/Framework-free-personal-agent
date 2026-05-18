"""LLM-callable query tools for health data.

Tables populated by scripts/process_health_data.py:
    health_daily   — per-day summary (steps, HR, sleep, SpO2)
    sport_daily    — per-day breakdown by sport type
    heart_rate     — per-reading HR time series
    sport_minute   — per-minute activity log
"""
from __future__ import annotations

from typing import Any

from ..db import _connect
from .registry import tool

# Health data records one row per activity minute. A gap longer than this
# between consecutive rows means the user stopped — start a new session.
# 300 s (5 min) covers normal pauses (traffic lights, water break) without
# splitting a single workout into fragments.
_SESSION_GAP_SECONDS = 300


# ---------------------------------------------------------------------------
# Daily summaries
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Get daily health summary for the last N days. Returns steps, distance, "
        "calories, heart rate (resting/max/min), SpO2, and sleep breakdown. "
        "Use this for trend questions: 'how has my sleep been?', 'steps this week'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Number of recent days to return (default 7, max 90)."},
        },
        "required": [],
    },
)
def health_daily_summary(days: int = 7) -> dict[str, Any]:
    days = min(max(1, days), 90)
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date, steps, distance_m, calories_kcal, duration_min,
                   resting_hr, max_hr, min_hr, spo2_avg,
                   sleep_total_min, sleep_deep_min, sleep_light_min,
                   sleep_rem_min, sleep_awake_min
            FROM health_daily
            ORDER BY date DESC
            LIMIT ?
        """, (days,)).fetchall()
    return {
        "days": [
            {
                "date": r[0],
                "steps": r[1],
                "distance_m": r[2],
                "calories_kcal": r[3],
                "active_min": r[4],
                "resting_hr": r[5],
                "max_hr": r[6],
                "min_hr": r[7],
                "spo2_avg": r[8],
                "sleep_total_min": r[9],
                "sleep_deep_min": r[10],
                "sleep_light_min": r[11],
                "sleep_rem_min": r[12],
                "sleep_awake_min": r[13],
            }
            for r in rows
        ]
    }


@tool(
    description=(
        "Get sport breakdown by activity type for recent days. "
        "Shows how many minutes and km were spent on each activity "
        "(running, cycling, walking, etc.) per day."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days":       {"type": "integer", "description": "Number of recent days (default 7, max 90)."},
            "sport_name": {"type": "string",  "description": "Filter to a sport name, e.g. 'running'. Optional."},
        },
        "required": [],
    },
)
def health_sport_breakdown(days: int = 7, sport_name: str = "") -> dict[str, Any]:
    days = min(max(1, days), 90)
    with _connect() as conn:
        if sport_name:
            rows = conn.execute("""
                SELECT date, sport_name, steps, distance_m, calories_kcal, duration_min
                FROM sport_daily
                WHERE sport_name LIKE ?
                ORDER BY date DESC
                LIMIT ?
            """, (f"%{sport_name}%", days * 5)).fetchall()
        else:
            rows = conn.execute("""
                SELECT date, sport_name, steps, distance_m, calories_kcal, duration_min
                FROM sport_daily
                WHERE date IN (
                    SELECT DISTINCT date FROM sport_daily ORDER BY date DESC LIMIT ?
                )
                ORDER BY date DESC, duration_min DESC
            """, (days,)).fetchall()
    return {
        "activities": [
            {
                "date": r[0], "sport": r[1],
                "steps": r[2], "distance_m": r[3],
                "calories_kcal": r[4], "duration_min": r[5],
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Heart rate
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Get heart rate readings for a specific date (YYYYMMDD). "
        "Returns a time series of BPM values. Useful for analysing "
        "workout intensity, recovery, or stress on a given day."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYYMMDD format, e.g. '20260510'."},
            "kind": {
                "type": "string",
                "enum": ["dynamic", "resting", "all"],
                "description": "Filter by reading type (default 'all').",
            },
        },
        "required": ["date"],
    },
)
def health_heart_rate(date: str, kind: str = "all") -> dict[str, Any]:
    date = date.replace("-", "")
    with _connect() as conn:
        if kind == "all":
            rows = conn.execute(
                "SELECT ts, bpm, kind FROM heart_rate WHERE date = ? ORDER BY ts",
                (date,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, bpm, kind FROM heart_rate WHERE date = ? AND kind = ? ORDER BY ts",
                (date, kind),
            ).fetchall()
    readings = [{"ts": r[0], "bpm": r[1], "kind": r[2]} for r in rows]
    if not readings:
        return {"date": date, "readings": [], "note": "No data for this date."}
    bpms = [r["bpm"] for r in readings]
    return {
        "date": date,
        "count": len(readings),
        "avg_bpm": round(sum(bpms) / len(bpms), 1),
        "max_bpm": max(bpms),
        "min_bpm": min(bpms),
        "readings": readings,
    }


# ---------------------------------------------------------------------------
# Workout sessions (derived from sport_minute)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Get recent workout sessions derived from per-minute activity data. "
        "Groups consecutive minutes of the same sport into sessions. "
        "Good for: 'how long did I run last Tuesday?', 'show my recent workouts'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days":       {"type": "integer", "description": "Look back this many days (default 30, max 365)."},
            "sport_name": {"type": "string",  "description": "Filter by sport, e.g. 'run'. Optional."},
            "limit":      {"type": "integer", "description": "Max sessions to return (default 20)."},
        },
        "required": [],
    },
)
def health_workout_sessions(days: int = 30, sport_name: str = "", limit: int = 20) -> dict[str, Any]:
    days  = min(max(1, days), 365)
    limit = min(max(1, limit), 100)

    with _connect() as conn:
        # Find the latest date in the table so relative queries work on historical data
        (latest,) = conn.execute("SELECT MAX(date) FROM sport_minute").fetchone()
        if not latest:
            return {"sessions": [], "note": "No sport_minute data in database."}

        # Compute cutoff date by subtracting `days` from the latest date in DB
        cutoff = conn.execute(
            "SELECT date(?, ?)", (f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}", f"-{days} days")
        ).fetchone()[0].replace("-", "")

        if sport_name:
            rows = conn.execute("""
                SELECT ts, date, sport_type, sport_name, steps,
                       distance_m, calories_kcal, altitude_m, duration_min
                FROM sport_minute
                WHERE sport_name LIKE ? AND date >= ?
                ORDER BY ts
            """, (f"%{sport_name}%", cutoff)).fetchall()
        else:
            rows = conn.execute("""
                SELECT ts, date, sport_type, sport_name, steps,
                       distance_m, calories_kcal, altitude_m, duration_min
                FROM sport_minute
                WHERE date >= ?
                ORDER BY ts
            """, (cutoff,)).fetchall()

    if not rows:
        return {"sessions": [], "note": "No activity data in this range."}

    # Group consecutive sport_minute rows into logical sessions.
    # A new session starts when either:
    #   (a) the sport type changes — e.g. switching from running to walking, or
    #   (b) the gap between consecutive rows exceeds 300 seconds (5 minutes).
    # 300s was chosen because health data records one row per activity minute;
    # a gap longer than 5 minutes almost always means a rest/stop, not just
    # a missed reading. Shorter pauses (e.g. waiting at traffic lights) stay
    # within the same session.
    sessions = []
    cur: dict | None = None

    for r in rows:
        ts, date, stype, sname, steps, dist, cal, alt, dur = r
        if (cur is None
                or cur["sport_type"] != stype
                or ts - cur["last_ts"] > _SESSION_GAP_SECONDS):
            if cur:
                sessions.append(cur)
            cur = {
                "date": date,
                "sport_type": stype,
                "sport_name": sname,
                "start_ts": ts,
                "last_ts": ts,
                "duration_min": dur or 0,
                "distance_m": dist or 0,
                "calories_kcal": cal or 0,
                "steps": steps or 0,
            }
        else:
            cur["last_ts"]       = ts
            # `dur` is occasionally NULL in the raw health export for the first
            # minute of a session. Fall back to 1 so the total isn't under-counted.
            cur["duration_min"] += dur or 1
            cur["distance_m"]   += dist or 0
            cur["calories_kcal"] += cal or 0
            cur["steps"]         += steps or 0

    if cur:
        sessions.append(cur)

    # Sort newest first, cap
    sessions.sort(key=lambda s: s["start_ts"], reverse=True)
    sessions = sessions[:limit]

    # Clean up internal fields
    for s in sessions:
        s.pop("last_ts")
        s["distance_km"] = round(s.pop("distance_m") / 1000, 2)
        s["calories_kcal"] = round(s["calories_kcal"], 1)

    return {"sessions": sessions, "total_found": len(sessions)}
