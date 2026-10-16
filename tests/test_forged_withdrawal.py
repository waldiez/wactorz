"""A withdrawal is taken only where the agent could have sent it.

An empty retained manifest says an agent is gone, and main then removes it from
its spawn registry and its node's desired state. Every node may write
``agents/#``, and the message carries nothing that says who sent it, so a node
could make main forget any agent, here or on another node. What main can trust
is where the agent runs: its own actor registry for an agent here, and for one
on a node that node's heartbeat, which only the node can publish.

So a withdrawal for an agent still running here is refused: an agent leaves
main's registry before it withdraws. One for an agent its node still reports
is held until that node's next heartbeat, and taken if the agent is gone from
it, or dropped if the node still runs it.
"""

from typing import Any, cast

from wactorz.agents.main.hosts import NodeHost
from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import derive_actor_id


class _Actors:
    """Main's actor registry, holding the agents running here."""

    def __init__(self, running: tuple[str, ...] = ()) -> None:
        self.running = set(running)

    def find_by_name(self, name: str) -> object | None:
        return object() if name in self.running else None


class Host:
    """The parts of main a withdrawal touches."""

    def __init__(
        self,
        registry: dict[str, dict[str, Any]],
        *,
        running_here: tuple[str, ...] = (),
        #: agent -> the node whose latest heartbeat lists it, fresh or not.
        on_nodes: dict[str, str] | None = None,
    ) -> None:
        self.name = "main"
        self._registry = _Actors(running_here)
        self._spawn_registry = registry
        self._on_nodes = dict(on_nodes or {})
        self._conversation_history: list[dict[str, Any]] = []
        self._agent_manifests: dict[str, dict[str, Any]] = {}
        self._topic_registry: dict[str, list[dict[str, Any]]] = {}
        self.desired_state_calls: list[tuple[str, str | None]] = []
        self.published: list[str] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self._spawn_registry

    def _remove_from_spawn_registry(self, name: str) -> None:
        self._spawn_registry.pop(name, None)

    def _node_last_listing(self, name: str) -> str:
        return self._on_nodes.get(name, "")

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        self.desired_state_calls.append((node, remove_name))

    async def _mqtt_publish(self, topic: str, payload: Any, **_: Any) -> None:
        self.published.append(topic)

    def persist(self, key: str, value: Any) -> None:
        pass


def _main(host: Host) -> tuple[LifecycleService, ManifestRegistry]:
    lifecycle = LifecycleService(host)  # pyright: ignore[reportArgumentType]

    class _ManifestHost:
        async def agent_withdrew(self, actor_id: str, name: str = "") -> bool:
            return await lifecycle.agent_withdrew(actor_id, name)

    return lifecycle, ManifestRegistry(_ManifestHost())  # pyright: ignore[reportArgumentType]


def _tombstone(name: str) -> tuple[str, bytes]:
    return f"agents/{derive_actor_id(name)}/manifest", b""


class TestAnAgentHere:
    async def test_a_withdrawal_while_it_runs_is_refused(self) -> None:
        host = Host({"worker": {"code": "..."}}, running_here=("worker",))
        _lifecycle, manifests = _main(host)
        manifests.manifests["worker"] = {"name": "worker", "actor_id": derive_actor_id("worker")}

        await manifests.receive_manifest(*_tombstone("worker"))

        assert "worker" in host._spawn_registry
        # And what main knows of it stays: its capabilities are still offered.
        assert "worker" in manifests.manifests

    async def test_one_it_sent_on_ending_is_taken(self) -> None:
        # An agent that ends itself leaves main's registry before it withdraws.
        host = Host({"worker": {"code": "..."}})
        _lifecycle, manifests = _main(host)

        await manifests.receive_manifest(*_tombstone("worker"))

        assert "worker" not in host._spawn_registry


