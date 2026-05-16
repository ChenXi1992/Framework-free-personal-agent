"""Tool package.

Importing this module side-effect-imports every tool file, which causes their
`@tool(...)` decorators to register themselves into `registry.REGISTRY`. The
agent loop iterates that registry to build the OpenAI `tools=[...]` payload.

Each integration is import-guarded so a missing optional dep (or missing
config) doesn't take the bot down. The bot logs which integrations are
disabled at startup.
"""
import logging

from . import registry  # noqa: F401
from . import pending   # noqa: F401

_log = logging.getLogger(__name__)


def _try(name: str) -> None:
    """Import a tool sub-module and log at DEBUG if it fails.

    Missing optional dependencies (e.g. google-api-python-client) are expected
    in minimal installs — not a problem, just a graceful skip. DEBUG keeps
    startup output clean; the startup summary in main.py reports which tools
    are actually active at INFO level.
    """
    try:
        __import__(f"{__name__}.{name}", fromlist=[name])
    except Exception as e:  # noqa: BLE001
        _log.debug("Tool module %r disabled: %s", name, e)


for _name in ("context", "notion", "gmail", "calendar", "files", "notes", "prompts", "health"):
    _try(_name)
