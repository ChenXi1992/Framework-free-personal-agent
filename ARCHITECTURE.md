# Architecture Guide

A walkthrough of the codebase for someone reading it for the first time.
Start here before reading any source file.

---

## What this is

A personal Telegram bot backed by DeepSeek's LLM. You send it a message;
it decides which specialist agent (workout / career / lifestyle / general)
should handle it, runs a tool-calling loop against your real data (Gmail,
Notion, Calendar, local files, SQLite health data), and replies.

No framework — just Python, the OpenAI SDK pointed at DeepSeek, and
python-telegram-bot for the Telegram layer.

---

## A message, step by step

```
You → Telegram
          │
          ▼
    app/main.py  handle_msg()
    ├─ Allow-list check (TELEGRAM_ALLOWED_USER_IDS)
    ├─ Plain-text shortcuts: "confirm" / "cancel" → cmd_confirm / cmd_cancel
    ├─ log_message() → SQLite messages table
    └─ agent_handle(text, user_id, history)
              │
              ▼
    app/agents/dispatch.py  handle()
    ├─ _last_used_agent()  → look up most recent agent_conversations row
    ├─ _router.route(message, recent_context=hint)
    │         └─ One LLM call with router.md prompt → {type, agent, category}
    ├─ Sticky fallback: short message + recent agent → inherit that agent
    │
    ├─ [if type == "note"]
    │   └─ classify_note() → INSERT into notes table → early return
    │
    ├─ [if agent == "none"]
    │   └─ chat(global_history, system_prompt=_build_general_prompt())
    │
    └─ [if agent == "workout" | "career" | "lifestyle"]
        ├─ _build_persona(agent, category)
        │   ├─ load data/prompts/<agent>.md
        │   └─ _build_agent_context() → today's date + tool rules + recent notes
        │                                + agent conversation history
        ├─ agent_message_history() → recent turns from agent_conversations table
        └─ chat(agent_history + new_message, system_prompt=persona)
                  │
                  ▼
        app/llm.py  chat()
        Loop up to MAX_TOOL_TURNS (default 10, configurable via .env):
        ├─ Audit: llm_call event
        ├─ DeepSeek API call (OpenAI-compatible)
        ├─ Audit: llm_response event (with first 500 chars of text)
        ├─ [if no tool calls] → return ChatResult(text, invocations)
        └─ [if tool calls]
            ├─ Inject user_id into destructive tools
            ├─ registry.dispatch(name, args) → call the Python function
            ├─ Audit: tool_call event
            └─ Append tool result to messages → loop
              │
              ▼
    back in dispatch.py
    ├─ store_turn(agent, "user", message)   → agent_conversations table
    └─ store_turn(agent, "assistant", text) → agent_conversations table
              │
              ▼
    back in main.py
    ├─ log_message(assistant text) → messages table
    ├─ Audit: assistant_reply event
    ├─ _assemble_reply() → debug header (if LOG_LEVEL=DEBUG) + LLM text + staged footer
    └─ Telegram reply
```

---

## File map

### Entry point

**`app/main.py`**
The Telegram bot. Long-polling only — no inbound ports needed.

| Function | What it does |
|---|---|
| `main()` | Initialises DB, registers handlers, starts polling loop |
| `handle_msg()` | Every plain-text message lands here. Checks allow-list, handles confirm/cancel shortcuts, calls `agent_handle()`, assembles reply |
| `cmd_confirm()` | `/confirm [id]` — executes the most recent (or specified) pending action |
| `cmd_cancel()` | `/cancel [id]` — discards a pending action |
| `cmd_reset()` | Inserts a sentinel into `messages` so `_trim_after_reset()` drops prior history |
| `_assemble_reply()` | Combines optional debug header + LLM text + staged-action footer |
| `_trim_after_reset()` | Drops conversation history up to the last `/reset` sentinel |
| `EXECUTORS` dict | Maps `tool_name → execute_confirmed()` for every destructive tool |

---

### LLM loop

**`app/llm.py`**
Everything to do with calling DeepSeek and running tool calls.

| Function / class | What it does |
|---|---|
| `chat()` | The agent loop. Takes history + system prompt, calls the API, dispatches tool calls, loops until no more tool calls or MAX_TOOL_TURNS hit. Returns `ChatResult` |
| `ChatResult` | Dataclass: `text` (final LLM reply) + `invocations` (every tool call that happened) |
| `ToolInvocation` | Dataclass: one tool call — name, args, result, duration, ok/fail |
| `format_debug_header()` | Formats tool invocations as a compact multi-line header for Telegram (shown when LOG_LEVEL=DEBUG) |
| `format_staged_footer()` | Builds the unconditional "⏳ STAGED" footer listing any pending actions this turn |

