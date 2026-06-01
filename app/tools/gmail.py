"""Gmail tools.

Auth: an OAuth refresh token is read from `./data/gmail_token.json`. Run
`python -m app.tools.gmail_auth` once on a machine with a browser to create
that file. The token auto-refreshes; this module only needs `gmail_token.json`
and `gmail_credentials.json` to be present.

Read tools execute immediately. Both `send_message` and `trash_message` are
staged — nothing executes until the user confirms.
"""
from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .gmail_auth import load_credentials
from .pending import stage_action
from .registry import tool

log = logging.getLogger(__name__)

_service = None  # lazy: only build the client when actually needed


def _get_service():
    global _service
    if _service is None:
        creds = load_credentials()
        if creds is None:
            raise RuntimeError(
                "Gmail not authenticated. Run `python -m app.tools.gmail_auth` "
                "to create gmail_token.json."
            )
        _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _header(headers: list[dict[str, str]], name: str) -> str:
    name_l = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_l:
            return h.get("value", "")
    return ""


def _summarise_message(m: dict[str, Any]) -> dict[str, Any]:
    headers = m.get("payload", {}).get("headers", [])
    return {
        "id": m.get("id"),
        "thread_id": m.get("threadId"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "snippet": m.get("snippet"),
        "label_ids": m.get("labelIds", []),
    }


def _extract_body(payload: dict[str, Any]) -> str:
    """Best-effort plain-text extraction. Walks the MIME tree once."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    # Fallback: first part with body data, even if HTML
    if payload.get("body", {}).get("data"):
        try:
            return base64.urlsafe_b64decode(
                payload["body"]["data"]
            ).decode("utf-8", errors="replace")
        except Exception:
            pass
    return ""


def _build_raw_message(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> str:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Search Gmail using Gmail's search syntax (e.g. 'from:alice subject:invoice "
        "newer_than:7d'). Returns up to `max_results` message summaries. Use this "
        "before reading a specific message. For unread inbox use query='is:unread "
        "in:inbox'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query. Empty string returns recent inbox.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
            },
        },
        "required": ["query"],
    },
)
def gmail_search(query: str, max_results: int = 10) -> dict[str, Any]:
    try:
        svc = _get_service()
        listing = svc.users().messages().list(
            userId="me", q=query or None, maxResults=max_results,
        ).execute()
        ids = [m["id"] for m in listing.get("messages", [])]
        # Fetch metadata for each. Could be N requests; for v1 that's fine.
        out = []
        for mid in ids:
            full = svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            out.append(_summarise_message(full))
        return {"results": out, "count": len(out)}
    except (HttpError, RuntimeError) as e:
        return {"error": f"Gmail: {e}"}


@tool(
    description=(
        "Read one Gmail message in full, including plain-text body. Use this "
        "after `gmail_search` returns a message id you need the contents of."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message_id": {"type": "string"},
        },
        "required": ["message_id"],
    },
)
def gmail_get_message(message_id: str) -> dict[str, Any]:
    try:
        svc = _get_service()
        m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    except (HttpError, RuntimeError) as e:
        return {"error": f"Gmail: {e}"}
    summary = _summarise_message(m)
    summary["body"] = _extract_body(m.get("payload", {}))
    return summary


# ---------------------------------------------------------------------------
# Non-destructive writes
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Create a Gmail draft (NOT send). Drafts appear in the user's Drafts "
        "folder for them to review and send manually. Use this when the user "
        "asks for help composing without explicitly asking to send."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body."},
            "cc": {"type": "string", "default": ""},
            "bcc": {"type": "string", "default": ""},
        },
        "required": ["to", "subject", "body"],
    },
)
def gmail_create_draft(
    to: str, subject: str, body: str, cc: str = "", bcc: str = "",
) -> dict[str, Any]:
    try:
        svc = _get_service()
        raw = _build_raw_message(to, subject, body, cc, bcc)
        draft = svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw}},
        ).execute()
    except (HttpError, RuntimeError) as e:
        return {"error": f"Gmail: {e}"}
    return {"ok": True, "draft_id": draft.get("id"), "message_id": draft.get("message", {}).get("id")}


@tool(
    description=(
        "Move a message to Gmail Trash. STAGED — requires confirmation before executing. "
        "Trashed mail is recoverable for 30 days from the Gmail Trash folder."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Gmail message ID to trash."},
            "subject":    {"type": "string", "description": "Subject line for preview (call gmail_get_message first)."},
            "user_id":    {"type": "integer"},
        },
        "required": ["message_id", "user_id"],
    },
    destructive=True,
)
def gmail_trash_message(message_id: str, user_id: int, subject: str = "") -> dict[str, Any]:
    preview = f"Move to Trash: \"{subject or message_id}\""
    action_id = stage_action(
        user_id=user_id,
        tool_name="gmail_trash_message",
        arguments={"message_id": message_id, "subject": subject},
        preview=preview,
    )
    return {"staged": True, "action_id": action_id, "preview": preview}


# ---------------------------------------------------------------------------
# Destructive: send (staged)
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Send an email. THIS IS DESTRUCTIVE — emails cannot be unsent. The "
        "bot stages the action and shows a confirmation footer automatically; "
        "you only need to show the user the recipient, subject, and body so "
        "they can review before they reply 'confirm'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string", "default": ""},
            "bcc": {"type": "string", "default": ""},
            "user_id": {
                "type": "integer",
                "description": "Telegram user_id; injected by the agent loop.",
            },
        },
        "required": ["to", "subject", "body", "user_id"],
    },
    destructive=True,
)
def gmail_send_message(
    to: str, subject: str, body: str, user_id: int, cc: str = "", bcc: str = "",
) -> dict[str, Any]:
    preview = (
        f"Send email\n  To: {to}\n"
        + (f"  Cc: {cc}\n" if cc else "")
        + (f"  Bcc: {bcc}\n" if bcc else "")
        + f"  Subject: {subject}\n\n{body}"
    )
    action_id = stage_action(
        user_id=user_id,
        tool_name="gmail_send_message",
        arguments={"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc},
        preview=preview,
    )
    return {
        "staged": True,
        "action_id": action_id,
        "preview": preview,
        "instruction": (
            "Show the recipient, subject, and body to the user. End your "
            "reply with: 'Reply confirm to send or cancel to discard.' "
            "Do NOT type the action_id or '/confirm <id>' — a footer below "
            "your reply already carries that."
        ),
    }


# ---------------------------------------------------------------------------
# Executor for confirmed pending sends
# ---------------------------------------------------------------------------

def execute_confirmed(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "gmail_send_message":
        try:
            raw = _build_raw_message(
                to=arguments["to"],
                subject=arguments["subject"],
                body=arguments["body"],
                cc=arguments.get("cc", ""),
                bcc=arguments.get("bcc", ""),
            )
            sent = _get_service().users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()
        except (HttpError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "message_id": sent.get("id")}
    if tool_name == "gmail_trash_message":
        try:
            _get_service().users().messages().trash(
                userId="me", id=arguments["message_id"]
            ).execute()
        except (HttpError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}
    return {"ok": False, "error": f"unknown tool: {tool_name}"}
