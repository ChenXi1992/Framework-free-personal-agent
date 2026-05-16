"""Notion tools — powered by the Notion MCP server (mcp.notion.com).

Auth: run `python -m app.tools.notion_auth` once. It registers a fresh OAuth
client dynamically (no Notion admin rights needed), opens your browser, and
saves the bearer token to data/notion_mcp_token.json.

All tool signatures are unchanged — main.py and the agent loop need no edits.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import requests

from .pending import stage_action
from .registry import tool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP HTTP client
# ---------------------------------------------------------------------------

MCP_URL  = "https://mcp.notion.com/mcp"
DATA_DIR = Path(os.environ.get("NOTION_DATA_DIR", "./data"))


class _MCPError(Exception):
    pass


class _NotionMCPClient:
    """Thin JSON-RPC client for the Notion MCP server."""

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization":        f"Bearer {token}",
            "Content-Type":         "application/json",
            "Accept":               "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        })
        self._id         = 0
        self._session_id: str | None = None
        self._initialize()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _initialize(self) -> None:
        """Perform the MCP handshake and store the session ID."""
        payload = {
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  "initialize",
            "params":  {
                "protocolVersion": "2025-03-26",
                "capabilities":    {},
                "clientInfo":      {"name": "me-agent", "version": "1.0"},
            },
        }
        resp = self._session.post(MCP_URL, json=payload, timeout=20)
        resp.raise_for_status()
        self._session_id = resp.headers.get("Mcp-Session-Id")
        if self._session_id:
            self._session.headers["Mcp-Session-Id"] = self._session_id
        log.debug("MCP session initialised: %s", self._session_id)

    def _post(self, payload: dict) -> dict:
        resp = self._session.post(MCP_URL, json=payload, timeout=20)
        if resp.status_code == 401:
            # Token expired — try refresh once before giving up
            log.info("Notion token expired, refreshing…")
            from .notion_auth import refresh_access_token
            new_token = refresh_access_token()
            if new_token:
                self._session.headers["Authorization"] = f"Bearer {new_token}"
                self._session_id = None
                self._initialize()
                resp = self._session.post(MCP_URL, json=payload, timeout=20)
            else:
                log.warning("Notion token refresh failed — re-run notion_auth.py")
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            return _parse_sse(resp.text)
        return resp.json()

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool and return its parsed result."""
        payload = {
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  "tools/call",
            "params":  {"name": tool_name, "arguments": arguments},
        }
        body = self._post(payload)

        if "error" in body:
            raise _MCPError(body["error"].get("message", str(body["error"])))

        result  = body.get("result", {})
        content = result.get("content", [])
        if not content:
            return {}
        texts = [c["text"] for c in content if c.get("type") == "text"]
        raw   = "\n".join(texts)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}


def _parse_sse(text: str) -> dict:
    """Extract the last JSON object from an SSE stream."""
    last: dict = {}
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    last = json.loads(data)
                except json.JSONDecodeError:
                    pass
    return last


# ---------------------------------------------------------------------------
# Client construction — reads token written by notion_auth.py
# ---------------------------------------------------------------------------

def _load_client() -> _NotionMCPClient | None:
    # Env var override (raw bearer token)
    token = os.environ.get("NOTION_MCP_TOKEN", "")
    if not token:
        f = DATA_DIR / "notion_mcp_token.json"
        if f.exists():
            try:
                data = json.loads(f.read_text())
                token = data.get("access_token", "")
            except Exception as e:  # noqa: BLE001
                log.warning("Could not read %s: %s", f, e)
    if not token:
        return None
    try:
        return _NotionMCPClient(token)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            log.info("Notion access token rejected at startup, attempting refresh…")
            from .notion_auth import refresh_access_token
            new_token = refresh_access_token()
            if new_token:
                try:
                    return _NotionMCPClient(new_token)
                except Exception as e2:  # noqa: BLE001
                    log.warning("Notion still failing after refresh: %s", e2)
            else:
                log.warning(
                    "Notion refresh failed — run `python -m app.tools.notion_auth`"
                )
        return None


_client_instance: _NotionMCPClient | None = _load_client()

if _client_instance is None:
    log.warning(
        "No Notion MCP token found. "
        "Run `python -m app.tools.notion_auth` to set up access."
    )


def _client() -> _NotionMCPClient:
    if _client_instance is None:
        raise RuntimeError(
            "Notion not configured. "
            "Run `python -m app.tools.notion_auth` first."
        )
    return _client_instance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The `workspace` parameter ("personal" | "work") is kept in every tool
# signature so the LLM can express intent clearly in its tool calls. However,
# mcp.notion.com serves ALL workspaces the user authorised during OAuth from a
# single token — there is no separate client per workspace. The parameter is
# accepted but not forwarded to the MCP server. If you need true workspace
# isolation in the future, create one token per workspace and route on it here.

