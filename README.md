# Personal agent - A side project


A personal Telegram-fronted AI assistant. Reads Gmail, Notion, Google Calendar, and local files. Routes messages to specialist agents (workout, lifestyle, growth, career, dutch) and remembers your history. Hand-rolled agent loop on top of DeepSeek's OpenAI-compatible API — no LangChain, no LlamaIndex, no framework.

Designed to run on a local NAS or home server. 

The final goal is to build a personal assistant that store all the information about my daily life ( work, hobbies, lifestyle ), and ultimately makes decisions, provide feedback & automate certain tasks for me. 

Currently: 
- Access to Gmail, Calendar, Notion
- Sub-agent: Career - auto reply email, workout coach, planning agent
- Deepseek api only

Next: 
- Fine-tune the prompt ✅
- Add diary category ✅
- Auto-summary & proactive agent
- Review the memory system 
- Deploy it by FastAPI ( Multi-access channel ) 

---

## What it does

- **Read your stuff.** Search Gmail, read Notion pages, list calendar events, read local text files.
- **Write your stuff.** Append to Notion, compose drafts, create calendar events, edit files — gated behind a two-step confirmation flow for anything destructive.
- **Specialist agents.** Drop a `.md` file into `data/prompts/` and the agent is live on next restart — no code changes. Five built-in agents:
  - **workout** — training coach with access to Google Calendar and health data
  - **lifestyle** — tracks observable behaviours: diet, sleep schedule, screen time, gaming
  - **growth** — inner-life coach for emotions, self-reflection, personal development
  - **career** — professional development, meeting notes, career goals
  - **dutch** — Dutch language teacher with pain-point tracking
- **Note intake.** Anything logged as a note is categorised, stored, and automatically surfaced as context to the relevant agent on future calls.
- **Diary.** Personal journal entries stored in `diary.md` and indexed in the notes table so agents can reference them.
- **Todo list.** `todo_add`, `todo_done`, `todo_list` backed by `data/todo.md`.
- **Goals files.** Goals are split by domain in `data/goals/` (`workout.md`, `growth.md`, `lifestyle.md`, `career.md`, `dutch.md`). Each specialist agent owns and updates its own file via staged file operations.
- **Stays out of your way.** No inbound ports (long polling). No manual confirmation typing — `confirm` works, no slash, no action_id. Typos (`confirn`) and emoji (`confirm :)`) are handled.
- **Audit trail.** JSON log of every user message, LLM call, tool invocation, and confirmation.
- **Hallucination guard.** If the LLM claims to have saved something but called no write tool, a visible warning is appended to the reply.

---

## Architecture

```
Telegram (long polling — no inbound ports)
         │
         ▼
   app/main.py
   - allow-list auth
   - /start /reset /confirm /cancel /tools
   - fuzzy "confirm" shortcut (handles typos, emoji, "yes")
   - hallucination guard (regex check on reply vs tool calls)
   - assembles reply (debug header + LLM text + staged-action footer)
         │
         ▼
   app/agents/dispatch.py         ← Pass 1: classify + route
   - router.route(msg) → {type, agent, category}
   - types: note | diary | chat
   - agents: discovered dynamically from data/prompts/ on startup
   - agents: workout | lifestyle | growth | career | dutch | none
   - note intake → db.notes (auto-stored + optional agent response)
   - diary intake → agent writes diary.md + note_add for context visibility
   - sticky fallback: short message + last agent within 1hr → inherit agent
   - builds persona + context block for specialist agents
         │
         ▼
   app/llm.py                     ← Pass 2: tool-calling loop
   - DeepSeek client (OpenAI-compatible SDK)
   - up to MAX_TOOL_TURNS (default 10) tool turns per user message
   - preserves reasoning_content for DeepSeek thinking models
   - injects user_id into destructive tools server-side
         │
    ┌────┴─────────────────────────────────────┐
    ▼                ▼                         ▼
 app/tools/       app/tools/pending       app/audit.py
 registry.py      - SQLite staging         - JSON log
 - @tool decorator  queue                  - every event
 - hides user_id    - /confirm executes
   from LLM schema  - /cancel discards

Tool integrations (all optional, import-guarded):
  notion.py      — search, read, append, create, archive (staged)
  gmail.py       — search, read, draft, trash, send (staged)
  calendar.py    — list, find-free, create (staged), delete (staged)
  files.py       — read, list, write (staged), edit (staged), append (staged)
  notes.py       — note store, search, agent memory, feedback
  todo.py        — todo_add, todo_done, todo_list, todo_delete
  diary.py       — diary_add, diary_recent
  context.py     — context_list, context_load (on-demand file loading)
  prompts.py     — read/propose agent prompt files (staged), log tool needs
  health.py      — query health data (steps, HR, sleep, workouts)
```

