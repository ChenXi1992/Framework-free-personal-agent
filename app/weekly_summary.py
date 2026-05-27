"""Weekly summary generator.

Triggered on the first message after a week boundary (Monday 00:00 local time).
For each agent that has notes in a completed week, generates a structured summary
via an LLM call, stores it in the weekly_summaries table, writes it to
data/summaries/<agent>/<year>-W<week>.md, and pushes it to Telegram.

Runs in an asyncio background task so it never blocks the user's reply.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Callable, Awaitable

from zoneinfo import ZoneInfo

from openai import OpenAI, OpenAIError

from . import audit
from .config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, TIMEZONE
from .db import _connect, _utc_now

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_SUMMARIES_DIR = _DATA_DIR / "summaries"
_PROMPTS_DIR = _DATA_DIR / "prompts"


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------

def _now_local() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(TIMEZONE))


def _iso_week(dt: datetime.datetime) -> tuple[int, int]:
    """Return (iso_year, iso_week) for a datetime."""
    cal = dt.isocalendar()
    return cal.year, cal.week


def _week_date_range(year: int, week: int) -> tuple[datetime.date, datetime.date]:
    """Return (monday, sunday) for an ISO week."""
    monday = datetime.date.fromisocalendar(year, week, 1)
    sunday = datetime.date.fromisocalendar(year, week, 7)
    return monday, sunday


def _week_unix_range(year: int, week: int) -> tuple[int, int]:
    """Return (start_ts, end_ts) unix seconds covering the full ISO week in local tz."""
    tz = ZoneInfo(TIMEZONE)
    monday, sunday = _week_date_range(year, week)
    start = int(datetime.datetime(monday.year, monday.month, monday.day,
                                  tzinfo=tz).timestamp())
    end   = int(datetime.datetime(sunday.year, sunday.month, sunday.day,
                                  23, 59, 59, tzinfo=tz).timestamp())
    return start, end


# ---------------------------------------------------------------------------
# Missing-week detection
# ---------------------------------------------------------------------------

def _last_summary_week(agent: str) -> tuple[int, int] | None:
    """Return (year, week) of the most recent summary for `agent`, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT year, week FROM weekly_summaries "
            "WHERE agent = ? ORDER BY year DESC, week DESC LIMIT 1",
            (agent,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def _first_note_week(agent: str) -> tuple[int, int] | None:
    """Return (year, week) of the agent's oldest note, or None if no notes exist."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(ts) FROM notes WHERE category = ?",
            (agent,),
        ).fetchone()
    if not row or row[0] is None:
        return None
    tz = ZoneInfo(TIMEZONE)
    dt = datetime.datetime.fromtimestamp(row[0], tz=tz)
    return _iso_week(dt)


def _missing_weeks(agent: str) -> list[tuple[int, int]]:
    """Return all completed ISO weeks that need a summary for `agent`.

    Start point: the week of the agent's first ever note (not an arbitrary
    lookback). If no notes exist yet, returns [] — nothing to summarise.
    Already-summarised weeks are skipped via the weekly_summaries table.
    'Completed' means the week has fully ended — the current week is never included.
    """
    now_year, now_week = _iso_week(_now_local())
    last = _last_summary_week(agent)

    if last is None:
        # No summaries yet — start from the week of the first note.
        first = _first_note_week(agent)
        if first is None:
            return []  # no notes at all, nothing to summarise
        # Start one week before first note so the loop picks it up correctly
        start_date = datetime.date.fromisocalendar(first[0], first[1], 1) \
                     - datetime.timedelta(weeks=1)
        start_cal = start_date.isocalendar()
        last = (start_cal.year, start_cal.week)

    missing: list[tuple[int, int]] = []
    d = datetime.date.fromisocalendar(last[0], last[1], 1) + datetime.timedelta(weeks=1)
    while True:
        cal = d.isocalendar()
        if (cal.year, cal.week) >= (now_year, now_week):
            break
        missing.append((cal.year, cal.week))
        d += datetime.timedelta(weeks=1)
    return missing


# ---------------------------------------------------------------------------
# Note fetching
# ---------------------------------------------------------------------------

def _fetch_notes(agent: str, year: int, week: int) -> list[dict]:
    """Fetch all notes for `agent` that fall within the given ISO week."""
    start_ts, end_ts = _week_unix_range(year, week)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, summary, raw_text FROM notes "
            "WHERE category = ? AND ts >= ? AND ts <= ? "
            "ORDER BY ts ASC",
            (agent, start_ts, end_ts),
        ).fetchall()
    return [{"ts": r[0], "summary": r[1], "raw_text": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _load_summary_rules(agent: str) -> str:
    """Extract the '## Weekly summary' section from the agent's prompt file.

    Falls back to a minimal generic instruction if the section is missing,
    so new agents work out of the box without requiring a summary section.
    """
    p = _PROMPTS_DIR / f"{agent}.md"
    if not p.exists():
        return (
            f"Write a concise weekly summary for the {agent} domain. "
            "Use markdown. Include what was logged and any notable patterns."
        )
    text = p.read_text(encoding="utf-8")
    # Find the ## Weekly summary section and extract until the next ## heading
    marker = "## Weekly summary"
    start = text.find(marker)
    if start == -1:
        return (
            f"Write a concise weekly summary for the {agent} domain. "
            "Use markdown. Include what was logged and any notable patterns."
        )
    after = text[start + len(marker):]
    # Cut at the next top-level heading (## ...) if present
    next_heading = after.find("\n## ")
    if next_heading != -1:
        after = after[:next_heading]
    return after.strip()


def _format_notes_block(notes: list[dict], year: int, week: int) -> str:
    monday, sunday = _week_date_range(year, week)
    tz = ZoneInfo(TIMEZONE)
    lines = [f"Week {week}, {year} ({monday.strftime('%b %d')} – {sunday.strftime('%b %d, %Y')})"]
    if not notes:
        lines.append("(no notes logged this week)")
    else:
        for n in notes:
            dt = datetime.datetime.fromtimestamp(n["ts"], tz=tz)
            day = dt.strftime("%a %b %d")
            lines.append(f"- [{day}] {n['summary']}")
    return "\n".join(lines)


def _generate_summary(agent: str, year: int, week: int, notes: list[dict]) -> str:
    """Call the LLM to produce a weekly summary. Returns the summary text."""
    monday, sunday = _week_date_range(year, week)
    summary_rules = _load_summary_rules(agent)
    notes_block = _format_notes_block(notes, year, week)

    # Replace {week}, {year}, {date range} placeholders in the rules so the
    # agent doesn't have to figure out the current period itself.
    date_range = f"{monday.strftime('%B %d')} – {sunday.strftime('%B %d, %Y')}"
    summary_rules = (
        summary_rules
        .replace("{week}", str(week))
        .replace("{year}", str(year))
        .replace("{date range}", date_range)
    )

    system = (
        f"Today's date context: week {week} of {year}, "
        f"period {date_range}.\n\n"
        "Use only the notes provided below — do not invent data.\n\n"
        + summary_rules
    )

    user = f"Generate the weekly summary for the notes below:\n\n{notes_block}"

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=0.3,   # summaries should be factual, low creativity
            max_tokens=600,
        )
        return (resp.choices[0].message.content or "").strip()
    except OpenAIError as e:
        log.error("weekly_summary: LLM call failed for %s W%d/%d: %s", agent, week, year, e)
        return (
            f"## Week {week}, {year} — {agent.capitalize()}\n"
            f"**Period:** {monday.strftime('%B %d')} – {sunday.strftime('%B %d, %Y')}\n\n"
            f"_(Summary generation failed: {e})_"
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store_empty(agent: str, year: int, week: int) -> None:
    """Mark a week as processed in the DB without generating a summary or file.

    Prevents re-visiting empty weeks on every startup while keeping
    data/summaries/ clean (no files for weeks with nothing logged).
    """
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO weekly_summaries"
            "(ts, created_at, agent, year, week, summary) VALUES (?,?,?,?,?,?)",
            (int(time.time()), _utc_now(), agent, year, week, ""),
        )


def _store(agent: str, year: int, week: int, summary: str) -> None:
    """Write summary to DB and to data/summaries/<agent>/<year>-W<week>.md."""
    # DB
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO weekly_summaries"
            "(ts, created_at, agent, year, week, summary) VALUES (?,?,?,?,?,?)",
            (int(time.time()), _utc_now(), agent, year, week, summary),
        )

    # File: data/summaries/<agent>/<year>-W<week:02d>.md
    agent_dir = _SUMMARIES_DIR / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / f"{year}-W{week:02d}.md"
    path.write_text(summary + "\n", encoding="utf-8")
    log.info("weekly_summary: stored %s/%d-W%02d → %s", agent, year, week, path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def check_and_send(
    user_id: int,
    send_fn: Callable[[str], Awaitable[None]],
) -> None:
    """Check for missing weekly summaries across all agents and push them.

    Designed to run as a background asyncio task — fires after the user's
    message is already answered so it never adds latency.

    `send_fn` is an async callable that sends one Telegram message to the user,
    e.g. `lambda text: bot.send_message(chat_id=user_id, text=text)`.
    """
    from .agents.discovery import get_agents

    agents = get_agents()
    to_send: list[tuple[str, int, int, str]] = []  # (agent, year, week, summary)

    for agent in agents:
        missing = _missing_weeks(agent)
        for year, week in missing:
            notes = _fetch_notes(agent, year, week)

            if not notes:
                # No notes logged — mark the week as done so we never revisit it,
                # but skip the LLM call and don't push anything to Telegram.
                _store_empty(agent, year, week)
                log.debug("weekly_summary: no notes for %s W%d/%d — skipped", agent, week, year)
                continue

            log.info(
                "weekly_summary: generating %s W%d/%d (%d notes)",
                agent, week, year, len(notes),
            )
            # Yield to event loop between LLM calls so the bot stays responsive
            await asyncio.sleep(0)
            summary = await asyncio.get_running_loop().run_in_executor(
                None, _generate_summary, agent, year, week, notes
            )
            _store(agent, year, week, summary)
            to_send.append((agent, year, week, summary))
            audit.log_event(
                "weekly_summary_generated",
                agent=agent,
                year=year,
                week=week,
                num_notes=len(notes),
            )

    if not to_send:
        return

    # Push each summary as a separate Telegram message so they don't hit the
    # 4096-char limit and are easy to scroll through individually.
    for agent, year, week, summary in to_send:
        try:
            await send_fn(summary)
            # Small delay between messages so Telegram doesn't rate-limit us
            await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log.error("weekly_summary: failed to send %s W%d/%d: %s", agent, week, year, e)