_WORKSPACE_PARAM = {
    "type": "string",
    "enum": ["personal", "work"],
    "description": "Which Notion workspace to use (authorised during OAuth setup).",
}


def _safe_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return _client().call(tool_name, arguments)
    except _MCPError as e:
        return {"error": f"Notion MCP: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Notion: {e}"}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Search Notion for pages/databases whose title matches the query. "
        "Returns up to `page_size` summaries (id, title, url, last_edited). "
        "Empty query returns recently edited pages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query":      {"type": "string", "description": "Search text. Empty matches everything."},
            "workspace":  _WORKSPACE_PARAM,
            "page_size":  {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "required": ["query", "workspace"],
    },
)
def notion_search(query: str, workspace: str, page_size: int = 10) -> dict[str, Any]:
    return _safe_call("notion-search", {
        "query":     query or " ",
        "filters":   {},
        "page_size": min(page_size, 25),
    })


@tool(
    description=(
        "Read a single Notion page's content. Returns title and a markdown "
        "rendering of the page body. Use after notion_search to fetch content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "page_id":   {"type": "string", "description": "Notion page UUID."},
            "workspace": _WORKSPACE_PARAM,
        },
        "required": ["page_id", "workspace"],
    },
)
def notion_get_page(page_id: str, workspace: str) -> dict[str, Any]:
    return _safe_call("notion-fetch", {"id": page_id})


# ---------------------------------------------------------------------------
# Write tools (non-destructive — execute immediately)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Append a paragraph to the bottom of an existing Notion page. "
        "Existing content is never modified."
    ),
    parameters={
        "type": "object",
        "properties": {
            "page_id":   {"type": "string"},
            "text":      {"type": "string", "description": "Paragraph text to append."},
            "workspace": _WORKSPACE_PARAM,
        },
        "required": ["page_id", "text", "workspace"],
    },
)
def notion_append_paragraph(page_id: str, text: str, workspace: str) -> dict[str, Any]:
    return _safe_call("notion-update-page", {
        "id":      page_id,
        "content": text,
        "mode":    "append",
    })


@tool(
    description=(
        "Create a new Notion page as a child of an existing page. "
        "The parent must be accessible in the authorised workspace."
    ),
    parameters={
        "type": "object",
        "properties": {
            "parent_page_id": {"type": "string", "description": "ID of the parent page."},
            "title":          {"type": "string"},
            "body":           {"type": "string", "description": "Optional initial body paragraph."},
            "workspace":      _WORKSPACE_PARAM,
        },
        "required": ["parent_page_id", "title", "workspace"],
    },
)
def notion_create_page(
    parent_page_id: str,
    title: str,
    workspace: str,
    body: str = "",
) -> dict[str, Any]:
    children = []
    if body:
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": body}}]
            },
        })
    return _safe_call("notion-create-pages", {
        "parent": {"page_id": parent_page_id, "type": "page_id"},
        "pages":  [{"properties": {"title": title}, "content": body}],
    })


# ---------------------------------------------------------------------------
# Destructive write tools (staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Archive (soft-delete) a Notion page. STAGED — requires user confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "page_id":   {"type": "string"},
            "workspace": _WORKSPACE_PARAM,
            "user_id":   {"type": "integer"},
        },
        "required": ["page_id", "workspace", "user_id"],
    },
    destructive=True,
)
def notion_archive_page(page_id: str, workspace: str, user_id: int) -> dict[str, Any]:
    page = _safe_call("notion_retrieve_page", {"page_id": page_id})
    if "error" in page:
        return page
    title   = _extract_title(page)
    preview = f"Archive Notion page: \"{title}\" ({page_id})"
    action_id = stage_action(
        user_id=user_id,
        tool_name="notion_archive_page",
        arguments={"page_id": page_id, "workspace": workspace},
        preview=preview,
    )
    return {
        "staged": True,
        "action_id": action_id,
        "preview": preview,
        "instruction": (
            "Tell the user which page will be archived and ask them to "
            "reply 'confirm' to proceed or 'cancel' to abort."
        ),
    }


def _extract_title(page: dict) -> str:
    try:
        props = page.get("properties", {})
        for v in props.values():
            if v.get("type") == "title":
                parts = v.get("title", [])
                return "".join(p.get("plain_text", "") for p in parts)
    except Exception:  # noqa: BLE001
        pass
    return "(untitled)"


# ---------------------------------------------------------------------------
# Executor for /confirm handler
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "notion_archive_page":
        result = _safe_call("notion-move-pages", {
            "page_or_database_ids": [arguments["page_id"]],
            "new_parent":           {"type": "workspace"},
        })
        if "error" in result:
            return {"ok": False, "error": result["error"]}
        return {"ok": True}
    return {"ok": False, "error": f"unknown tool: {tool_name}"}