Key design: `user_id` is injected server-side for destructive tools — the LLM never sees this parameter so it can't hallucinate a wrong one.

---

### Routing & agent context

**`app/agents/router.py`**
Two concerns: (1) classify what kind of message this is, (2) assemble the context block that gets injected into the agent's system prompt.

| Function | What it does |
|---|---|
| `route(message, recent_context)` | One LLM call using `data/prompts/router.md` → returns `{type, agent, category}`. `recent_context` is a hint like "previous conversation was with the workout agent" so short follow-ups resolve correctly |
| `classify_note(text)` | Separate LLM call using `data/prompts/classifier.md` → `{category, summary}` for note intake |
| `_build_agent_context(agent, category)` | Assembles the context block: today's date → tool rules → recent notes → agent conversation history. Deliberately does NOT auto-inject files — agents use `context_list` / `context_load` tools on demand |
| `agent_message_history(agent)` | Returns recent turns from `agent_conversations` as OpenAI-format messages |
| `store_turn(agent, role, content)` | Appends one turn to `agent_conversations` table |
| `_llm_json(system, user)` | Helper that calls the LLM and parses JSON from the response (handles DeepSeek markdown code fences) |

**`app/agents/dispatch.py`**
Orchestrates the full routing decision and calls `llm.chat()`.

| Function | What it does |
|---|---|
| `handle()` | Main entry point from `main.py`. Looks up last agent, builds context hint, calls router, applies sticky-agent fallback, routes to general or specialist |
| `_last_used_agent()` | Queries `agent_conversations` for the most recent agent name — used for sticky routing |
| `_build_general_prompt()` | System prompt for non-agent messages. Injects today's date + live tool list |
| `_build_persona(agent, category)` | Combines the agent's `.md` persona file with the dynamic context block |

**Sticky-agent fallback:** If the router returns `agent=none` AND the message is ≤ 5 words AND there's a recent agent conversation, the last agent is inherited. Handles replies like "Yes", "Sounds good", "Let's do it."

---

### Tool system

**`app/tools/registry.py`**
The tool registration and dispatch layer.

| Component | What it does |
|---|---|
| `@tool(description, parameters, destructive)` | Decorator. Registers a Python function as an LLM-callable tool into `REGISTRY` |
| `Tool.to_openai()` | Converts a registered tool to OpenAI's `{"type":"function",...}` format. Strips `user_id` from the schema so the LLM never sees it |
| `all_openai_tools()` | Returns the full tools list for the `tools=` argument in every LLM call |
| `dispatch(name, args)` | Looks up the function in REGISTRY and calls it |

**`app/tools/pending.py`**
The staged-action queue for destructive operations.

| Function | What it does |
|---|---|
| `stage_action(user_id, tool_name, arguments, preview)` | Writes a row to `pending_actions` table with status="pending". Returns the 8-char action ID |
| `get(action_id)` | Fetch a pending action by ID |
| `mark(action_id, status, result)` | Update status to "confirmed" / "cancelled" / "executed" / "failed" |
| `latest_pending_for(user_id)` | Returns the most recent pending action — used when user types bare "confirm" with no ID |

**`app/tools/context.py`**
On-demand loading of user context files. Agents call these instead of having files auto-injected.

| Function | What it does |
|---|---|
| `context_list()` | Scans `data/*.md` and returns the list of available filenames |
| `context_load(filename)` | Reads and returns the content of a specific file in `data/` |

---

### Integrations (all optional)

Each integration follows the same pattern:
- Read tools: execute immediately, return data
- Write/destructive tools: call `stage_action()`, return a preview + action ID
- `execute_confirmed(tool_name, arguments)`: called by `main.py`'s `/confirm` handler

| File | Tools |
|---|---|
| `app/tools/gmail.py` | `gmail_search`, `gmail_get_message`, `gmail_create_draft`, `gmail_trash_message`, `gmail_send_message` (staged) |
| `app/tools/calendar.py` | `calendar_list_events`, `calendar_find_free_slots`, `calendar_create_event` (staged), `calendar_delete_event` (staged). Supports `all_day=true` with `YYYY-MM-DD` dates |
| `app/tools/notion.py` | `notion_search`, `notion_get_page`, `notion_append_paragraph`, `notion_create_page`, `notion_archive_page` (staged) |
| `app/tools/files.py` | `file_read`, `file_list`, `file_write` (staged), `file_edit` (staged), `file_append` (staged). All paths resolved relative to `FILES_ROOT` (defaults to `data/`) |
| `app/tools/notes.py` | `note_add`, `notes_recent`, `notes_search`, `conversation_add`, `feedback_add` |
| `app/tools/health.py` | `health_daily_summary`, `health_sport_breakdown`, `health_heart_rate`, `health_workout_sessions` (queries Huawei Health data in SQLite) |
| `app/tools/prompts.py` | `prompt_read`, `prompt_propose` (staged), `tool_need` (logs feature requests to `data/tool_needs.md`) |

