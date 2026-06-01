"""Configuration loaded from environment variables.

The .env file is read once at import time. Missing required values raise
KeyError immediately, so the bot fails fast on misconfiguration instead of
silently sending requests with no token.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    """Return the value of `name` from the environment, or raise KeyError with a helpful message."""
    value = os.environ.get(name)
    if not value:
        raise KeyError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


# --- Required ---------------------------------------------------------------
DEEPSEEK_API_KEY: str = _required("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN: str = _required("TELEGRAM_BOT_TOKEN")

# --- Allow-list -------------------------------------------------------------
# Comma-separated list of Telegram numeric user IDs. Anyone not in this set
# gets a polite refusal instead of an LLM call (saves your DeepSeek credit).
_raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
ALLOWED_USERS: set[int] = {
    int(x) for x in _raw_ids.split(",") if x.strip().isdigit()
}
if not ALLOWED_USERS:
    raise KeyError(
        "TELEGRAM_ALLOWED_USER_IDS must list at least one numeric Telegram ID."
    )

# --- Optional ---------------------------------------------------------------
DB_PATH: str = os.environ.get("DB_PATH", "me.db")

# Tavily web search API key (https://app.tavily.com).
# Leave blank to disable the web_search tool — the bot starts fine without it.
TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")

# Local faster-whisper HTTP server for voice-to-text transcription.
# Set to "" to disable voice message handling.
WHISPER_API_URL: str = os.environ.get("WHISPER_API_URL", "http://localhost:8000")
WHISPER_API_KEY: str = os.environ.get("WHISPER_API_KEY", "")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

# Model: deepseek-chat (V3, fast/cheap) or deepseek-reasoner (R1, slower/smarter).
DEEPSEEK_MODEL: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# Base URL for the LLM API. DeepSeek is OpenAI-compatible, so changing this
# to "https://api.openai.com/v1" (+ swapping the API key) switches providers.
# Works for any OpenAI-compatible endpoint: local Ollama, Azure OpenAI, etc.
DEEPSEEK_BASE_URL: str = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
)

# Temperature for the main agent loop. Controls response creativity/randomness.
# 0.0 = fully deterministic, 1.0 = most varied. 0.9 is a good default for a
# personal assistant — creative enough to feel natural, not so random it hallucinates.
# Lower this (e.g. 0.5) if you want more focused, conservative answers.
AGENT_TEMPERATURE: float = float(os.environ.get("AGENT_TEMPERATURE", "0.8"))

# Maximum number of tool-call iterations per user message. Each iteration is
# one LLM call + its tool calls. Most questions resolve in 1–3 turns; the cap
# prevents a buggy or looping model from burning tokens indefinitely.
MAX_TOOL_TURNS: int = int(os.environ.get("MAX_TOOL_TURNS", "10"))

# Per-request timeout (seconds) for the main agent LLM call. Without this a
# hung DeepSeek connection blocks the worker thread forever and the user never
# gets a reply. On timeout the call is retried up to LLM_MAX_RETRIES times.
LLM_TIMEOUT_SECONDS: float = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

# How many times to retry the main LLM call on a transient error (timeout,
# connection reset, 5xx). 2 retries = 3 total attempts.
LLM_MAX_RETRIES: int = int(os.environ.get("LLM_MAX_RETRIES", "2"))

# How many turns of prior conversation to include in each LLM call.
HISTORY_TURNS: int = int(os.environ.get("HISTORY_TURNS", "20"))

# Conversation summarisation threshold.
# When an agent's total turn count exceeds this, the oldest turns (beyond the
# recent 20) are summarised into a single paragraph and injected as persistent
# context. Summarisation only triggers once per threshold crossing (not every call).
CONVERSATION_SUMMARY_THRESHOLD: int = int(
    os.environ.get("CONVERSATION_SUMMARY_THRESHOLD", "40")
)

# Default agent for diary entries when the router returns agent=none.
# Override if you've replaced or renamed the growth agent.
DIARY_DEFAULT_AGENT: str = os.environ.get("DIARY_DEFAULT_AGENT", "growth")

# Default agent for grill sessions when the router returns agent=none
# (e.g. bare "grill me" with no domain specified).
# Growth is the most natural default for open-ended self-challenge.
GRILL_DEFAULT_AGENT: str = os.environ.get("GRILL_DEFAULT_AGENT", "growth")

# Timezone for week boundary calculations (weekly summaries).
# Weekly summaries are generated on Monday 00:00 in this timezone.
# Any IANA timezone name works: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIMEZONE: str = os.environ.get("TIMEZONE", "Europe/Amsterdam")

# --- Telegram display toggles -----------------------------------------------
# These are independent of LOG_LEVEL — flip them in .env without switching
# to full debug mode (which floods the console and log files).

# Show which agent is handling the conversation at the top of each reply.
# Format: "🤖 career"
# Default false — enable with SHOW_AGENT_NAME=true in .env.
SHOW_AGENT_NAME: bool = os.environ.get("SHOW_AGENT_NAME", "false").lower() == "true"

# Show every tool call as a header line on each Telegram reply.
# Format: "🔧 tool_name(arg=val) → result [42ms]"
# Default false — enable with SHOW_TOOL_CALLS=true in .env.
SHOW_TOOL_CALLS: bool = os.environ.get("SHOW_TOOL_CALLS", "false").lower() == "true"

# Forward DeepSeek reasoning_content (chain-of-thought) to the Telegram chat.
# Only relevant when using a reasoning model (deepseek-reasoner / R1).
# Reasoning is shown as a collapsed italic block before the final answer.
# Default false — enable with SHOW_REASONING=true in .env.
SHOW_REASONING: bool = os.environ.get("SHOW_REASONING", "false").lower() == "true"

# How many characters of reasoning to show in Telegram before truncating.
# Reasoning tokens can run to thousands of chars; cap keeps messages readable.
REASONING_MAX_CHARS: int = int(os.environ.get("REASONING_MAX_CHARS", "1000"))

# --- Per-component debug logging -------------------------------------------
# Edit these directly when debugging. All are no-ops unless LOG_LEVEL=DEBUG.
# When active, a separate data/debug.log.json is written with full-fidelity
# data (no truncation). Enable only what you're currently investigating —
# DEBUG_LOG_MESSAGES in particular can produce very large records.
_IS_DEBUG = LOG_LEVEL == "DEBUG"

DEBUG_LOG_ROUTER:        bool = _IS_DEBUG and True  # router.md prompt + raw response + token usage
DEBUG_LOG_AGENT_PROMPT:  bool = _IS_DEBUG and True  # full system prompt (persona.md + context block) — can be large
DEBUG_LOG_LLM_RESPONSE:  bool = _IS_DEBUG and True   # complete reply text every turn (no 500-char cap)
DEBUG_LOG_REASONING:     bool = _IS_DEBUG and True  # DeepSeek reasoning_content / thinking tokens — can be very long
DEBUG_LOG_FINISH_REASON: bool = _IS_DEBUG and True   # stop / tool_calls / length per turn
DEBUG_LOG_MESSAGES:      bool = _IS_DEBUG and True  # full messages[] sent to API (⚠ can be large)
DEBUG_LOG_TOOL_CALLS:    bool = _IS_DEBUG and True  # each tool call: name, args, result, duration
DEBUG_LOG_TOOL_SUMMARY:  bool = _IS_DEBUG and True  # per-turn summary: tools called, ok/fail, total time
DEBUG_LOG_DISPATCH:      bool = _IS_DEBUG and True  # routing decision: agent, type, sticky, bridge, excluded tools
DEBUG_LOG_CONTEXT_SIZE:  bool = _IS_DEBUG and True  # chars + tool count sent to API before each call
DEBUG_LOG_STAGE:         bool = _IS_DEBUG and True  # when a destructive tool stages an action
