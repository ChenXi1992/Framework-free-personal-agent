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
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# Model: deepseek-chat (V3, fast/cheap) or deepseek-reasoner (R1, slower/smarter).
DEEPSEEK_MODEL: str = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

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
AGENT_TEMPERATURE: float = float(os.environ.get("AGENT_TEMPERATURE", "0.3"))

# Maximum number of tool-call iterations per user message. Each iteration is
# one LLM call + its tool calls. Most questions resolve in 1–3 turns; the cap
# prevents a buggy or looping model from burning tokens indefinitely.
MAX_TOOL_TURNS: int = int(os.environ.get("MAX_TOOL_TURNS", "10"))

# How many turns of prior conversation to include in each LLM call.
HISTORY_TURNS: int = int(os.environ.get("HISTORY_TURNS", "20"))

# --- Per-component debug logging -------------------------------------------
# Edit these directly when debugging. All are no-ops unless LOG_LEVEL=DEBUG.
# When active, a separate data/debug.log.json is written with full-fidelity
# data (no truncation). Enable only what you're currently investigating —
# DEBUG_LOG_MESSAGES in particular can produce very large records.
_IS_DEBUG = LOG_LEVEL == "DEBUG"

DEBUG_LOG_ROUTER:        bool = _IS_DEBUG and True   # router.md prompt + raw response + token usage
DEBUG_LOG_CLASSIFIER:    bool = _IS_DEBUG and False  # classifier.md prompt + raw response (notes only)
DEBUG_LOG_AGENT_PROMPT:  bool = _IS_DEBUG and False  # full system prompt (persona.md + context block) — can be large
DEBUG_LOG_LLM_RESPONSE:  bool = _IS_DEBUG and True   # complete reply text every turn (no 500-char cap)
DEBUG_LOG_REASONING:     bool = _IS_DEBUG and False  # DeepSeek reasoning_content / thinking tokens — can be very long
DEBUG_LOG_FINISH_REASON: bool = _IS_DEBUG and True   # stop / tool_calls / length per turn
DEBUG_LOG_MESSAGES:      bool = _IS_DEBUG and False  # full messages[] sent to API (⚠ can be large)
