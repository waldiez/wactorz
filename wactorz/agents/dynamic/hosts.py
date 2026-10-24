"""What the AgentAPI mixins require of the object they are mixed into.

Typing-only, like `agents/planner/hosts.py` and `agents/mixins/host.py`: nothing
here runs and nothing is inherited at runtime, so the real MRO is exactly what it
would be without it.

Split by mixin rather than combined, because the split is the interesting part.
`queries` reaches for little -- the database, or main through the actor when
the agent runs on a node -- while `streams` and `messaging` lean on the actor
for much more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .agent import DynamicAgent


class QueryHost(Protocol):
    """Reading stored data back needs a name, a way out, and the actor.

    The actor says whether the history is read here or from main.
    """

    name: str
    _actor: DynamicAgent

    async def publish(self, topic: str, data: Any) -> Any: ...

    async def mqtt_get(self, topic: str, timeout: float = ...) -> Any: ...


class ApiHost(QueryHost, Protocol):
    """Anything touching MQTT directly also holds the actor and its topic set."""

    actor_id: str
    _published_topics: set[str]
    _windows: dict[str, Any]

    async def _publish_manifest(self) -> None: ...
