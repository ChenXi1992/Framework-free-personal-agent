"""DeepSeek client + tool-calling agent loop.

DeepSeek's HTTP API is OpenAI-compatible and supports the `tools` parameter
in chat.completions, so the same code path will work with OpenAI/GPT-4 if you
ever swap providers — change the base_url, change the model.

The loop:
    1. Send the running conversation + tool catalog to the model.
    2. If the response is plain text, return it.
    3. If the response contains tool_calls, dispatch each one, append the
       results, loop. Bounded by MAX_TOOL_TURNS so a buggy model can't burn
       infinite tokens.

Returns a ChatResult that captures BOTH the final assistant text and a list
of every tool invocation that happened on the way. main.py uses the
invocation list to render a debug header (LOG_LEVEL=DEBUG) so hallucinated
or silently-staged tool calls become visible during development.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI, OpenAIError

from . import audit, debug_log
from .config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, AGENT_TEMPERATURE, MAX_TOOL_TURNS,
    DEBUG_LOG_LLM_RESPONSE, DEBUG_LOG_REASONING, DEBUG_LOG_FINISH_REASON, DEBUG_LOG_MESSAGES,
)
from .tools import registry

log = logging.getLogger(__name__)

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)

# MAX_TOOL_TURNS and AGENT_TEMPERATURE are imported from config so they can be
# tuned via .env without touching code. See config.py for their descriptions.

# Fallback system prompt used ONLY when chat() is called with no system_prompt
# argument. In normal operation dispatch.py always supplies a persona-specific
# prompt, so this acts as a safety net rather than the real system prompt.
SYSTEM_PROMPT = (
    "You are Xi's personal assistant. Be concise, direct, and never sycophantic.\n"
    "\n"
    "You have tool access to Xi's accounts. The exact set of available tools "
    "is provided to you on every call — read it. When the user asks about "
    "their email, notes, channels, messages, calendar, or any other personal "
    "data, CALL THE RELEVANT TOOL instead of refusing or guessing. If the "
    "user asks a meta-question like 'can you access my Gmail?', demonstrate "
    "by calling a small read tool (e.g., listing 3 recent items) and showing "
    "the result, rather than answering 'no'. If a tool returns an error, "
    "show the error to the user verbatim — don't pretend the integration "
    "doesn't exist.\n"
    "\n"
    "When showing tool results, include identifying details (titles, ids, "
    "URLs, timestamps) so the user can verify what you saw before any "
    "follow-up action.\n"
    "\n"
    "Destructive tools (archiving pages, sending mail, creating calendar "
    "events) do not execute when called — they return a human-readable "
    "preview. Show the preview verbatim, then end your reply with exactly: "
    "'Reply confirm to send or cancel to discard.' Do NOT include the "
    "action_id or '/confirm <id>' in your text — the bot appends a "
    "structural footer below your reply that carries the id and commands.\n"
    "\n"
    "When asked for advice, output: (1) the recommendation, (2) the single "
    "biggest reason it might be wrong, (3) one cheap experiment to test it."
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ToolInvocation:
    """One tool call observed during a chat turn."""
    name: str
    arguments: dict[str, Any]
    result: Any
    duration_ms: float
    ok: bool


@dataclass
class ChatResult:
    """What the agent loop returns: final text + everything that happened."""
    text: str
    invocations: list[ToolInvocation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def chat(
    history: list[dict[str, Any]],
    user_id: int,
    system_prompt: str | None = None,
    agent: str = "",
) -> ChatResult:
    """Run the agent loop for one user turn.

    `history` is the prior conversation as already-shaped OpenAI messages
    (role + content). `user_id` is forwarded to destructive tools that need
    to know who staged the action. `system_prompt` overrides the default when
    an agent persona should be injected instead. `agent` is the name of the
    active specialist agent (or empty string for general chat) — recorded in
    the audit log so you can filter by agent.
    """
    invocations: list[ToolInvocation] = []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
        *history,
    ]
    tools_payload = registry.all_openai_tools()

    for turn in range(MAX_TOOL_TURNS):  # cap imported from config
        # Debug: full messages array sent to the API this turn.
        if DEBUG_LOG_MESSAGES:
            debug_log.log(
                "debug_messages",
                agent=agent or "general",
                turn=turn,
                messages=messages,
            )

        audit.log_event(
            "llm_call",
            user_id=user_id,
            agent=agent or "general",
            turn=turn,
            model=DEEPSEEK_MODEL,
            temperature=AGENT_TEMPERATURE,
            num_messages=len(messages),
            num_tools=len(tools_payload),
            tool_choice="auto" if tools_payload else None,
            # Include the system prompt on turn 0 so the audit log shows exactly
            # what persona + context was injected. Omitted on subsequent turns
            # because it's unchanged and would bloat every record.
            system_prompt_preview=(system_prompt or SYSTEM_PROMPT)[:600] if turn == 0 else None,
        )
        t_llm = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                tools=tools_payload if tools_payload else None,
                tool_choice="auto" if tools_payload else None,
                temperature=AGENT_TEMPERATURE,
            )
        except OpenAIError as e:
            log.exception("DeepSeek API error on turn %s", turn)
            audit.log_event(
                "llm_error", user_id=user_id, turn=turn,
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.monotonic() - t_llm) * 1000.0,
            )
            return ChatResult(text=f"[LLM error: {e}]", invocations=invocations)
        _resp_text = resp.choices[0].message.content or ""
        _usage = getattr(resp, "usage", None)
        audit.log_event(
            "llm_response",
            user_id=user_id,
            agent=agent or "general",
            turn=turn,
            duration_ms=round((time.monotonic() - t_llm) * 1000.0, 1),
            has_tool_calls=bool(resp.choices[0].message.tool_calls),
            # Include the actual text so intermediate turns are readable.
            # Truncated to 500 chars — full text is in the assistant_reply event.
            text=_resp_text[:500] + ("..." if len(_resp_text) > 500 else ""),
            text_len=len(_resp_text),
            usage={
                "prompt":     _usage.prompt_tokens     if _usage else None,
                "completion": _usage.completion_tokens if _usage else None,
                "total":      _usage.total_tokens      if _usage else None,
                "reasoning":  getattr(_usage, "completion_tokens_details", None)
                              and getattr(_usage.completion_tokens_details, "reasoning_tokens", None),
            },
        )

        msg = resp.choices[0].message

        # --- Debug events (only when LOG_LEVEL=DEBUG + individual flag set) ---

        # Full response text, no truncation (audit log caps at 500 chars).
        if DEBUG_LOG_LLM_RESPONSE:
            debug_log.log(
                "debug_llm_response",
                agent=agent or "general",
                turn=turn,
                text=_resp_text,
            )

        # reasoning_content: internal thinking from DeepSeek reasoning models.
        # Can be very long; only log when explicitly requested.
        if DEBUG_LOG_REASONING:
            _reasoning = getattr(msg, "reasoning_content", None)
            if _reasoning:
                debug_log.log(
                    "debug_reasoning",
                    agent=agent or "general",
                    turn=turn,
                    reasoning_content=_reasoning,
                )

        # finish_reason: why the model stopped this turn.
        # "stop" = natural end, "tool_calls" = called a tool, "length" = hit token cap.
        if DEBUG_LOG_FINISH_REASON:
            debug_log.log(
                "debug_finish_reason",
                agent=agent or "general",
                turn=turn,
                finish_reason=resp.choices[0].finish_reason,
            )

        # model_dump() converts the SDK's Message object to a plain dict while
        # preserving ALL fields, including non-standard ones. This matters for
        # DeepSeek thinking models (deepseek-reasoner, deepseek-v4-flash) which
        # return a `reasoning_content` field that the API REQUIRES be echoed
        # back verbatim on the next request — omitting it causes HTTP 400.
        assistant_entry: dict[str, Any] = msg.model_dump(exclude_none=True)

        # Strip fields the API rejects when they appear in a message being sent
        # back. These are response-only fields that have no meaning as input:
        #   refusal       — model's refusal reason (read-only metadata)
        #   annotations   — citation spans (read-only metadata)
        #   audio         — audio response content (not used here)
        #   function_call — deprecated predecessor to tool_calls; causes errors
        for unwanted in ("refusal", "annotations", "audio", "function_call"):
            assistant_entry.pop(unwanted, None)
        # Belt-and-braces: ensure tool_calls is in the wire format the API wants.
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        # `content` must be present even if empty (API requires the field).
        assistant_entry.setdefault("content", msg.content or "")
        messages.append(assistant_entry)

        if not msg.tool_calls:
            return ChatResult(
                text=msg.content or "[empty response]",
                invocations=invocations,
            )

        # Execute each tool call
        for tc in msg.tool_calls:
            name = tc.function.name
            t0 = time.monotonic()
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                args = {}
                result: Any = {"error": f"invalid arguments JSON: {e}"}
                ok = False
            else:
                # Inject user_id for destructive tools that need it. Always
                # override — the LLM never sees this param (it's filtered out
                # by registry.Tool.to_openai), so anything the model put here
                # is a hallucination and must be replaced.
                if name in registry.REGISTRY and registry.REGISTRY[name].destructive:
                    args["user_id"] = user_id
                try:
                    result = registry.dispatch(name, args)
                    ok = not (isinstance(result, dict) and "error" in result)
                except Exception as e:  # noqa: BLE001 — surface any tool error to the LLM
                    log.exception("Tool %s raised %s", name, type(e).__name__)
                    result = {"error": f"{type(e).__name__}: {e}"}
                    ok = False

            duration_ms = (time.monotonic() - t0) * 1000.0
            invocations.append(ToolInvocation(
                name=name, arguments=args, result=result,
                duration_ms=duration_ms, ok=ok,
            ))

            # Audit: full args + result (truncated by audit._truncate)
            audit.log_event(
                "tool_call",
                user_id=user_id,
                turn=turn,
                name=name,
                arguments=args,
                result=result,
                ok=ok,
                duration_ms=duration_ms,
            )

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    log.warning("Hit MAX_TOOL_TURNS=%s without a final answer", MAX_TOOL_TURNS)
    return ChatResult(
        text="[stopped: too many tool turns. Try a more specific question.]",
        invocations=invocations,
    )


# ---------------------------------------------------------------------------
# Debug header formatting
# ---------------------------------------------------------------------------

def _compact_arg(v: Any, max_len: int = 40) -> str:
    """Single-line, length-capped repr of a tool argument value."""
    s = json.dumps(v, default=str, ensure_ascii=False)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _summarise_result(r: Any) -> str:
    """One-line gist of a tool result for the debug header."""
    if not isinstance(r, dict):
        s = str(r)
        return s[:60] + ("..." if len(s) > 60 else "")
    if r.get("staged"):
        return f"staged {r.get('action_id', '?')}"
    if "error" in r:
        e = str(r["error"])
        return f"error: {e[:80]}" + ("..." if len(e) > 80 else "")
    for key in ("results", "messages", "channels"):
        v = r.get(key)
        if isinstance(v, list):
            return f"{len(v)} {key[:-1] if key.endswith('s') else key}(s)"
    if r.get("ok"):
        # Surface useful ids when present
        for key in ("id", "draft_id", "message_id", "ts", "url"):
            if key in r:
                return f"ok ({key}={_compact_arg(r[key], 30)})"
        return "ok"
    return _compact_arg(r, 60)


def _format_invocation(inv: ToolInvocation) -> str:
    icon = "🔧" if inv.ok else "❌"
    args_str = ", ".join(
        f"{k}={_compact_arg(v)}"
        for k, v in inv.arguments.items()
        if k != "user_id"  # injected by the loop, not user-relevant
    )
    res = _summarise_result(inv.result)
    return f"{icon} {inv.name}({args_str}) → {res} [{inv.duration_ms:.0f}ms]"


def format_debug_header(invocations: list[ToolInvocation], max_chars: int = 1200) -> str:
    """Format a list of ToolInvocations as a multi-line header for Telegram.

    Capped at `max_chars` so it can't blow past Telegram's 4096-char message
    limit even with very chatty tool calls.
    """
    if not invocations:
        return ""
    out = "\n".join(_format_invocation(inv) for inv in invocations)
    if len(out) > max_chars:
        out = out[: max_chars - 3] + "..."
    return out


def format_staged_footer(
    invocations: list[ToolInvocation],
    preview_chars: int = 400,
) -> str:
    """Return a structural footer listing any actions staged this turn.

    Unlike the debug header, this is shown unconditionally — it's the safety
    net for when the LLM forgets to mention /confirm. Each staged action gets
    its action_id, the preview the tool produced, and the confirm/cancel
    commands, regardless of what the assistant text says.
    """
    staged: list[tuple[str, str]] = []
    for inv in invocations:
        if isinstance(inv.result, dict) and inv.result.get("staged"):
            aid = str(inv.result.get("action_id") or "?")
            preview = str(inv.result.get("preview") or "(no preview)")
            staged.append((aid, preview))
    if not staged:
        return ""

    parts = ["⏳ STAGED — none of these have happened yet:"]
    for aid, preview in staged:
        if len(preview) > preview_chars:
            preview = preview[: preview_chars - 3] + "..."
        parts.append("")
        parts.append(f"[id: {aid}]")
        parts.append(preview)
        parts.append(f"→ /confirm {aid}   |   /cancel {aid}")
    return "\n".join(parts)
