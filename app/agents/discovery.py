"""Dynamic agent discovery.

Agents are any .md files in data/prompts/ that are not reserved system files.
Adding a new agent requires no code changes:
  1. Drop data/prompts/<name>.md  (include a ## Routing section — see below)
  2. Optionally add data/goals/<name>.md
  3. Restart the bot

## Routing section format
Each agent prompt should contain a ## Routing section with a single line
describing the agent's domain. This line is injected into router.md at runtime
so the LLM knows when to route to this agent.

Example in data/prompts/workout.md:
    ## Routing
    Physical training, exercise, sport, fitness, body, recovery.

If the section is absent, a generic description is used as fallback.

The agent list is cached for the process lifetime — restart to pick up new agents.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "data" / "prompts"

# Files in data/prompts/ that are system prompts, NOT specialist agents.
# Skill files (grill, notion, …) live in data/prompts/skills/ so they are
# never scanned by get_agents() — no reserved list needed for them.
_RESERVED = frozenset({"router"})


@lru_cache(maxsize=1)
def get_agents() -> tuple[str, ...]:
    """Return a sorted tuple of discovered agent names.

    Scans data/prompts/ for .md files and excludes reserved names.
    Cached for the lifetime of the process — restart to pick up new agents.
    """
    if not PROMPTS_DIR.exists():
        log.warning("Prompts directory not found: %s", PROMPTS_DIR)
        return ()
    agents = tuple(sorted(
        p.stem for p in PROMPTS_DIR.glob("*.md")
        if p.stem not in _RESERVED
    ))
    log.info("Discovered agents: %s", list(agents))
    return agents


def get_routing_description(agent: str) -> str:
    """Extract the routing description from an agent prompt's ## Routing section.

    Reads the first non-empty, non-heading line after '## Routing'.
    Falls back to a generic description if the section is absent.
    """
    path = PROMPTS_DIR / f"{agent}.md"
    if not path.exists():
        return f"{agent.capitalize()}-related topics."

    lines = path.read_text(encoding="utf-8").splitlines()
    in_routing = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "## routing":
            in_routing = True
            continue
        if in_routing:
            content = stripped.lstrip("-").strip()
            if content and not content.startswith("#"):
                return content
            if content.startswith("#"):
                break  # hit the next section without finding content

    log.debug("Agent %r has no ## Routing section — using generic fallback", agent)
    return f"{agent.capitalize()}-related topics."


def build_agents_block() -> str:
    """Build the agents markdown block for injection into router.md.

    Generates one bullet per discovered agent using its ## Routing description,
    then appends the hardcoded 'none' option (not a file-based agent).
    """
    lines: list[str] = []
    for agent in get_agents():
        desc = get_routing_description(agent)
        lines.append(f"- **{agent}**: {desc}")
    lines.append("- **none**: No specialist needed — answer directly.")
    return "\n".join(lines)
