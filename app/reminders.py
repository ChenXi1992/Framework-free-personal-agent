"""Reminder scheduler.

Runs as a repeating job inside the Telegram bot's job_queue (every minute).
For each active reminder that is due today at the current time:
  1. Check if a session already happened today (query agent_conversations).
  2. If messages exist — ask the LLM to judge whether it was a meaningful session.
  3. If no session (or LLM says no) — generate a smart push message and send it.
  4. Mark last_fired so it doesn't fire twice in the same day.
"""
from __future__ import annotations

import datetime
import logging

from zoneinfo import ZoneInfo

from openai import OpenAI, OpenAIError
from telegram.ext import ContextTypes

from .config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    TIMEZONE, ALLOWED_USERS,
)
from .db import _connect, _utc_now

log = logging.getLogger(__name__)

_WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

_PROMPTS_DIR = __import__("pathlib").Path(__file__).parent.parent / "data" / "prompts"
_GOALS_DIR   = __import__("pathlib").Path(__file__).parent.parent / "data" / "goals"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_local() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(TIMEZONE))


def _today_str() -> str:
    return _now_local().strftime("%Y-%m-%d")


def _is_due(days: str, fire_time: str, now: datetime.datetime) -> bool:
    """Return True if this reminder should fire at some point today.

    Fires if:
      - today's weekday matches the days list (or 'daily')
      - fire_time has already passed today (no upper bound)

    No upper bound — this is intentional catch-up logic. If the Mac was
    asleep during the scheduled window, the reminder fires as soon as the
    bot wakes up. Double-firing is prevented by the `last_fired == today`
    check in check_due_reminders, which is set after the first fire.
    """
    today_name = _WEEKDAY_NAMES[now.weekday()]
    day_list = [d.strip() for d in days.split(",")]
    if "daily" not in day_list and today_name not in day_list:
        return False

    h, m = map(int, fire_time.split(":"))
    tz = ZoneInfo(TIMEZONE)
    fire_dt = datetime.datetime.combine(now.date(), datetime.time(h, m), tzinfo=tz)
    return now >= fire_dt


def _load_file(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _get_today_conversations(agent: str) -> list[dict]:
    """Return today's agent_conversations rows for the given agent."""
    tz = ZoneInfo(TIMEZONE)
    start_of_day = int(
        datetime.datetime.combine(
            _now_local().date(), datetime.time.min, tzinfo=tz
        ).timestamp()
    )
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM agent_conversations "
            "WHERE agent = ? AND ts >= ? ORDER BY ts ASC",
            (agent, start_of_day),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def _llm(system: str, user: str, max_tokens: int = 200) -> str:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except OpenAIError as e:
        log.error("reminders: LLM call failed: %s", e)
        return ""


def _judge_session(agent: str, conversations: list[dict]) -> bool:
    """Ask the LLM whether today's conversations count as a meaningful session.

    Returns True if the session was meaningful (skip the reminder),
    False if it was too shallow or off-topic (fire the reminder).
    """
    if not conversations:
        return False

    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in conversations
    )
    system = (
        f"You are evaluating whether a conversation with the {agent} agent "
        f"counts as a meaningful session today.\n"
        f"For 'dutch': a meaningful session involves actual language practice — "
        f"writing, correction, grammar, vocabulary, or translation.\n"
        f"For 'workout': logging a session, discussing training, reviewing a plan.\n"
        f"For other agents: genuine engagement with the domain, not just a quick note.\n\n"
        f"Reply with only YES (meaningful session happened) or NO (it did not)."
    )
    user = f"Today's conversation:\n\n{convo_text}"
    answer = _llm(system, user, max_tokens=10).upper()
    return answer.startswith("YES")


def _generate_reminder_message(agent: str, label: str) -> str:
    """Generate a smart, personalised push message for this reminder."""
    # Load context: recent notes + goals file
    with _connect() as conn:
        rows = conn.execute(
            "SELECT summary FROM notes WHERE category = ? ORDER BY ts DESC LIMIT 5",
            (agent,),
        ).fetchall()
    recent_notes = "\n".join(f"- {r[0]}" for r in rows) or "(no recent notes)"
    goals = _load_file(_GOALS_DIR / f"{agent}.md") or "(no goals file)"

    system = (
        f"You are generating a short push notification reminder for Xi.\n"
        f"Agent: {agent}. Reminder: '{label}'.\n\n"
        f"Rules:\n"
        f"- Maximum 2 sentences. Be specific — reference actual goals or recent data.\n"
        f"- Motivating but not preachy. Direct tone.\n"
        f"- For Dutch: you may write partly in Dutch.\n"
        f"- No emojis unless they add meaning.\n"
        f"- Do NOT start with 'Hey' or 'Hi'.\n"
    )
    user = (
        f"Recent {agent} notes:\n{recent_notes}\n\n"
        f"Goals:\n{goals}\n\n"
        f"Write the push reminder now."
    )
    msg = _llm(system, user, max_tokens=100)
    return msg or f"Time for your {label} — you haven't done it yet today."


# ---------------------------------------------------------------------------
# Main scheduler job
# ---------------------------------------------------------------------------

async def check_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job that runs every minute — checks for due reminders and fires them."""
    now = _now_local()
    today = _today_str()

    with _connect() as conn:
        due = conn.execute(
            "SELECT id, agent, label, days, fire_time, last_fired "
            "FROM reminders WHERE active = 1"
        ).fetchall()

    for row in due:
        rid, agent, label, days, fire_time, last_fired = row

        if not _is_due(days, fire_time, now):
            continue
        if last_fired == today:
            continue  # already fired today

        log.info("reminders: checking %s / '%s'", agent, label)

        # Judge session quality in a thread (blocking LLM call)
        import asyncio
        conversations = _get_today_conversations(agent)
        meaningful = await asyncio.get_running_loop().run_in_executor(
            None, _judge_session, agent, conversations
        )
        if meaningful:
            log.info("reminders: %s session already done today — skipping '%s'", agent, label)
            # Mark fired so we don't re-evaluate every minute for the rest of the window
            with _connect() as conn:
                conn.execute(
                    "UPDATE reminders SET last_fired = ? WHERE id = ?",
                    (today, rid),
                )
            continue

        # Generate smart message and push
        message = await asyncio.get_running_loop().run_in_executor(
            None, _generate_reminder_message, agent, label
        )

        # Send to all allowed users (single-user bot — ALLOWED_USERS has one entry)
        for user_id in ALLOWED_USERS:
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
                log.info("reminders: pushed '%s' to user %s", label, user_id)
            except Exception as e:  # noqa: BLE001
                log.error("reminders: failed to push to %s: %s", user_id, e)

        with _connect() as conn:
            conn.execute(
                "UPDATE reminders SET last_fired = ? WHERE id = ?",
                (today, rid),
            )
