"""What the mixins require of the class they are mixed into.

Each mixin here reaches for attributes it does not define — `self.name`,
`self._registry`, `self.llm` — and relies on `MainActor` supplying them by
inheriting `LLMAgent`. That relationship was written only in prose, at the top
of each mixin, so nothing checked it and a type checker reported every use as an
unknown attribute: around 190 findings, all of one cause, burying the real ones.

These Protocols say the same thing in a form that is checked. They are
**typing-only**: nothing here runs, nothing is inherited at runtime, and the
method resolution order is untouched. If any of that changes, the change has
gone wrong.

The split is deliberate. `ActorHost` is what every mixin needs; each mixin then
names only what it additionally requires. One combined Protocol would be
simpler and would hide the interesting part — that `RoutingMixin` does not touch
persistence, and that a mixin needing something surprising is a fact about
coupling worth seeing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ...core.actor import Actor, MessageType
    from ...core.registry import ActorRegistry
    from ..llm_agent import LLMProvider


class ActorHost(Protocol):
    """The `Actor` surface every mixin uses."""

    name: str
    actor_id: str
    _registry: ActorRegistry | None
    _persistence_dir: Path

    async def send(self, target_id: str, msg_type: MessageType, payload: Any = None) -> bool: ...

    async def spawn(self, actor_class: type[Actor], **kwargs: Any) -> Actor: ...

    def persist(self, key: str, value: Any) -> None: ...

    def recall(self, key: str, default: Any = None) -> Any: ...


class LLMHost(ActorHost, Protocol):
    """`ActorHost` plus the `LLMAgent` surface — a provider and cost tracking."""

    llm: LLMProvider | None
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float

    def _persist_cost(self) -> None: ...


class RoutingHost(LLMHost, Protocol):
    """What `RoutingMixin` needs: the LLM surface plus in-flight task futures."""

    _result_futures: dict[str, asyncio.Future]


class MemoryHost(LLMHost, Protocol):
    """What `MemoryMixin` needs — nothing beyond the LLM host.

    Named anyway rather than reusing `LLMHost` directly: the mixin should say
    what it depends on, and if that grows the change belongs here where it is
    visible.
    """


class SpawnHost(ActorHost, Protocol):
    """What `SpawnMixin` needs: a provider to give the agents it creates, plus
    in-flight task futures.

    The provider is handed on rather than called through, so a spawning host
    owes no cost tracking and need not be an `LLMAgent`.
    """

    llm: LLMProvider | None
    _result_futures: dict[str, asyncio.Future]


class PlanningHost(LLMHost, Protocol):
    """What `PlanningMixin` needs — and it is the widest of the four.

    Two things fall out of this list and neither is incidental:

    - **`get_user_facts` comes from `MemoryMixin`**, not from any base. Planning
      depends on a sibling mixin, so the two cannot be composed independently.
      That coupling was real, undocumented, and only discoverable by running it.
    - **The last four are `MainActor`'s own methods.** This mixin is not merely
      "an LLMAgent host" as its docstring says; it requires `MainActor`
      specifically, which is why it cannot be reused elsewhere as written.
    """

    _result_futures: dict[str, asyncio.Future]
    _conversation_history: list[dict]

    def get_user_facts(self) -> dict: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...

    def _remove_from_spawn_registry(self, name: str) -> None: ...

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None: ...

    def _record_agent_deletion(self, name: str, reason: str = "user request") -> None: ...
