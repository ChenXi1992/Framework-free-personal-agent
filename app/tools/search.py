"""Web search tool powered by Tavily.

Disabled automatically if TAVILY_API_KEY is not set — the bot starts fine
without it and simply won't register the web_search tool.
"""
from __future__ import annotations

from typing import Any

from tavily import TavilyClient

from ..config import TAVILY_API_KEY
from .registry import tool

_client = TavilyClient(api_key=TAVILY_API_KEY)


@tool(
    description=(
        "Search the web for current information. Returns a synthesised answer "
        "and the top source snippets. Use when the user asks about recent events, "
        "facts you may not know, or anything that requires up-to-date information."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query — be specific for better results.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of source snippets to return (default 5, max 10).",
            },
        },
        "required": ["query"],
    },
)
def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Run a Tavily search and return the answer + top source snippets."""
    max_results = min(max(1, max_results), 10)
    try:
        resp = _client.search(
            query,
            search_depth="basic",
            max_results=max_results,
            include_answer=True,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}

    return {
        "answer": resp.get("answer") or "",
        "sources": [
            {
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "snippet": r.get("content", "")[:400],
            }
            for r in resp.get("results", [])
        ],
    }
