"""What each node collaborator requires of the actor that owns it.

Typing-only, in the spirit of `mixins/host.py`: nothing here runs, nothing is
inherited, and the collaborators read these attributes rather than being handed
values, because the actor's state and its broker address both change after
construction.

Split rather than combined, for the same reason the mixin protocols are. One
protocol listing everything would be shorter and would hide what is worth
seeing: that the manifest registry needs nothing but a connection, that only
migration reaches for the spawn machinery, and that a collaborator asking for
something surprising is a fact about coupling rather than a detail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import asyncio

    from ...core.actor import ActorState
    from ..llm_agent import LLMProvider

from ..mixins.host import ActorHost, LLMHost


class ListenerHost(Protocol):
    """What anything holding open an MQTT subscription needs.

    The state is read on every pass of the loop rather than captured, so a
    collaborator stops when the actor does.
    """

    name: str
    state: ActorState
    _mqtt_broker: str
    _mqtt_port: int


class ManifestHost(ListenerHost, Protocol):
    """The manifest registry needs only a connection.

    It owns its own tables and answers from them, which is why this adds
    nothing — worth stating rather than leaving to be inferred from a larger
    protocol it happens to satisfy.
    """


class SpawnHost(Protocol):
    """What putting an agent somewhere needs from the actor.

    Wider than the others, and that width is the finding rather than an
    accident: spawning reaches persistence, the live registry, the broker, the
    node table, the catalogue and the local spawn machinery, because it is the
    one operation that touches every one of them. Listing them says so.

    No connection is inherited from `ListenerHost` — nothing here holds a
    subscription open. Publishing goes through the actor.
    """

    name: str
    actor_id: str
    _registry: Any
    _result_futures: dict[str, Any]

    @property
    def _agent_manifests(self) -> dict[str, dict[str, Any]]:
        """Read-only here: the actor owns this and hands it out."""
        ...

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        """Read-only here: consulted for a node's address before an install."""
        ...

    def recall(self, key: str) -> Any: ...

    def persist(self, key: str, value: Any) -> None: ...

    def _match_catalog_recipe(
        self, name: str, capabilities: list[str] | None = ...
    ) -> str | None: ...

    async def send(self, target_id: str, msg_type: Any, payload: dict[str, Any]) -> bool: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = ..., qos: int = ...
    ) -> None: ...

    async def _update_node_desired_state(self, node: str, new_config: dict[str, Any]) -> None: ...

    async def _spawn_local_from_config(
        self, config: dict[str, Any], *, register: bool = ..., from_registry: bool = ...
    ) -> Any: ...


class DelegationHost(Protocol):
    """What handing a task to another agent needs from the actor.

    The broker address is read rather than passed: a reply topic opens its own
    connection each time, and the address can change after construction.

    `_resolve_or_spawn` is the one surprising entry. Delegation resolves a name
    before sending to it, and an unrestricted caller is allowed to have the
    catalogue spawn what it named — so delegation reaches the spawn machinery,
    and the restricted path exists precisely to not.
    """

    name: str
    actor_id: str
    _registry: Any
    _result_futures: dict[str, Any]
    _mqtt_broker: str
    _mqtt_port: int

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        """Read-only here: consulted for which node claims an agent."""
        ...

    @property
    def _agent_manifests(self) -> dict[str, dict[str, Any]]:
        """Read-only here: says whether a name is a catalogue recipe."""
        ...

    async def send(self, target_id: str, msg_type: Any, payload: dict[str, Any]) -> bool: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = ..., qos: int = ...
    ) -> None: ...

    async def _resolve_or_spawn(self, agent_name: str) -> tuple[Any, bool]: ...


class NodeHost(ListenerHost, Protocol):
    """What the node collaborator needs beyond a connection.

    All of it serves one job: an agent that stops appearing in a node's
    heartbeats has to be cleaned up as thoroughly as a deliberate delete, minus
    the stop signal there is nothing left to send.
    """

    _registry: Any

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]: ...

    def _remove_from_spawn_registry(self, name: str) -> None: ...

    def _record_agent_deletion(self, name: str, reason: str = ...) -> None: ...

    def _queue_notification(self, notice: dict[str, Any]) -> None: ...

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = ...) -> None: ...


