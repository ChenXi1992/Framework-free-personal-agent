# Architecture Guide

A walkthrough of the codebase for someone reading it for the first time.
Start here before reading any source file.

---

## What this is

A personal Telegram bot backed by DeepSeek's LLM. You send it a message;
it decides which specialist agent (workout / lifestyle / growth / career / dutch / general)
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
    ├─ Plain-text shortcuts: fuzzy "confirm*" / "cancel" → cmd_confirm / cmd_cancel
    │   (handles typos like "confirn", emoji like "confirm :)", bare "yes")
    ├─ log_message() → SQLite messages table
    └─ agent_handle(text, user_id, history)
              │
              ▼
    app/agents/dispatch.py  handle()
    ├─ _last_used_agent()  → most recent agent_conversations row (within 1 hour)
    ├─ _router.route(message, recent_context=hint)
    │         ├─ discovery.build_agents_block() → injects {{AGENTS}} into router.md
    │         └─ One LLM call with router.md prompt → {type, agent, category, summary}
    ├─ Sticky fallback: message ≤5 words + agent=none + recent agent (<1hr) → inherit
    │
    ├─ [if type == "note"]
    │   └─ use router-generated summary + category → INSERT into notes table → route to agent for response (or ack)
    │
    ├─ [if type == "diary"]
    │   └─ route to relevant agent (default: growth) with instruction to:
    │       1. call diary_add() → write to diary.md
    │       2. call note_add() → make entry visible in future context
    │       3. call todo_add / file_append for any actionable items
    │
    ├─ [if agent == "none"]
    │   └─ chat(global_history, system_prompt=_build_general_prompt())
    │
    └─ [if agent == "workout" | "lifestyle" | "growth" | "career" | "dutch"]
        ├─ _build_persona(agent, category)
        │   ├─ load data/prompts/<agent>.md
        │   └─ _build_agent_context() → today's date + tool rules + recent notes
        │       + personal_profile.md (always) + goals/<agent>.md (if exists)
        │       (conversation history is NOT duplicated here — it's in the messages array)
        ├─ agent_message_history() → recent turns from agent_conversations table
        └─ chat([agent_history…, new_message], system_prompt=persona)
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
    ├─ Hallucination guard: if reply claims a write but no write tool was called → append warning
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
| `handle_msg()` | Every plain-text message lands here. Checks allow-list, handles confirm/cancel shortcuts, calls `agent_handle()`, runs hallucination guard, assembles reply |
| `cmd_confirm()` | `/confirm [id]` — executes the most recent (or specified) pending action |
| `cmd_cancel()` | `/cancel [id]` — discards a pending action |
| `cmd_reset()` | Inserts a sentinel into `messages` so `_trim_after_reset()` drops prior history |
| `_assemble_reply()` | Combines optional debug header + LLM text + staged-action footer |
| `_trim_after_reset()` | Drops conversation history up to the last `/reset` sentinel |
| `_telegram_error_handler()` | Logs Telegram/network errors and notifies the user to retry |
| `EXECUTORS` dict | Maps `tool_name → execute_confirmed()` for every destructive tool |

**Confirm shortcut normalisation:** the raw message is stripped of all non-letter characters (removes emoji, punctuation, trailing smileys) before matching. `"confirm :)"`, `"confirn"`, `"confim"` all trigger confirmation.

**Hallucination guard:** after every agent reply, checks if the text contains a first-person write claim (regex: `I've saved`, `I added`, `I logged`, etc.) but no write tool was actually called. Appends a visible warning to the reply if triggered. Prevents the agent from silently lying about having stored data.

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

**`app/agents/discovery.py`**
Dynamic agent registry — the single source of truth for which agents exist.

| Function | What it does |
|---|---|
| `get_agents()` | Scans `data/prompts/` for `.md` files, excludes `router.md`. Returns a sorted tuple of agent names. Cached per process — restart to pick up new agents |
| `get_routing_description(agent)` | Reads the `## Routing` section from an agent's prompt file. Used to build the agents block injected into `router.md` at runtime |
| `build_agents_block()` | Generates the markdown bullet list injected into `router.md`'s `{{AGENTS}}` placeholder. Always appends `none` as the final option |

**`app/agents/router.py`**
Two concerns: (1) classify what kind of message this is, (2) assemble the context block injected into the agent's system prompt.

| Function | What it does |
|---|---|
| `route(message, recent_context)` | One LLM call using `data/prompts/router.md` → returns `{type, agent, category, summary}`. For notes, `summary` is a one-liner stored directly in the notes table — no separate classifier call needed. `recent_context` is a hint like "previous conversation was with the workout agent" so short follow-ups resolve correctly |
| `_load_prompt(name)` | Reads `data/prompts/<name>.md`. For the router prompt, replaces `{{AGENTS}}` with the dynamically discovered agent list before returning |
| `_build_agent_context(agent, category)` | Assembles the context block injected into the system prompt: today's date → tool rules → recent notes → `personal_profile.md` (always) → `goals/<agent>.md` (if it exists). Uses `get_agents()` to determine valid note categories dynamically. **Does NOT include conversation history** — passed as actual OpenAI messages instead |
| `agent_message_history(agent)` | Returns recent turns from `agent_conversations` as OpenAI-format messages |
| `store_turn(agent, role, content)` | Appends one turn to `agent_conversations` table |
| `_llm_json(system, user)` | Helper that calls the LLM expecting JSON output. Retries on empty or unparseable responses. Handles code fences, preamble text, and truncated JSON |

**`app/agents/dispatch.py`**
Orchestrates the full routing decision and calls `llm.chat()`.

| Function | What it does |
|---|---|
| `handle()` | Main entry point from `main.py`. Looks up last agent (within 1 hour), builds context hint, calls router, applies sticky-agent fallback, routes to general or specialist |
| `_last_used_agent()` | Queries `agent_conversations` for the most recent agent name if it was within the last hour. Returns `None` if stale — prevents a morning workout conversation from silently hijacking an unrelated evening message |
| `_build_general_prompt()` | System prompt for non-agent messages. Injects today's date + live tool list |
| `_build_persona(agent, category)` | Combines the agent's `.md` persona file with the dynamic context block |

**Sticky-agent fallback:** If the router returns `agent=none` AND the message is ≤ 5 words AND there was a specialist agent conversation **within the last hour**, that agent is inherited. Handles replies like "Yes", "Sounds good", "Let's do it." The 1-hour window prevents stale inheritance across unrelated conversations.

---

### Specialist agents

Agents are discovered automatically from `data/prompts/` — any `.md` file that is not `router.md` is treated as a specialist agent. The current set:

| Agent | Persona file | Scope |
|---|---|---|
| `workout` | `workout.md` | Physical training, running, sport, body, recovery. Access to health data and Google Calendar |
| `lifestyle` | `lifestyle.md` | Observable daily behaviours: diet, sleep schedule, screen time, gaming, routines. **Not** feelings — those go to growth |
| `growth` | `growth.md` | Emotions, mood, self-reflection, personal development, relationships, psychology, mindset. Default handler for diary entries (configurable via `DIARY_DEFAULT_AGENT`) |
| `career` | `career.md` | Work, meetings, professional development, job performance, career goals |
| `dutch` | `dutch.md` | Dutch language teaching: grammar, vocabulary, correction with explanations. Tracks recurring pain points via `note_add(category="dutch")` |

All agents receive `personal_profile.md` and `goals/<agent>.md` automatically in their system prompt — no tool call needed for these. When updating goals, agents call `file_edit()` or `file_append()` on `goals/<agent>.md` immediately — no describe-then-skip.

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
On-demand loading of user context files. `personal_profile.md` and `goals/<agent>.md` are auto-injected by `_build_agent_context()` — everything else is loaded on demand via these tools.

| Function | What it does |
|---|---|
| `context_list()` | Scans `data/*.md` and returns the list of available filenames (excluding internal files like `tool_needs.md`) |
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
| `app/tools/files.py` | `file_read`, `file_list`, `file_write` (staged), `file_edit` (staged), `file_append` (staged). All paths resolved relative to `FILES_ROOT` (defaults to `data/`). Returns `{"staged": True, "action_id": …}` dict so the staged footer always fires |
| `app/tools/notes.py` | `note_add`, `notes_recent`, `notes_search`, `conversation_add`, `feedback_add`, `feedback_recent` |
| `app/tools/todo.py` | `todo_add`, `todo_list`, `todo_done`, `todo_delete`. Backed by `data/todo.md` |
| `app/tools/diary.py` | `diary_add` (appends dated entry to `data/diary.md`), `diary_recent` (reads back by category) |
| `app/tools/health.py` | `health_daily_summary`, `health_sport_breakdown`, `health_heart_rate`, `health_workout_sessions` (queries health data in SQLite) |
| `app/tools/prompts.py` | `prompt_read`, `prompt_propose` (staged), `tool_need` (logs feature requests to `data/tool_needs.md`) |

---

### Data layer

**`app/db.py`**
All SQLite. WAL mode + autocommit so concurrent reads/writes don't block.

| Table | What it stores |
|---|---|
| `messages` | Every user + assistant turn. Used for global conversation history (general LLM) |
| `notes` | Structured notes logged by the agent: category (`workout` / `lifestyle` / `growth` / `career` / `dutch` / `uncategorized`), summary, raw text |
| `agent_conversations` | Per-agent conversation memory (separate from global `messages`). Filtered by `agent` column. Specialist agents receive this as properly-formatted OpenAI messages — not as a text digest in the system prompt |
| `feedback` | Agent feedback entries (positive / negative / neutral) |
| `pending_actions` | Staged destructive actions waiting for user confirmation |
| `health_daily` | Daily health summary (steps, calories, HR, sleep) |
| `sport_daily` | Daily breakdown by sport type |
| `heart_rate` | Time-series heart rate readings |
| `sport_minute` | Per-minute activity breakdown |

Every table has `ts INTEGER` (unix epoch, fast for sorting) and a human-readable `created_at TEXT` column. Health tables also have `date_iso TEXT` / `datetime_utc TEXT`.

**Data files in `data/`**

| File | What it is |
|---|---|
| `goals/` | Per-domain goals folder (gitignored). Contains `workout.md`, `growth.md`, `lifestyle.md`, `career.md`, `dutch.md`. Each agent owns and updates its own file. Auto-injected into the agent's system prompt by `_build_agent_context()` |
| `personal_profile.md` | Xi's background (age, fitness, career history, habits). Auto-injected into every agent's system prompt |
| `diary.md` | Journal entries with dated headers, written by the growth agent via `diary_add` |
| `todo.md` | Todo list, managed by `todo_add` / `todo_done` / `todo_delete` |
| `prompts/` | Agent persona files (editable). One `.md` per agent |

**`app/audit.py`**
Append-only JSONL audit log at `data/agent.log.json`. One pretty-printed JSON block per event, blank line between records. Every LLM call, tool call, confirm, cancel, and user message is recorded.

---

## Key design decisions

**`user_id` hidden from LLM schemas**
If the model sees `user_id: integer` in a tool's parameter list, it hallucinates a value. The staged action then belongs to a phantom user and `/confirm` fails the ownership check. `Tool.to_openai()` strips it from the schema; the agent loop injects the real value server-side.

**Staged actions, not immediate execution**
Destructive tools (`send_email`, `create_event`, `archive_page`, `file_write`, `file_edit`, `file_append`) never execute when called by the LLM. They write to `pending_actions` and return a `{"staged": True, "action_id": …, "preview": …}` dict. The staged footer in the Telegram reply is built from this dict — so the footer always fires for any staged tool, regardless of what the LLM wrote in its text reply.

**Agent memory is separate from global history**
`agent_conversations` is a per-agent table. When the workout agent runs, it sees its own history passed as actual OpenAI messages. When the career agent runs, it sees its own. The agents don't cross-contaminate each other's memory. Global `messages` captures everything for general LLM calls, but each specialist agent only sees its own thread.

**No history duplication in the system prompt**
`_build_agent_context()` used to include a truncated text digest of the conversation history in the system prompt. This was removed — the actual `agent_message_history()` messages are already passed as the messages array. Double-injecting the same data as both messages and a text block wastes tokens and can confuse the model.

**Stateless router + sticky fallback with recency window**
The router LLM call is stateless — it sees only the current message. For short follow-ups ("Yes", "OK", "Let's do it"), the router can't tell which agent to use. Two compensations: (1) we append the last agent as a context hint to the router message; (2) if the router still returns `none` and the message is ≤ 5 words, we inherit the last agent — but **only if that agent conversation happened within the last hour**. Without this window, a morning workout conversation would silently drag in an unrelated evening message.

**Core context is auto-injected; everything else is on demand**
`personal_profile.md` and `goals/<agent>.md` are automatically included in every specialist agent's system prompt by `_build_agent_context()` — agents always start with Xi's profile and their domain goals, without a tool call. All other `data/*.md` files (plans, health summaries, etc.) are still loaded on demand: the agent calls `context_list()` to see what exists and `context_load(filename)` for what it needs. This keeps token usage low and irrelevant context out.

**Diary entries need a `note_add` call too**
`diary_add` writes to `diary.md` (narrative record). But the notes context block is built from the `notes` table — not from diary.md. If the agent only calls `diary_add`, the entry is invisible in future calls. The diary dispatch instruction explicitly asks agents to also call `note_add(category=…)` so the entry surfaces in context on subsequent turns.

**Hallucination guard**
After every agent reply, `main.py` runs a regex check: does the reply text contain a first-person write claim (`I've saved`, `I added`, `I logged`, etc.) while no write tool was called? If so, it appends `"⚠️ Heads up: no data was actually saved"` to the reply. This catches the failure mode where the LLM describes having stored something without ever calling the tool.

**DeepSeek reasoning_content must be echoed back**
Thinking models (deepseek-reasoner, deepseek-v4-flash) return a `reasoning_content` field. The API requires you to include it in subsequent messages or you get HTTP 400. Using `msg.model_dump(exclude_none=True)` preserves all extra fields automatically.

---

## How to extend

### Add a new tool

1. Create (or open) a file in `app/tools/`.
2. Decorate a function with `@tool(description=..., parameters=..., destructive=False)`.
3. For destructive tools: call `stage_action()` and return `{"staged": True, "action_id": …, "preview": …}`. Add an `execute_confirmed()` function and register it in `EXECUTORS` in `main.py`.
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

**No code changes required.** Drop two files and restart:

1. **`data/prompts/<name>.md`** — the agent's persona. Follow the standard structure:
   - Opening line: `You are Xi's personal <X>. You are <traits>.`
   - `## Routing` — one-line description used by the router to know when to route here
   - `## Your role`, `## Scope`, `## Style`, `## What you know`, `## When to call note_add`, `## Updating goals/<name>.md`, `## Self-improvement`
   - `personal_profile.md` and `goals/<name>.md` are auto-injected — do not instruct the agent to call `context_load` for these

2. **`data/goals/<name>.md`** *(optional)* — the agent's goals file. Created empty is fine; the agent will populate it when Xi sets goals in that domain.

On restart, `discovery.get_agents()` picks up the new file, `router.md`'s `{{AGENTS}}` block is regenerated, tool enums update automatically, and the DB accepts the new category without migration.

### Add a persistent context file

Drop a `.md` file into `data/`. It immediately appears in `context_list()`. The agent will find and load it when relevant. No code change needed.

- No prefix (e.g. `personal_profile.md`) → available to all agents
- Agent prefix (e.g. `workout_summer_plan.md`) → `context_list()` still shows it, but the naming convention signals which agent should care about it

### Add a new config value

Add it to `app/config.py` with `os.environ.get("MY_VAR", default)` for optional or `_required("MY_VAR")` for mandatory. Document it in `.env.example`.