---

## Quick start

You need: Python 3.11+, a Telegram account, a DeepSeek API key.

```bash
git clone <your repo>
cd me-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

| Variable | Where to get it |
|---|---|
| `DEEPSEEK_API_KEY` | <https://platform.deepseek.com/api_keys> |
| `TELEGRAM_BOT_TOKEN` | Message `@BotFather`, run `/newbot` |
| `TELEGRAM_ALLOWED_USER_IDS` | Message `@userinfobot`, use your numeric ID |

```bash
python -m app.main
```

DM your bot in Telegram. Type `/start` to confirm it's running. Type `/tools` to see every registered tool.

---

## Integrations

All are optional. The bot starts fine with zero integrations — it's a Telegram-fronted chatbot. Each integration is import-guarded; missing config or dependencies log a warning and skip that module.

### Notion (via mcp.notion.com OAuth)

No admin rights or integration registration in Notion is required.

```bash
python -m app.tools.notion_auth
```

This registers a fresh OAuth client dynamically, opens your browser, and saves the token to `data/notion_mcp_token.json`. The token refreshes automatically on 401.

**Tools:** `notion_search`, `notion_get_page`, `notion_append_paragraph`, `notion_create_page`, `notion_archive_page` (staged).

### Gmail + Google Calendar (OAuth, one-time)

1. Google Cloud Console → enable **Gmail API** and **Google Calendar API**.
2. APIs & Services → OAuth consent screen → External → add yourself as a Test user.
3. Credentials → Create OAuth client (Desktop app) → download JSON → save as `data/gmail_credentials.json`.
4. Run `python -m app.tools.gmail_auth` once on a machine with a browser.
5. Copy both `gmail_credentials.json` and `gmail_token.json` to `data/` on your server.

The token auto-refreshes. Scopes cover full Gmail + Calendar read/write.

**Gmail tools:** `gmail_search`, `gmail_get_message`, `gmail_create_draft`, `gmail_trash_message`, `gmail_send_message` (staged).

**Calendar tools:** `calendar_list_events`, `calendar_find_free_slots`, `calendar_create_event` (staged), `calendar_delete_event` (staged).

### Local files

The agent can read, list, and write plain-text files. `FILES_ROOT` defaults to `data/` (the project's own data directory), so file reads and writes stay within the project by default. Set `FILES_ROOT` in `.env` to point at a different directory. Allowed extensions: `.md .txt .rst .csv .json .yaml .yml .toml .ini .cfg .log .html .xml`.

Write/edit/append are staged — the user must confirm before bytes hit disk. Staged tools return a `{"staged": True, "action_id": …, "preview": …}` dict so the footer always appears.

### Health data

Import your exported health data into SQLite once:

```bash
# Place your health data export in HEALTH_DATA/
python scripts/process_health_data.py --data-dir HEALTH_DATA --db data/me.db
```

The workout agent then has access to:

**Tools:** `health_daily_summary`, `health_sport_breakdown`, `health_heart_rate`, `health_workout_sessions`.

---

## Agent routing

Every message is classified by a two-pass LLM call before it reaches the main agent loop.

**Pass 1 — Router** (`data/prompts/router.md`): classifies `{type, agent, category}`.

| Field | Values |
|---|---|
| `type` | `note`, `diary`, `chat` |
| `agent` | `workout`, `lifestyle`, `growth`, `career`, `dutch`, `none` |
| `category` | domain tag for note storage — `workout`, `lifestyle`, `growth`, `career`, `dutch`, `uncategorized`, `none` |

**Pass 2 — Dispatch** (`app/agents/dispatch.py`):
- `note` → router-generated summary stored to `notes` table, then optionally routes to agent for a response
- `diary` → routed to agent (default: `growth`) with instructions to call `diary_add` + `note_add` + any action items
- `agent != none` → persona prompt + today's date + tool rules + recent notes injected as system prompt; agent conversation history injected as messages
- `agent == none` → general assistant with today's date + live tool list

**Auto-injected context:** `personal_profile.md` and `goals/<agent>.md` are automatically included in every specialist agent's system prompt by `_build_agent_context()` — agents always know who Xi is and what their domain goals are without any tool call. Other `data/*.md` files are still loaded on demand via `context_list()` / `context_load()`.

**Sticky-agent fallback:** short messages (≤ 5 words) with `agent=none` inherit the last specialist agent — but only if that conversation happened **within the last hour**. Prevents a morning workout session from dragging into an unrelated evening conversation.

Agent persona files live in `data/prompts/` and can be edited live. Agents can propose changes to their own prompts via `prompt_propose` (staged).

**Adding a new agent:** create `data/prompts/<name>.md` with a `## Routing` section (one-liner describing the domain) and restart. The agent is automatically discovered, added to the router, and its category accepted in all tools — zero code changes required. Optionally add `data/goals/<name>.md` for domain-specific goals.

---

## Using the bot

| What you type | What happens |
|---|---|
| *"I ran 10km today at 6:59/km"* | Logged as a workout note; workout agent responds with analysis |
| *"Add to my goals: reduce screen time to 2.5hrs"* | Routed as chat → lifestyle agent → stages `file_append` to `goals/lifestyle.md` |
| *"Ik wil Nederlands leren"* | Dutch agent corrects/responds; pain points tracked via notes |
| *"What unread mail do I have?"* | `gmail_search` → summary |
| *"Send a one-line OOO to alex@example.com"* | `gmail_send_message` stages → preview shown |
| `confirm` | Executes the most recent pending action (also: `confirn`, `confirm :)`) |
| `cancel` | Discards the most recent pending action |
| `/confirm <id>` | Confirm a specific staged action by ID |
| `/cancel <id>` | Cancel a specific staged action by ID |
| `/reset` | Start fresh (prior history excluded from LLM context) |
| `/tools` | List every registered tool and its description |

---

## Staged actions (destructive tools)

Tools that can't be undone (`gmail_send_message`, `notion_archive_page`, `calendar_create_event`, `file_write`, `file_edit`, `file_append`, etc.) never execute immediately. They write to `pending_actions` in SQLite and return a preview.

Every reply that has a staged action gets an unconditional footer:

```
⏳ STAGED — none of these have happened yet:

[id: a3f8b2c1]
Write file: goals.md
  Size: 312 characters
  Preview: "## Career Goal\n- Goal: Transition to freelancing..."
→ /confirm a3f8b2c1   |   /cancel a3f8b2c1
```

The footer is the source of truth — it appears regardless of what the LLM said in its text reply. The footer is built directly from the tool's return value (`{"staged": True, "action_id": …}`), not from the LLM's description.

---

## Debugging

**Debug mode** — set `LOG_LEVEL=DEBUG` in `.env`. Every reply gets a header:

```
🔧 file_append(path="goals/career.md", text="## Career Goal…") → staged a3f8b2c1 [4ms]
———
<LLM reply>
———
<staged-action footer>
```

**Audit log** — every event is appended to `data/agent.log.json`:

```bash
# Live tail
tail -f data/agent.log.json | jq

# All failed tool calls
jq 'select(.event=="tool_call" and .ok==false)' data/agent.log.json

# All routing decisions
jq 'select(.event=="route_decision")' data/agent.log.json

# Hallucination guard triggers
jq 'select(.event=="warning")' data/agent.WARNING.json

# Total tokens used today
jq -s '[.[] | select(.event=="llm_response") | .usage.total] | add' data/agent.log.json
```

Disable audit log by setting `AUDIT_LOG_PATH=` (empty) in `.env`.

**Python log files** in `data/`:
- `agent.INFO.json` — INFO-level messages only
- `agent.WARNING.json` — WARNING messages (router failures, hallucination guard triggers)
- `agent.ERROR.json` — errors with full tracebacks

---

## Configuration reference

```env
# Required
DEEPSEEK_API_KEY=sk-...
TELEGRAM_BOT_TOKEN=12345:ABC...
TELEGRAM_ALLOWED_USER_IDS=123456789   # comma-separated Telegram numeric IDs

# Storage
DB_PATH=./data/me.db
AUDIT_LOG_PATH=./data/agent.log.json  # set to '' to disable

# Behaviour
LOG_LEVEL=INFO              # DEBUG adds per-tool header to every Telegram reply
DEEPSEEK_MODEL=deepseek-v4-flash  # or deepseek-chat, deepseek-reasoner
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1  # swap to openai.com/v1 or a local Ollama endpoint
HISTORY_TURNS=20

# LLM tuning
AGENT_TEMPERATURE=0.8       # 0.0 = deterministic, 1.0 = most varied
MAX_TOOL_TURNS=10           # max tool-call iterations per user message

# Optional
FILES_ROOT=./data           # root for file tools (defaults to project data/ dir)
DIARY_DEFAULT_AGENT=growth  # which agent handles diary entries when router returns agent=none
NOTION_DATA_DIR=./data      # where notion_mcp_token.json is stored
GMAIL_DATA_DIR=./data       # where gmail_credentials.json and gmail_token.json are stored
```

---

## File map

```
me-agent/
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── ARCHITECTURE.md                  ← codebase walkthrough for new developers
├── data/                            ← persisted data (partially gitignored)
│   ├── goals/                       ← per-domain goals (gitignored)
│   │   ├── workout.md
│   │   ├── growth.md
│   │   ├── lifestyle.md
│   │   ├── career.md
│   │   └── dutch.md
│   ├── personal_profile.md          ← Xi's profile (auto-injected into every agent)
│   ├── diary.md                     ← dated journal entries (written by diary_add)
│   ├── todo.md                      ← todo list (managed by todo_add / todo_done)
│   ├── health_summary.md            ← static health export (excluded from context_list)
│   └── prompts/                     ← agent persona files (editable live)
│       ├── router.md                ← message type + agent classification + note summary prompt
│       ├── workout.md
│       ├── lifestyle.md
│       ├── growth.md
│       ├── career.md
│       └── dutch.md
├── scripts/
│   └── process_health_data.py       ← one-time health data import
└── app/
    ├── __init__.py
    ├── config.py                    ← env-var loading + validation
    ├── db.py                        ← SQLite schema (messages, notes, health tables…)
    ├── audit.py                     ← thread-safe JSONL audit logger
    ├── debug_log.py                 ← optional full-fidelity debug log (LOG_LEVEL=DEBUG)
    ├── llm.py                       ← DeepSeek client + tool-calling loop
    ├── main.py                      ← Telegram bot entry point
    ├── agents/
    │   ├── __init__.py
    │   ├── discovery.py             ← dynamic agent registry (scans data/prompts/)
    │   ├── dispatch.py              ← two-pass routing + agent persona builder
    │   └── router.py               ← LLM-based classifier + context assembly
    └── tools/
        ├── __init__.py              ← imports each integration with guard
        ├── registry.py             ← @tool decorator, user_id injection
        ├── pending.py              ← staged-action queue
        ├── context.py              ← context_list / context_load (on-demand file loading)
        ├── notes.py                ← note store, agent memory, feedback
        ├── todo.py                 ← todo list (backed by data/todo.md)
        ├── diary.py                ← diary entries (backed by data/diary.md)
        ├── prompts.py              ← read/propose agent prompt files
        ├── health.py               ← health data query tools
        ├── notion.py               ← Notion MCP tools
        ├── notion_auth.py          ← one-time OAuth setup for Notion MCP
        ├── notion_cookie_auth.py   ← legacy cookie-based setup (superseded)
        ├── gmail.py                ← Gmail tools
        ├── gmail_auth.py           ← one-time OAuth setup for Gmail + Calendar
        ├── calendar.py             ← Google Calendar tools (supports all_day events)
        └── files.py                ← local file tools (root: data/ by default)
```

---

## Lessons baked into the code

- **`user_id` is hidden from LLM schemas.** If the model sees `user_id: integer` as a required parameter, it fills it in with a hallucinated number. The staged action then belongs to a phantom user and `/confirm` fails. The agent loop injects it server-side after dispatch.
- **Staged tools return dicts, not strings.** `file_append` returns `{"staged": True, "action_id": …, "preview": …}`. The staged footer is built from this dict. If a tool returns a plain string, `isinstance(result, dict)` fails and the footer never fires — silent staging failure.
- **Confirm shortcuts need fuzzy matching.** Users type `"confirn"`, `"confirm :)"`, `"CONFIRM!"`. Stripping only `.!?` misses emoji and typos. The normaliser strips all non-letter characters before matching, covering the real cases seen in production.
- **Don't duplicate history in the system prompt.** `_build_agent_context()` previously included a truncated text digest of conversation history. This was removed because the same data is already passed as properly-formatted OpenAI messages. Double-injecting wastes tokens and can confuse the model with two slightly different versions.
- **Sticky agent needs a recency window.** Without one, a morning workout conversation will silently pull in an unrelated "yes" from the evening. The 1-hour window means stale context expires naturally.
- **Diary entries need `note_add` too.** `diary_add` writes to `diary.md` but the context block is built from the `notes` table. Without an explicit `note_add` call, diary content is invisible to future conversations.
- **Hallucination guard needs a regex, not keywords.** A keyword list fires on `"I've noted your baseline"` or `"well done"`. A regex matching `I've saved`, `I added`, `I logged` only fires on explicit first-person write claims.
- **DeepSeek thinking models return `reasoning_content`** which the API requires you to echo back on turn 2+, or you get HTTP 400. Using `msg.model_dump(exclude_none=True)` preserves all extra fields automatically.
- **Notion MCP tokens expire hourly.** The `refresh_access_token()` function in `notion_auth.py` refreshes on 401 automatically; no manual re-auth needed.
- **Health data may be historical.** The `health_workout_sessions` tool uses `MAX(date)` from the table as the reference point rather than `date('now')`, so queries like "last 30 days" work correctly against historical data.
- **The LLM is not a reliable narrator.** The staged-action footer is the structural source of truth for "what is pending" — shown unconditionally below the LLM's reply text, built from tool return values, not from what the LLM said.