---

### Data layer

**`app/db.py`**
All SQLite. WAL mode + autocommit so concurrent reads/writes don't block.

| Table | What it stores |
|---|---|
| `messages` | Every user + assistant turn. Used for global conversation history |
| `notes` | Structured notes logged by the agent: category (workout/lifestyle/career), summary, raw text |
| `agent_conversations` | Per-agent conversation memory (separate from global `messages`). Filtered by `agent` column |
| `feedback` | Agent feedback entries |
| `pending_actions` | Staged destructive actions waiting for user confirmation |
| `health_daily` | Daily Huawei Health summary (steps, calories, HR, sleep) |
| `sport_daily` | Daily breakdown by sport type |
| `heart_rate` | Time-series heart rate readings |
| `sport_minute` | Per-minute activity breakdown |

Every table has `ts INTEGER` (unix epoch, fast for sorting) and a human-readable `created_at TEXT` / `date_iso TEXT` / `datetime_utc TEXT` column for readability in DB Browser.

**`app/audit.py`**
Append-only JSONL audit log at `data/agent.log.json`. One pretty-printed JSON block per event, blank line between records. Every LLM call, tool call, confirm, cancel, and user message is recorded. Useful for debugging and replaying what the agent actually did.

---

## Key design decisions

**`user_id` hidden from LLM schemas**
If the model sees `user_id: integer` in a tool's parameter list, it hallucinates a value. The staged action then belongs to a phantom user and `/confirm` fails the ownership check. `Tool.to_openai()` strips it from the schema; the agent loop injects the real value server-side.

**Staged actions, not immediate execution**
Destructive tools (`send_email`, `create_event`, `archive_page`, `file_write`) never execute when called by the LLM. They write to `pending_actions` and return a preview. The user confirms or cancels. This keeps the LLM's capability and your control simultaneously.

**Agent memory is separate from global history**
`agent_conversations` is a per-agent table. When the workout agent runs, it sees its own history. When the career agent runs, it sees its own. The agents don't cross-contaminate each other's memory. Global `messages` captures everything for the Telegram UX, but the LLM only sees agent-specific history.

**Stateless router + sticky fallback**
The router LLM call is stateless — it sees only the current message. For short follow-ups ("Yes", "OK", "Let's do it"), the router can't tell which agent to use. Two compensations: (1) we append the last agent as a context hint to the router message; (2) if the router still returns `none` and the message is ≤ 5 words, we inherit the last agent.

**Context files are loaded on demand, not injected**
`data/*.md` files (goals, personal profile, plans) are NOT dumped into every system prompt. The agent calls `context_list()` to see what exists and `context_load(filename)` for what it actually needs. This keeps token usage down and irrelevant context out.

**DeepSeek reasoning_content must be echoed back**
Thinking models (deepseek-reasoner, deepseek-v4-flash) return a `reasoning_content` field. The API requires you to include it in subsequent messages or you get HTTP 400. Using `msg.model_dump(exclude_none=True)` preserves all extra fields automatically.

---

## How to extend

### Add a new tool

1. Create (or open) a file in `app/tools/`.
2. Decorate a function with `@tool(description=..., parameters=..., destructive=False)`.
3. For destructive tools: call `stage_action()` and return the action ID. Add an `execute_confirmed()` function and register it in `EXECUTORS` in `main.py`.
4. The tool is automatically included in every LLM call on next restart — no further wiring needed.

```python
from .registry import tool

@tool(
    description="Get the current price of a stock ticker.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "e.g. 'AAPL'"},
        },
        "required": ["ticker"],
    },
)
def stock_price(ticker: str) -> dict:
    ...
```

### Add a new specialist agent

1. Create `data/prompts/<name>.md` with the agent's persona.
2. Add the agent name to the router prompt (`data/prompts/router.md`) so the classifier knows to route to it.
3. That's it — `dispatch.py` handles any agent name dynamically.

### Add a persistent context file

Drop a `.md` file into `data/`. It immediately appears in `context_list()`. The agent will find and load it when relevant. No code change needed.

- No prefix (e.g. `personal_profile.md`) → available to all agents
- Agent prefix (e.g. `workout_summer_plan.md`) → `context_list()` still shows it, but the naming convention signals which agent should care about it

### Add a new config value

Add it to `app/config.py` with `os.environ.get("MY_VAR", default)` for optional or `_required("MY_VAR")` for mandatory. Document it in `.env.example`.
