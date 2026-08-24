"""What the AgentAPI mixins require of the object they are mixed into.

Typing-only, like `agents/planner/hosts.py` and `agents/mixins/host.py`: nothing
here runs and nothing is inherited at runtime, so the real MRO is exactly what it
would be without it.

Split by mixin rather than combined, because the split is the interesting part.
`queries` reaches for almost nothing -- it asks the database and publishes the
answer -- while `streams` and `messaging` both hold the actor itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .agent import DynamicAgent


class QueryHost(Protocol):
    """Reading stored data back needs only a name and a way out."""

    name: str

    async def publish(self, topic: str, data: Any) -> Any: ...

    async def mqtt_get(self, topic: str, timeout: float = ...) -> Any: ...


class ApiHost(QueryHost, Protocol):
    """Anything touching MQTT directly also holds the actor and its topic set."""

    actor_id: str
    _actor: DynamicAgent
    _published_topics: set[str]

    async def _publish_manifest(self) -> None: ...
