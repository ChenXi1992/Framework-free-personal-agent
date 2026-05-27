"""Tool registry.

Each integration module declares tools with the `@tool(...)` decorator. The
decorator records the function, its description, and an OpenAI-format JSON
schema for the parameters. The agent loop reads `REGISTRY` to (a) advertise
tools to DeepSeek and (b) dispatch incoming tool calls to Python functions.

Schemas are written by hand instead of derived from type hints — it's a few
extra lines per tool but the resulting prompts are far better, because we
can control the wording the model sees for each parameter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

REGISTRY: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    description: str
    parameters: dict[str, Any]
    destructive: bool = False   # if True, must go through pending-action flow
    agent_scoped: bool = False  # if True, `agent` is injected server-side (hidden from LLM)

    def to_openai(self) -> dict[str, Any]:
        # Hide server-injected fields from the LLM schema.
        # `user_id` — injected for destructive tools; LLM must never guess it.
        # `agent`   — injected for agent_scoped tools; LLM knows its own name
        #             but injecting it prevents hallucination and keeps schemas clean.
        _hidden = {"user_id", "agent"}
        params = self.parameters
        props = params.get("properties", {})
        required = params.get("required", [])
        if _hidden & (set(props) | set(required)):
            params = {
                **params,
                "properties": {k: v for k, v in props.items() if k not in _hidden},
                "required": [r for r in required if r not in _hidden],
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    def call(self, **kwargs: Any) -> Any:
        return self.fn(**kwargs)


def tool(
    *,
    description: str,
    parameters: dict[str, Any],
    destructive: bool = False,
    agent_scoped: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a Python function as an LLM-callable tool.

    Args:
        description: shown verbatim to the LLM. Be specific about behaviour and
            failure modes — this is the model's only documentation.
        parameters: JSON Schema (object) describing the function arguments.
        destructive: True for sends/deletes/etc. The implementation must use
            `pending.stage_action` and return an action_id rather than execute
            directly; the user confirms via /confirm <id>.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if fn.__name__ in REGISTRY:
            raise ValueError(f"Tool {fn.__name__!r} already registered")
        REGISTRY[fn.__name__] = Tool(
            name=fn.__name__,
            fn=fn,
            description=description,
            parameters=parameters,
            destructive=destructive,
            agent_scoped=agent_scoped,
        )
        return fn

    return decorator


def all_openai_tools() -> list[dict[str, Any]]:
    """OpenAI-format tools list for the chat.completions `tools` argument."""
    return [t.to_openai() for t in REGISTRY.values()]


def dispatch(name: str, arguments: dict[str, Any]) -> Any:
    """Look up `name` and call it with `arguments`. Raises KeyError if unknown."""
    if name not in REGISTRY:
        raise KeyError(f"Unknown tool: {name}")
    return REGISTRY[name].call(**arguments)
