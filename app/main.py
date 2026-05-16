"""Telegram bot entrypoint.

Uses long polling so the bot needs no inbound network access — your NAS
reaches out to Telegram, not the other way around.

New in this revision:
- `chat()` runs an agent loop with tool calls (Notion, etc.).
- `/confirm <id>` and `/cancel <id>` execute or discard pending destructive
  actions staged by tools like `notion_archive_page`.
"""
from __future__ import annotations

import json
import logging
import os
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
from .config import ALLOWED_USERS, DEEPSEEK_MODEL, HISTORY_TURNS, LOG_LEVEL, MAX_TOOL_TURNS, TELEGRAM_BOT_TOKEN
from .db import init as db_init, log_message, recent_history
from .llm import format_debug_header, format_staged_footer
from .tools import pending  # ensures pending table is created
from .agents.dispatch import handle as agent_handle


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


notion_tools   = _safe_import("notion")
gmail_tools    = _safe_import("gmail")
calendar_tools = _safe_import("calendar")
file_tools     = _safe_import("files")
prompt_tools   = _safe_import("prompts")

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
    EXECUTORS["notion_archive_page"] = notion_tools.execute_confirmed
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

    # Plain-text shortcuts: bare "confirm" / "cancel" (case-insensitive, with
    # optional punctuation) act as /confirm or /cancel when a pending action
    # exists. Saves the user from typing the slash + action_id every time.
    # "yes confirm" is included because the agent sometimes replies "Reply
    # 'yes confirm' to proceed" and users type it verbatim.
    stripped = text.strip().lower().rstrip(".!?")
    if stripped in ("confirm", "yes confirm") and pending.latest_pending_for(user_id):
        ctx.args = []
        await cmd_confirm(update, ctx)
        return
    if stripped in ("cancel", "discard") and pending.latest_pending_for(user_id):
        ctx.args = []
        await cmd_cancel(update, ctx)
        return

    log_message(user_id, "user", text)
    audit.log_event("user_message", user_id=user_id, text=text)

    history = _trim_after_reset(recent_history(user_id, limit=HISTORY_TURNS))

    # Two-pass routing: classify → agent dispatch (or general llm).
    result, routed_agent = agent_handle(text, user_id=user_id, global_history=history)

    # Persist only the assistant's natural-language text. The invocation list
    # is fully captured in audit.log_event("tool_call", ...) per turn.
    log_message(user_id, "assistant", result.text)
    staged_ids = [
        i.result.get("action_id")
        for i in result.invocations
        if isinstance(i.result, dict) and i.result.get("staged")
    ]
    audit.log_event(
        "assistant_reply",
        user_id=user_id,
        agent=routed_agent,
        text=result.text,
        num_invocations=len(result.invocations),
        staged_action_ids=staged_ids,
    )

    out_text = _assemble_reply(result)
    await msg.reply_text(out_text)


# Telegram's hard cap is 4096 UTF-16 code units (not bytes). We use 4000 to
# leave ~96 chars of headroom for the "———" dividers and the staged-action
# footer that _assemble_reply() appends after measuring the LLM text.
_TELEGRAM_LIMIT = 4000


def _assemble_reply(result) -> str:
    """Compose the Telegram reply: optional debug header, the LLM text, and
    an unconditional footer listing any actions staged this turn.

    The footer is the structural safety net — if the LLM forgets to mention
    /confirm, the user still sees the staged action_id with its preview.
    """
    sections: list[str] = []

    if SHOW_TOOL_CALLS and result.invocations:
        sections.append(format_debug_header(result.invocations))

    sections.append(result.text or "")

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
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Initialise the database and start the Telegram long-polling loop."""
    db_init()
    pending.init()

    # Startup summary — single place to confirm the bot is configured correctly.
    from .tools.registry import REGISTRY
    log.info("Model: %s  temperature: %s  max_tool_turns: %s", DEEPSEEK_MODEL, os.environ.get("AGENT_TEMPERATURE", "0.9"), MAX_TOOL_TURNS)
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

    log.info("Starting long-polling loop")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
