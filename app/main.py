"""Telegram bot entrypoint.

Receives messages via long polling (no inbound port required — works behind
any NAT/firewall) and routes them through the agent dispatch system.

Architecture overview
─────────────────────
1. Every plain-text message enters handle_msg().
2. Plain-text "confirm"/"cancel" shortcuts are intercepted before routing.
3. Real messages → dispatch.handle() → specialist agent or general LLM.
4. Responses are streamed to Telegram turn-by-turn via the _on_chunk callback
   so the user sees partial results immediately rather than waiting for the
   full agent loop to complete.
5. Destructive tool actions (send email, create calendar event, etc.) are
   *staged* — nothing executes until the user sends /confirm <id>.
6. Weekly summaries fire as a background asyncio task after each reply so
   they never add latency to the conversation.

Commands
────────
/start          — welcome message + command list
/tools          — list every registered tool
/reset          — clear conversation memory for this session
/confirm [id]   — execute a staged destructive action (id optional if only one pending)
/cancel  [id]   — discard a staged action
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import audit
from .config import AGENT_TEMPERATURE, ALLOWED_USERS, DEEPSEEK_MODEL, HISTORY_TURNS, LOG_LEVEL, MAX_TOOL_TURNS, TELEGRAM_BOT_TOKEN
from .db import init as db_init, log_message, recent_history
from .llm import format_debug_header, format_staged_footer, format_write_confirmation
from .tools import pending  # ensures pending table is created
from .agents.dispatch import handle as agent_handle
from .weekly_summary import check_and_send as _check_weekly_summaries
from .reminders import check_due_reminders


# ---------------------------------------------------------------------------
# Logging setup — must happen before any module calls log.warning() etc.
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Format a log record as a pretty-printed JSON block.

    Matches the style of audit.py: 2-space indent, blank line as separator,
    ISO-8601 timestamp, exc traceback included when present.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts":      datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, indent=2, ensure_ascii=False) + "\n"


class _ExactLevelFilter(logging.Filter):
    """Pass only records at exactly the given level (e.g. only INFO, not WARNING)."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self._level