class TestAnAgentOnANode:
    @staticmethod
    def _host() -> Host:
        return Host({"collector": {"code": "...", "node": "rpi"}}, on_nodes={"collector": "rpi"})

    async def test_a_withdrawal_its_node_still_contradicts_waits(self) -> None:
        host = self._host()
        _lifecycle, manifests = _main(host)
        manifests.manifests["collector"] = {"name": "collector"}

        await manifests.receive_manifest(*_tombstone("collector"))

        assert "collector" in host._spawn_registry
        assert "collector" in manifests.manifests
        assert not host.desired_state_calls

    async def test_and_is_dropped_when_the_next_heartbeat_still_lists_it(self) -> None:
        host = self._host()
        lifecycle, manifests = _main(host)
        await manifests.receive_manifest(*_tombstone("collector"))

        await lifecycle.settle_withdrawals("rpi", {"collector"})

        assert "collector" in host._spawn_registry
        assert not host.desired_state_calls

    async def test_and_is_taken_when_the_next_heartbeat_does_not(self) -> None:
        # The agent ended itself on the node: its heartbeat is a moment behind.
        host = self._host()
        lifecycle, manifests = _main(host)
        await manifests.receive_manifest(*_tombstone("collector"))

        await lifecycle.settle_withdrawals("rpi", set())

        assert "collector" not in host._spawn_registry
        assert host.desired_state_calls == [("rpi", "collector")]

    async def test_another_nodes_heartbeat_settles_nothing(self) -> None:
        host = self._host()
        lifecycle, manifests = _main(host)
        await manifests.receive_manifest(*_tombstone("collector"))

        await lifecycle.settle_withdrawals("other", set())

        assert "collector" in host._spawn_registry

    async def test_one_its_quiet_node_last_listed_waits_too(self) -> None:
        # Quiet but not yet forgotten: an outage must not open the door. Once
        # main forgets the node, it removes the node's agents anyway.
        host = self._host()
        lifecycle, manifests = _main(host)

        await manifests.receive_manifest(*_tombstone("collector"))
        assert "collector" in host._spawn_registry

        await lifecycle.settle_withdrawals("rpi", set())
        assert "collector" not in host._spawn_registry

    async def test_one_its_node_no_longer_reports_is_taken_at_once(self) -> None:
        host = Host({"collector": {"code": "...", "node": "rpi"}})
        _lifecycle, manifests = _main(host)

        await manifests.receive_manifest(*_tombstone("collector"))

        assert "collector" not in host._spawn_registry
        assert host.desired_state_calls == [("rpi", "collector")]


class _NodeHost:
    """What a heartbeat touches, and the withdrawals it is asked to settle."""

    name = "main"
    _registry = None

    def __init__(self) -> None:
        self.settled: list[tuple[str, set[str]]] = []
        self.nodes: NodeManager | None = None
        self.listed_when_settled: list[list[str]] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return {}

    def _remove_from_spawn_registry(self, name: str) -> None:
        pass

    async def _update_node_desired_state(self, node: str, **_: Any) -> None:
        pass

    async def _mqtt_publish(self, topic: str, payload: Any, **_: Any) -> None:
        pass

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        pass

    def _record_agent_deletion(self, name: str, reason: str = "") -> None:
        pass

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        pass

    async def settle_withdrawals(self, node: str, running: set[str]) -> None:
        self.settled.append((node, running))
        assert self.nodes is not None
        listed = self.nodes.known.get(node)
        self.listed_when_settled.append(listed["agents"] if listed else [])


class TestTheNextHeartbeat:
    async def test_it_settles_what_was_held_by_what_it_carries(self) -> None:
        # Decided by this heartbeat, not the one that made the withdrawal wait:
        # the node table already says what the node now runs.
        host = _NodeHost()
        nodes = NodeManager(cast("NodeHost", host), ManifestRegistry(cast("Any", host)))
        host.nodes = nodes
        await nodes.receive_heartbeat("rpi", {"agents": ["collector"]})

        await nodes.receive_heartbeat("rpi", {"agents": []})

        assert host.settled[-1] == ("rpi", set())
        assert host.listed_when_settled[-1] == []


class TestANodeMainForgets:
    async def test_what_was_held_for_it_is_settled(self) -> None:
        host = _NodeHost()
        nodes = NodeManager(cast("NodeHost", host), ManifestRegistry(cast("Any", host)))
        host.nodes = nodes
        await nodes.receive_heartbeat("rpi", {"agents": ["collector"]})
        nodes.known["rpi"]["last_seen"] = 0.0

        await nodes.forget_offline_nodes(now=1_000_000.0)

        assert host.settled[-1] == ("rpi", set())

    def test_the_latest_listing_counts_whether_or_not_it_is_fresh(self) -> None:
        nodes = NodeManager()
        nodes.known["rpi"] = {"last_seen": 0.0, "agents": ["collector"]}

        assert nodes.last_listed_on("collector") == "rpi"
        assert nodes.last_listed_on("other") == ""
