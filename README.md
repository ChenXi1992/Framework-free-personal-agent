# me-agent

A personal Telegram-fronted AI assistant. Reads Gmail, Notion, Google Calendar, and local files. Routes messages to specialist agents (workout, lifestyle, career) and remembers your history. Hand-rolled agent loop on top of DeepSeek's OpenAI-compatible API — no LangChain, no LlamaIndex, no framework.

Designed to run on a local NAS or home server. Works just as well on a laptop or a $5/month VPS.

---

## What it does

- **Read your stuff.** Search Gmail, read Notion pages, list calendar events, read local text files.
- **Write your stuff.** Append to Notion, compose drafts, create calendar events, edit files — gated behind a two-step confirmation flow for anything destructive.
- **Specialist agents.** A workout agent with access to your Huawei Health history; a lifestyle agent; a career agent. Each has its own memory, persona, and context.
- **Note intake.** Anything classified as a "note" is stored and surfaces back to the relevant agent as context.
- **Stays out of your way.** No inbound ports (long polling). No manual confirmation typing (`confirm` works, no slash, no action_id).
- **Audit trail.** JSON log of every user message, LLM call, tool invocation, and confirmation.

---

## Architecture

```
Telegram (long polling — no inbound ports)
         │
         ▼
   app/main.py
   - allow-list auth
   - /start /reset /confirm /cancel /tools
   - "confirm" / "cancel" plain-text shortcuts
   - assembles reply (debug header + LLM text + staged-action footer)
         │
         ▼
   app/agents/dispatch.py         ← Pass 1: classify user message
   - router.route(msg) → {type, agent, category}
   - note intake → db.notes
   - builds persona + context for specialist agents
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
 - @tool decorator  queue
 - hides user_id    - /confirm executes
   from LLM schema  - /cancel discards

Tool integrations (all optional, import-guarded):
  notion.py      — search, read, append, create, archive (staged)
  gmail.py       — search, read, draft, trash, send (staged)
  calendar.py    — list, find-free, create (staged), delete (staged)
  files.py       — read, list, write (staged), edit (staged), append (staged)
  notes.py       — note store, search, agent conversation memory, feedback
  prompts.py     — read/propose agent prompt files (staged), log tool needs
  health.py      — query Huawei Health data (steps, HR, sleep, workouts)
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

Write/edit/append are staged — the user must confirm before bytes hit disk.

### Huawei Health data

Import your exported health data into SQLite once:

```bash
# Place your Huawei Health export in HUAWEI_HEALTH_DATA/
python scripts/process_health_data.py --data-dir HUAWEI_HEALTH_DATA --db data/me.db
```

The workout agent then has access to:

**Tools:** `health_daily_summary`, `health_sport_breakdown`, `health_heart_rate`, `health_workout_sessions`.

---

## Agent routing

Every message is classified by a two-pass LLM call before it reaches the main agent loop.

**Pass 1 — Router** (`data/prompts/router.md`): classifies `{type, agent, category}`.
- `type`: `conversation`, `task`, or `note`
- `agent`: `workout`, `lifestyle`, `career`, or `none`
- `category`: finer-grained tag used for note retrieval

**Pass 2 — Dispatch** (`app/agents/dispatch.py`):
- `note` → stored in `notes` table, optionally routed to agent for a response
- `agent != none` → persona prompt + today's date + tool rules + recent notes + conversation memory injected; agent history used instead of global history
- `agent == none` → general assistant with today's date + live tool list injected

Context files (`goals.md`, `personal_profile.md`, etc.) are **not** auto-injected. The agent calls `context_list()` to see what exists and `context_load(filename)` for any file relevant to the question — loading only what's needed keeps token usage down.

Agent persona files live in `data/prompts/` and can be edited. Agents can propose changes to their own prompts via `prompt_propose` (staged).

---

## Using the bot

| What you type | What happens |
|---|---|
| *"What unread mail do I have?"* | `gmail_search` → summary |
| *"Read my Notion page 'Q2 Goals'"* | `notion_search` + `notion_get_page` |
| *"Send a one-line OOO to alex@example.com"* | `gmail_send_message` stages → preview shown |
| `confirm` | Executes the most recent pending action |
| `cancel` | Discards the most recent pending action |
| `/confirm <id>` | Confirm a specific staged action by ID |
| `/cancel <id>` | Cancel a specific staged action by ID |
| `/reset` | Start fresh (prior history excluded from LLM context) |
| `/tools` | List every registered tool and its description |

---

## Staged actions (destructive tools)

Tools that can't be undone (`gmail_send_message`, `notion_archive_page`, `calendar_create_event`, etc.) never execute immediately. They write to `pending_actions` in SQLite and return a preview.

Every reply that has a staged action gets an unconditional footer:

```
⏳ STAGED — none of these have happened yet:

[id: a3f8b2c1]
Send email
  To: alex@example.com
  Subject: Quick question
  Body: ...

→ /confirm a3f8b2c1   |   /cancel a3f8b2c1
```

The footer is the source of truth — it appears regardless of what the LLM said in its text.

---

## Debugging

**Debug mode** — set `LOG_LEVEL=DEBUG` in `.env`. Every reply gets a header:

```
🔧 gmail_search(query="is:unread") → 3 result(s) [410ms]
🔧 gmail_send_message(to="alex@...", subject="…") → staged a3f8b2c1 [18ms]
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

# Total tokens used today
jq -s '[.[] | select(.event=="llm_response") | .usage.total] | add' data/agent.log.json
```

Disable by setting `AUDIT_LOG_PATH=` (empty) in `.env`.

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
AGENT_TEMPERATURE=0.9       # 0.0 = deterministic, 1.0 = most varied
MAX_TOOL_TURNS=10           # max tool-call iterations per user message

# Optional
FILES_ROOT=./data           # root for file tools (defaults to project data/ dir)
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
│   ├── goals.md                     ← Xi's personal goals (loaded on demand by agents)
│   ├── personal_profile.md          ← Xi's profile (loaded on demand by agents)
│   ├── health_summary.md            ← static health export (excluded from context_list)
│   └── prompts/                     ← agent persona files (editable)
│       ├── router.md
│       ├── classifier.md
│       ├── workout.md
│       ├── lifestyle.md
│       └── career.md
├── scripts/
│   └── process_health_data.py       ← one-time Huawei Health import
└── app/
    ├── __init__.py
    ├── config.py                    ← env-var loading + validation
    ├── db.py                        ← SQLite schema (messages, notes, health tables…)
    ├── audit.py                     ← thread-safe JSONL audit logger
    ├── llm.py                       ← DeepSeek client + tool-calling loop
    ├── main.py                      ← Telegram bot entry point
    ├── agents/
    │   ├── __init__.py
    │   ├── dispatch.py              ← two-pass routing + agent persona builder
    │   └── router.py               ← LLM-based classifier + context assembly
    └── tools/
        ├── __init__.py              ← imports each integration with guard
        ├── registry.py             ← @tool decorator, user_id injection
        ├── pending.py              ← staged-action queue
        ├── context.py              ← context_list / context_load (on-demand file loading)
        ├── notes.py                ← note store, agent memory, feedback
        ├── prompts.py              ← read/propose agent prompt files
        ├── health.py               ← Huawei Health query tools
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

- **`user_id` is hidden from LLM schemas.** If the model sees `user_id: integer` as a required parameter, it fills it in with a hallucinated number. The staged action then belongs to a phantom user and `/confirm` fails the ownership check. The agent loop injects it server-side after dispatch.
- **DeepSeek thinking models return `reasoning_content`** which the API requires you to echo back on turn 2+, or you get HTTP 400. Using `msg.model_dump(exclude_none=True)` preserves all extra fields automatically.
- **Notion MCP tokens expire hourly.** The `refresh_access_token()` function in `notion_auth.py` refreshes on 401 automatically; no manual re-auth needed.
- **Huawei Health data ends in Feb 2023.** The `health_workout_sessions` tool uses `MAX(date)` from the table as the reference point rather than `date('now')`, so queries like "last 30 days" work correctly against historical data.
- **The LLM is not a reliable narrator.** The staged-action footer is the structural source of truth for "what is pending" — shown unconditionally below the LLM's reply text.
