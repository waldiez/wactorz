"""The planner and the machinery only it uses.

An agent is a file until it grows a second concern; then it becomes a package
with this shape, so the next one reads like `agents/main/`. `actor.py` is the
agent, and the concerns extracted from it sit beside it.

Layering: this package may import from the shared tier above it
(`agents/mixins`, `agents/llm`, `agents/prompts`, `agents/lookup`); the shared
tier may never import from here.
"""

from .agent import PlannerAgent

__all__ = ["PlannerAgent"]