class LifecycleHost(Protocol):
    """What deleting an agent needs from the actor.

    Reads the three places an agent can be known from — the live registry, the
    manifest cache, the spawn registry — because a delete has to clear all of
    them, and a name present in only one is still worth deleting.
    """

    name: str
    _registry: Any

    @property
    def _agent_manifests(self) -> dict[str, dict[str, Any]]:
        """Read-write here: an entry is dropped when its agent is deleted."""
        ...

    @property
    def _topic_registry(self) -> dict[str, list[dict[str, Any]]]:
        """Read-write here: the deleted agent stops claiming its topics."""
        ...

    _conversation_history: list[dict[str, Any]]

    def persist(self, key: str, value: Any) -> None: ...

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]: ...

    def _remove_from_spawn_registry(self, name: str) -> None: ...

    def _node_running_agent(self, name: str) -> str: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = ..., qos: int = ...
    ) -> None: ...

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = ..., remove_name: str | None = ...
    ) -> None: ...


class NodeReaders(Protocol):
    """The live node view a migration consults.

    Named separately from the host because it is a different object: the node
    collaborator, not the actor. A migration asks it two things — whether the
    destination is up, and which node currently runs an agent — and both are
    answered from the heartbeat table it owns.
    """

    known: dict[str, dict[str, Any]]

    def running_agent(self, name: str) -> str: ...


class MigrationHost(NodeHost, Protocol):
    """What moving an agent between machines needs.

    Extends `NodeHost` because a migration reads the same spawn registry and
    reports through the same notifications, and adds the machinery for putting
    an agent somewhere: spawning it here or there, and rewriting what each side
    believes it should be running.

    The four node readers are listed rather than reached through the node
    collaborator. Migration is held by that collaborator, so calling back into
    it would make the two mutually dependent for the sake of four questions the
    owning actor can already answer.
    """

    @property
    def _agent_manifests(self) -> dict[str, dict[str, Any]]:
        """Read-only here: the actor owns this and hands it out."""
        ...

    def _node_is_online(self, node_name: str) -> bool: ...

    def _online_node_names(self) -> list[str]: ...

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None: ...

    def _inject_llm_bridge_code(self, config: dict[str, Any]) -> dict[str, Any]: ...

    def _restore_earned_trust(self, agent_name: str, local_cfg: dict[str, Any]) -> bool: ...

    async def _purge_local_agent_persistence(self, actor: Any, name: str) -> None: ...

    async def _spawn_remote(self, config: dict[str, Any], node: str, save: bool) -> None: ...

    async def _spawn_from_config(
        self, config: dict[str, Any], save: bool = ..., *, from_registry: bool = ...
    ) -> Any: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = ..., qos: int = ...
    ) -> None: ...


class RoutingHost(LLMHost, Protocol):
    """What `RoutingMixin` needs: the LLM surface plus in-flight task futures."""

    _result_futures: dict[str, asyncio.Future]


class MemoryHost(LLMHost, Protocol):
    """What `MemoryMixin` needs — nothing beyond the LLM host.

    Named anyway rather than reusing `LLMHost` directly: the mixin should say
    what it depends on, and if that grows the change belongs here where it is
    visible.
    """


class PlanningHost(ActorHost, Protocol):
    """What `PlanningMixin` needs — and it is the widest of the four.

    Two things fall out of this list and neither is incidental:

    - **`get_user_facts` comes from `MemoryMixin`**, not from any base. Planning
      depends on a sibling mixin, so the two cannot be composed independently.
      That coupling was real, undocumented, and only discoverable by running it.
    - **The last four are `MainActor`'s own methods.** This mixin is not merely
      "an LLMAgent host" as its docstring says; it requires `MainActor`
      specifically, which is why it cannot be reused elsewhere as written.
    """

    llm: LLMProvider | None
    _result_futures: dict[str, asyncio.Future]
    _conversation_history: list[dict]

    def get_user_facts(self) -> dict: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...

    def _remove_from_spawn_registry(self, name: str) -> None: ...

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None: ...

    def _record_agent_deletion(self, name: str, reason: str = "user request") -> None: ...