class _MinLevelFilter(logging.Filter):
    """Pass records at or above the given level (e.g. ERROR and CRITICAL)."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self._level


def _setup_logging() -> None:
    """Configure the root logger with a console handler and three JSON file handlers.

    File layout under data/:
      agent.INFO.json     — INFO messages only
      agent.WARNING.json  — WARNING messages only
      agent.ERROR.json    — ERROR and CRITICAL (includes tracebacks)

    The console keeps the human-readable format. Files use JSON so they can be
    queried with jq the same way as agent.log.json and debug.log.json.
    """
    log_dir = Path(os.environ.get("DB_PATH", "./data/me.db")).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    # Console — human-readable, respects LOG_LEVEL
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    json_fmt = _JsonFormatter()

    # data/agent.INFO.json — INFO only
    h_info = logging.FileHandler(log_dir / "agent.INFO.json", encoding="utf-8")
    h_info.setFormatter(json_fmt)
    h_info.addFilter(_ExactLevelFilter(logging.INFO))
    root.addHandler(h_info)

    # data/agent.WARNING.json — WARNING only
    h_warn = logging.FileHandler(log_dir / "agent.WARNING.json", encoding="utf-8")
    h_warn.setFormatter(json_fmt)
    h_warn.addFilter(_ExactLevelFilter(logging.WARNING))
    root.addHandler(h_warn)

    # data/agent.ERROR.json — ERROR and above (includes tracebacks from log.exception)
    h_err = logging.FileHandler(log_dir / "agent.ERROR.json", encoding="utf-8")
    h_err.setFormatter(json_fmt)
    h_err.addFilter(_MinLevelFilter(logging.ERROR))
    root.addHandler(h_err)


_setup_logging()
log = logging.getLogger("me-agent")


# ---------------------------------------------------------------------------
# Optional integration imports
# ---------------------------------------------------------------------------

# Each integration module exposes `execute_confirmed(tool_name, arguments)` for
# its destructive tools. We import them defensively — if a workspace's deps
# aren't installed, the rest of the bot still runs.
def _safe_import(modname: str):
    """Import `app.tools.<modname>` and return the module, or None on failure.

    Keeps the bot alive when an optional dependency (google-api-python-client,
    notion-client, etc.) is not installed in the current environment.
    """
    try:
        from importlib import import_module
        return import_module(f"app.tools.{modname}")
    except Exception as e:  # noqa: BLE001
        log.warning("Skipping %s executor (import failed): %s", modname, e)
        return None


notion_tools    = _safe_import("notion")
gmail_tools     = _safe_import("gmail")
calendar_tools  = _safe_import("calendar")
file_tools      = _safe_import("files")
prompt_tools    = _safe_import("prompts")
reminder_tools  = _safe_import("reminders")

# Show every tool invocation as a header line on each Telegram reply when
# logging is at DEBUG level. Off in INFO/WARNING because it's chatty for
# day-to-day use. Toggle by setting LOG_LEVEL=DEBUG in .env and restarting.
SHOW_TOOL_CALLS = LOG_LEVEL == "DEBUG"


# Maps tool_name → the module-level execute_confirmed() that knows how to
# actually run that tool once the user has confirmed. Each destructive
# integration (gmail, calendar, etc.) exposes one execute_confirmed(tool_name,
# arguments) function that handles all its own staged tools. Adding a new
# destructive tool requires: (1) calling stage_action() inside the tool,
# (2) adding execute_confirmed() to the integration module, and
# (3) registering the tool_name here.
EXECUTORS: dict[str, callable] = {}
if notion_tools:
    EXECUTORS["notion_append_paragraph"] = notion_tools.execute_confirmed
    EXECUTORS["notion_create_page"]      = notion_tools.execute_confirmed
    EXECUTORS["notion_archive_page"]     = notion_tools.execute_confirmed
if gmail_tools:
    EXECUTORS["gmail_send_message"] = gmail_tools.execute_confirmed
if calendar_tools:
    EXECUTORS["calendar_create_event"] = calendar_tools.execute_confirmed
    EXECUTORS["calendar_delete_event"] = calendar_tools.execute_confirmed
if file_tools:
    EXECUTORS["file_write"] = file_tools.execute_confirmed
    EXECUTORS["file_edit"] = file_tools.execute_confirmed
    EXECUTORS["file_append"] = file_tools.execute_confirmed
if prompt_tools:
    EXECUTORS["prompt_propose"] = prompt_tools.execute_confirmed
if reminder_tools:
    EXECUTORS["reminder_set"] = reminder_tools.execute_confirmed


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _allowed(update: Update) -> bool:
    """Return True if the Telegram user is in the ALLOWED_USERS allow-list."""
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_tools(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tools — list every registered tool by name and description."""
    if not _allowed(update):
        return
    from .tools.registry import REGISTRY
    lines = ["*Available tools:*\n"]
    for name, t in REGISTRY.items():
        first_line = t.description.split("\n")[0][:80]
        lines.append(f"• `{name}` — {first_line}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — show a welcome message with the list of available commands."""
    if not _allowed(update):
        await update.message.reply_text("Not authorized.")
        return
    await update.message.reply_text(
        "Hi Xi. I'm online. I can read/write Notion (when configured) and "
        "remember our conversation.\n\n"
        "Commands:\n"
        "  /reset — start a fresh conversation\n"
        "  /confirm <id> — execute a staged destructive action\n"
        "  /cancel <id> — discard a staged action"
    )


async def cmd_reset(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reset — insert a sentinel message so _trim_after_reset drops prior history."""
    if not _allowed(update):
        return
    log_message(update.effective_user.id, "system", "--- conversation reset ---")
    await update.message.reply_text("Memory cleared for this conversation.")


async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /confirm [id] — execute the specified (or most recent) pending action."""
    if not _allowed(update):
        return
    user_id = update.effective_user.id

    # /confirm with no arg -> default to the latest still-pending action.
    if ctx.args:
        action_id = ctx.args[0]
        action = pending.get(action_id)
        if action is None:
            await update.message.reply_text(f"No such action: {action_id}")
            return
        if action["user_id"] != user_id:
            await update.message.reply_text("That action belongs to a different user.")
            return
    else:
        action = pending.latest_pending_for(user_id)
        if action is None:
            await update.message.reply_text("No pending actions to confirm.")
            return
        action_id = action["id"]

    if action["status"] != "pending":
        await update.message.reply_text(
            f"Action {action_id} is already {action['status']}."
        )
        return

    executor = EXECUTORS.get(action["tool_name"])
    if executor is None:
        await update.message.reply_text(
            f"No executor registered for {action['tool_name']}. "
            "This is a bug — please report."
        )
        return

    exec_result = executor(action["tool_name"], action["arguments"])
    audit.log_event(
        "confirm",
        user_id=update.effective_user.id,
        action_id=action_id,
        tool=action["tool_name"],
        arguments=action["arguments"],
        ok=bool(exec_result.get("ok")),
        result=exec_result,
    )
    if exec_result.get("ok"):
        pending.mark(action_id, "executed", str(exec_result))
        await update.message.reply_text(f"Done. {action['preview']}")
    else:
        pending.mark(action_id, "failed", str(exec_result))
        await update.message.reply_text(
            f"Failed: {exec_result.get('error', 'unknown error')}"
        )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel [id] — discard the specified (or most recent) pending action."""
    if not _allowed(update):
        return
    user_id = update.effective_user.id

    if ctx.args:
        action_id = ctx.args[0]
        action = pending.get(action_id)
        if action is None or action["user_id"] != user_id:
            await update.message.reply_text(f"No such action: {action_id}")
            return
    else:
        action = pending.latest_pending_for(user_id)
        if action is None:
            await update.message.reply_text("No pending actions to cancel.")
            return
        action_id = action["id"]

    if action["status"] != "pending":
        await update.message.reply_text(
            f"Action {action_id} is already {action['status']}."
        )
        return
    pending.mark(action_id, "cancelled")
    audit.log_event(
        "cancel",
        user_id=update.effective_user.id,
        action_id=action_id,
        tool=action["tool_name"],
    )
    await update.message.reply_text(f"Cancelled: {action['preview']}")


# ---------------------------------------------------------------------------
# Plain-text handler (the agent loop)
# ---------------------------------------------------------------------------

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every plain-text message: route through the agent system and reply."""
    msg = update.message
    if msg is None:
        return
    if not _allowed(update):
        log.warning(
            "Unauthorized: id=%s username=%s",
            update.effective_user.id if update.effective_user else None,
            update.effective_user.username if update.effective_user else None,
        )
        await msg.reply_text("Not authorized.")
        return

    user_id = update.effective_user.id
    text = msg.text or ""
    if not text.strip():
        return

    # Plain-text shortcuts: "confirm" / "cancel" act as /confirm or /cancel.
    # Normalise by stripping all non-letter characters (removes emoji, punctuation,
    # trailing smileys like ":)") before matching, so "confirm :)" and "confirn"
    # (common typo) both work. The set covers the typos seen in production logs.
    _norm = re.sub(r"[^a-z ]", "", text.strip().lower()).strip()
    _norm = " ".join(_norm.split())  # collapse whitespace

    _CONFIRM_TRIGGERS = {"confirm", "confirn", "confim", "comfirm",
                         "yes confirm", "yes confirn", "yes confim"}
    _CANCEL_TRIGGERS  = {"cancel", "discard"}

    if _norm in _CONFIRM_TRIGGERS:
        if pending.latest_pending_for(user_id):
            ctx.args = []
            await cmd_confirm(update, ctx)
        else:
            await msg.reply_text("Nothing to confirm — no pending action found.")
        return
    if _norm in _CANCEL_TRIGGERS:
        if pending.latest_pending_for(user_id):
            ctx.args = []
            await cmd_cancel(update, ctx)
        else:
            await msg.reply_text("Nothing to cancel — no pending action found.")
        return

    log_message(user_id, "user", text)
    audit.log_event("user_message", user_id=user_id, text=text)

    history = _trim_after_reset(recent_history(user_id, limit=HISTORY_TURNS))

    # Build a per-turn callback that sends each chunk immediately as it
    # completes, rather than buffering everything to the end.
    # chat() runs in a thread (via run_in_executor); run_coroutine_threadsafe
    # bridges back to the async event loop so we can call reply_text() safely.
    _event_loop = asyncio.get_running_loop()

    def _on_chunk(turn_invocations, chunk_text: str) -> None:
        sections: list[str] = []
        if SHOW_TOOL_CALLS and turn_invocations:
            sections.append(format_debug_header(turn_invocations))
        # Always show note/diary/todo write confirmations so the user has
        # ground-truth proof the tool actually ran — independent of LOG_LEVEL.
        write_conf = format_write_confirmation(turn_invocations)
        if write_conf:
            sections.append(write_conf)
        if chunk_text:
            sections.append(chunk_text)
        if not sections:
            return
        out = "\n———\n".join(sections)
        if len(out) > _TELEGRAM_LIMIT:
            out = out[: _TELEGRAM_LIMIT - 3] + "..."
        # Fire-and-forget: schedule the send on the event loop and return
        # immediately so the worker thread can start the next LLM turn.
        # Coroutines are queued in submission order so messages arrive in order.
        asyncio.run_coroutine_threadsafe(msg.reply_text(out), _event_loop)

    # Two-pass routing: classify → agent dispatch (or general llm).
    # Run in a thread executor so the event loop stays free to service the
    # reply_text() calls that _on_chunk fires via run_coroutine_threadsafe.
    # Without run_in_executor, agent_handle blocks the event loop thread and
    # _on_chunk's future.result() deadlocks waiting for the loop to run.
    result, routed_agent = await _event_loop.run_in_executor(
        None,
        lambda: agent_handle(text, user_id=user_id, global_history=history, on_chunk=_on_chunk),
    )

    # Persist only the assistant's natural-language text. The invocation list
    # is fully captured in audit.log_event("tool_call", ...) per turn.
    log_message(user_id, "assistant", result.text)
    staged_ids = [
        i.result.get("action_id")
        for i in result.invocations
        if isinstance(i.result, dict) and i.result.get("staged")
    ]

    # Hallucination guard: detect when the agent claims to have written data
    # but called no write tools.  This catches the failure mode where the LLM
    # says "Done! Added to your todo list" without ever calling todo_add().
    # We check invocations (ground truth) rather than trusting the reply text.
    #
    # Guard uses a regex instead of a keyword list to avoid false positives.
    # Broad keywords like "noted", "done", "updated" fire on normal analysis
    # replies ("I've noted your baseline", "well done").  The regex requires an
    # explicit first-person claim of a completed write action:
    #   "I saved …", "I've logged …", "I added …", "I recorded …", etc.
    _write_tools = {
        "todo_add", "note_add", "diary_add",
        "file_write", "file_append", "file_edit",
        "calendar_create_event", "gmail_send_message",
        "notion_archive_page",
    }
    # Matches: "I saved", "I've saved", "I have saved", "I added", "I've logged", …
    _CLAIM_RE = re.compile(
        r"\bi(?:'ve|'ve|\s+have)?\s+(?:saved|added|logged|recorded|stored|written|appended)\b"
    )
    _tools_called = {inv.name for inv in result.invocations}
    _write_tools_called = _tools_called & _write_tools
    _reply_lower = result.text.lower()
    _claims_write = bool(_CLAIM_RE.search(_reply_lower))

    if _claims_write and not _write_tools_called and not staged_ids:
        log.warning(
            "Hallucination guard triggered: agent=%s claimed a write but called no write tools "
            "(tools called: %s)",
            routed_agent, _tools_called or "none",
        )
        # Append warning to the result text — the final chunk was already sent,
        # so we send the warning as a follow-up message.
        warning = (
            "\n\n⚠️ *Heads up: no data was actually saved in this response. "
            "Ask me to try again if something was supposed to be stored.*"
        )
        await msg.reply_text(warning)
        result = result.__class__(
            text=result.text + warning,
            invocations=result.invocations,
        )

    audit.log_event(
        "assistant_reply",
        user_id=user_id,
        agent=routed_agent if routed_agent != "none" else "general",
        text=result.text,
        num_invocations=len(result.invocations),
        staged_action_ids=staged_ids,
    )

    # All content was already sent via _on_chunk. Only the staged-action
    # footer needs to go out now (if any actions are pending confirmation).
    staged_footer = format_staged_footer(result.invocations)
    if staged_footer:
        await msg.reply_text(staged_footer)

    # Fire weekly summary check in the background — after the user's reply
    # is already sent so it never adds latency to the conversation.
    async def _send(text: str) -> None:
        await msg.reply_text(text)

    asyncio.create_task(_check_weekly_summaries(user_id, _send))


# Telegram's hard cap is 4096 UTF-16 code units (not bytes). We use 4000 to
# leave ~96 chars of headroom for the "———" dividers and the staged-action
# footer that _assemble_reply() appends after measuring the LLM text.
_TELEGRAM_LIMIT = 4000


def _assemble_reply(result) -> str:
    """Compose the Telegram reply: optional debug header, the LLM text, and
    an unconditional footer listing any actions staged this turn.

    The footer is the structural safety net — if the LLM forgets to mention
    /confirm, the user still sees the staged action_id with its preview.

    NOTE: This function is NOT used in the live streaming path (handle_msg).
    Streaming delivers each turn immediately via _on_chunk. This function
    exists as a reference implementation and for any non-streaming callers
    that may be added in future.
    """
    sections: list[str] = []

    if SHOW_TOOL_CALLS and result.invocations:
        sections.append(format_debug_header(result.invocations))

    # Prepend any text the LLM emitted alongside tool-call turns.
    # These are mid-loop thoughts/explanations that would otherwise be silently
    # dropped (e.g. "I need to be honest: I didn't log anything — let me fix that").
    for interim in result.interim_texts:
        sections.append(interim)

    sections.append(result.text or "")

    write_conf = format_write_confirmation(result.invocations)
    if write_conf:
        sections.append(write_conf)

    footer = format_staged_footer(result.invocations)
    if footer:
        sections.append(footer)

    out = "\n———\n".join(s for s in sections if s)
    if len(out) > _TELEGRAM_LIMIT:
        out = out[: _TELEGRAM_LIMIT - 3] + "..."
    return out


def _trim_after_reset(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop all turns up to (and including) the most recent /reset sentinel.

    /reset inserts a system message with "conversation reset" in it.
    We find the last such message and return only what follows, giving
    the LLM a clean slate without losing the user's new messages.
    """
    cutoff = -1
    for i, m in enumerate(history):
        if m["role"] == "system" and "conversation reset" in m["content"]:
            cutoff = i
    return history[cutoff + 1 :] if cutoff >= 0 else history


# ---------------------------------------------------------------------------
# Telegram error handler
# ---------------------------------------------------------------------------

async def _telegram_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log Telegram / network errors and notify the user when possible.

    Without this, httpx.RemoteProtocolError and similar transient network
    errors are silently swallowed and the user gets no response.
    """
    log.error("Telegram update error: %s", context.error, exc_info=context.error)
    # Try to send a human-readable message so the user knows to retry.
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my end — please try again."
            )
    except Exception:  # noqa: BLE001
        pass  # Don't let the error handler itself crash the loop


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Initialise the database and start the Telegram long-polling loop."""
    db_init()
    pending.init()

    # Startup summary — single place to confirm the bot is configured correctly.
    from .tools.registry import REGISTRY
    log.info("Model: %s  temperature: %s  max_tool_turns: %s", DEEPSEEK_MODEL, AGENT_TEMPERATURE, MAX_TOOL_TURNS)
    log.info("Allowed users: %s", ALLOWED_USERS)
    log.info("Tools loaded (%d): %s", len(REGISTRY), ", ".join(sorted(REGISTRY)))
    log.info("Audit log: %s", audit.get_log_path())
    log.info(
        "Python logs: INFO→data/agent.INFO.json  WARNING→data/agent.WARNING.json  ERROR→data/agent.ERROR.json"
    )

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tools", cmd_tools))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    app.add_error_handler(_telegram_error_handler)

    # Reminder scheduler — checks every 60 seconds for due reminders.
    app.job_queue.run_repeating(check_due_reminders, interval=60, first=10)
    log.info("Reminder scheduler started (60s interval)")

    log.info("Starting long-polling loop")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
