#!/usr/bin/env python3
"""Show the prompt + routing flow for recent conversations.

Usage:
    python scripts/show_flow.py              # last 1 conversation
    python scripts/show_flow.py -n 3         # last 3 conversations
    python scripts/show_flow.py --tail       # live tail (Ctrl-C to stop)
    python scripts/show_flow.py --prompts    # include full system prompts

Each conversation block shows:
    user_message → route_decision → dispatch → llm_call (prompt) → tool_calls → reply
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "data" / "agent.log.json"

# Event display order and labels
_EVENT_STYLES = {
    "user_message":   ("👤", "USER"),
    "route_decision": ("🔀", "ROUTE"),
    "note_classified":("📝", "NOTE CLASSIFY"),
    "dispatch":       ("🎯", "DISPATCH"),
    "llm_call":       ("🤖", "LLM CALL"),
    "llm_response":   ("💬", "LLM RESP"),
    "tool_call":      ("🔧", "TOOL"),
    "assistant_reply":("✅", "REPLY"),
}

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_BLUE   = "\033[34m"
_PURPLE = "\033[35m"

_EVENT_COLORS = {
    "user_message":   _BOLD + _CYAN,
    "route_decision": _YELLOW,
    "note_classified":_YELLOW,
    "dispatch":       _BOLD + _YELLOW,
    "llm_call":       _BLUE,
    "llm_response":   _DIM,
    "tool_call":      _GREEN,
    "assistant_reply":_BOLD + _GREEN,
}


def _color(event: str, text: str) -> str:
    return _EVENT_COLORS.get(event, "") + text + _RESET


def _read_events() -> list[dict]:
    """Read all JSON records from the audit log (handles multi-line pretty JSON)."""
    if not LOG_PATH.exists():
        return []
    text = LOG_PATH.read_text(encoding="utf-8")
    events = []
    # Records are separated by blank lines; each record is a complete JSON object.
    depth = 0
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        depth += stripped.count("{") - stripped.count("}")
        buf.append(line)
        if depth == 0 and buf:
            try:
                events.append(json.loads("\n".join(buf)))
            except json.JSONDecodeError:
                pass
            buf = []
    return events


def _group_conversations(events: list[dict]) -> list[list[dict]]:
    """Split events into conversation groups, each starting with user_message."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for e in events:
        if e.get("event") == "user_message" and current:
            groups.append(current)
            current = []
        current.append(e)
    if current:
        groups.append(current)
    return groups


def _fmt_event(e: dict, show_prompts: bool = False) -> str:
    evt = e.get("event", "?")
    icon, label = _EVENT_STYLES.get(evt, ("•", evt.upper()))
    ts = e.get("ts", "")[-12:-4]   # HH:MM:SS.ms portion of ISO timestamp
    header = f"{_DIM}{ts}{_RESET}  {_color(evt, f'{icon} {label}')}"

    lines = [header]

    if evt == "user_message":
        lines.append(f"   {_BOLD}{e.get('text', '')}{_RESET}")

    elif evt == "route_decision":
        r = e.get("result", {})
        hint = " (had context hint)" if e.get("had_context_hint") else ""
        lines.append(
            f"   type={_YELLOW}{r.get('type')}{_RESET}  "
            f"agent={_YELLOW}{r.get('agent')}{_RESET}  "
            f"category={_YELLOW}{r.get('category')}{_RESET}{hint}"
        )
        inp = e.get("input_preview", "")
        if inp:
            lines.append(f"   {_DIM}input: {inp[:100]}{_RESET}")

    elif evt == "dispatch":
        sticky = " ← sticky fallback!" if e.get("sticky_fallback") else ""
        lines.append(
            f"   agent={_BOLD}{_YELLOW}{e.get('agent')}{_RESET}  "
            f"type={e.get('type')}  "
            f"category={e.get('category')}"
            f"{_RED}{sticky}{_RESET}"
        )

    elif evt == "llm_call":
        turn = e.get("turn", 0)
        agent = e.get("agent", "?")
        n_msgs = e.get("num_messages", "?")
        n_tools = e.get("num_tools", "?")
        lines.append(
            f"   turn={turn}  agent={_BLUE}{agent}{_RESET}  "
            f"messages={n_msgs}  tools={n_tools}"
        )
        if turn == 0 and e.get("system_prompt_preview"):
            preview = e["system_prompt_preview"]
            if show_prompts:
                # Full prompt, indented
                for pl in preview.splitlines():
                    lines.append(f"   {_DIM}│ {pl}{_RESET}")
            else:
                # Just the first line — usually the date or persona name
                first = preview.split("\n")[0][:120]
                lines.append(f"   {_DIM}prompt: {first}…{_RESET}")

    elif evt == "llm_response":
        ms = round(e.get("duration_ms", 0))
        usage = e.get("usage") or {}
        total = usage.get("total", "?")
        text_len = e.get("text_len", 0)
        tool_calls = e.get("has_tool_calls", False)
        lines.append(
            f"   {ms}ms  tokens={total}  "
            f"has_tool_calls={tool_calls}  text_len={text_len}"
        )

    elif evt == "tool_call":
        ok = e.get("ok", True)
        icon2 = _GREEN + "✓" + _RESET if ok else _RED + "✗" + _RESET
        name = e.get("name", "?")
        args = e.get("arguments", {})
        # Compact args, skip user_id
        args_str = "  ".join(
            f"{k}={json.dumps(v, ensure_ascii=False)[:40]}"
            for k, v in args.items()
            if k != "user_id"
        )
        ms = round(e.get("duration_ms", 0))
        lines.append(f"   {icon2} {_BOLD}{name}{_RESET}({args_str})  [{ms}ms]")
        if not ok:
            err = (e.get("result") or {}).get("error", "")
            lines.append(f"     {_RED}error: {err}{_RESET}")

    elif evt == "assistant_reply":
        text = e.get("text", "")
        staged = e.get("staged_action_ids", [])
        lines.append(f"   {text[:200]}" + ("…" if len(text) > 200 else ""))
        if staged:
            lines.append(f"   {_YELLOW}staged: {staged}{_RESET}")

    return "\n".join(lines)


def show_conversations(n: int, show_prompts: bool) -> None:
    events = _read_events()
    groups = _group_conversations(events)
    target = groups[-n:] if n > 0 else groups
    for i, group in enumerate(target):
        print(f"\n{'━' * 70}")
        print(f"  Conversation {len(groups) - len(target) + i + 1}")
        print(f"{'━' * 70}")
        for e in group:
            print(_fmt_event(e, show_prompts=show_prompts))
    print()


def tail_log(show_prompts: bool) -> None:
    """Live tail — poll every 0.5s, print new events as they arrive."""
    seen = 0
    print(f"Tailing {LOG_PATH} … (Ctrl-C to stop)\n")
    try:
        while True:
            events = _read_events()
            for e in events[seen:]:
                print(_fmt_event(e, show_prompts=show_prompts))
            seen = len(events)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", type=int, default=1, metavar="N",
                   help="Show last N conversations (default: 1)")
    p.add_argument("--tail", action="store_true",
                   help="Live tail — print new events as they arrive")
    p.add_argument("--prompts", action="store_true",
                   help="Show full system prompt on llm_call turn 0")
    args = p.parse_args()

    if not LOG_PATH.exists():
        print(f"Audit log not found: {LOG_PATH}", file=sys.stderr)
        print("Start the bot and send a message first.", file=sys.stderr)
        sys.exit(1)

    if args.tail:
        tail_log(show_prompts=args.prompts)
    else:
        show_conversations(n=args.n, show_prompts=args.prompts)


if __name__ == "__main__":
    main()
