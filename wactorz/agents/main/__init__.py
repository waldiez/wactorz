"""The orchestrator and the machinery only it uses.

An agent is a file until it grows a second concern; then it becomes a package
with this shape, so the next one reads like this one. `actor.py` is the
orchestrator, the rest are the collaborators it holds and the mixins only it
mixes in.

Layering: this package may import from the shared tier above it
(`agents/mixins`, `agents/llm`, `agents/prompts`, `agents/lookup`); the shared
tier may never import from here.
"""

from .actor import MainActor

__all__ = ["MainActor"]
