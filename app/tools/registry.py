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
    destructive: bool = False  # if True, must go through pending-action flow

    def to_openai(self) -> dict[str, Any]:
        # Hide `user_id` from the LLM. It's an agent-loop-injected field, not
        # something the model should ever see or guess. If we left it in the
        # schema (as we did initially), DeepSeek would fill it in with a
        # hallucinated number, and the staged action would belong to a
        # phantom user — breaking /confirm ownership checks.
        params = self.parameters
        props = params.get("properties", {})
        required = params.get("required", [])
        if "user_id" in props or "user_id" in required:
            params = {
                **params,
                "properties": {k: v for k, v in props.items() if k != "user_id"},
                "required": [r for r in required if r != "user_id"],
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
