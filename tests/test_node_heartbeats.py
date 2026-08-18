"""What a node's heartbeat tells main, and what main does about it.

Every remote node reports itself on `nodes/+/heartbeat`: who it is, which agents
it is running, and some system metrics. Three things happen on each one — the
node table is refreshed, agents that have stopped appearing are pruned, and
agents whose manifest has not arrived yet get a provisional contract so the
planner can see them.

The prune is the part worth care. An agent missing from one heartbeat is a
transient gap, not a death; only a run of consecutive misses counts. Getting
that wrong deletes live agents, which is why the counter is pinned here rather
than left to the number in the source.
"""

import asyncio
import json
import logging
from typing import Any

import pytest

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.migration import Migration
from wactorz.agents.nodes import VANISH_MISS_THRESHOLD, NodeManager
from wactorz.core.actor import ActorState


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self._messages:
            yield message
        self._on_drained()


class _Broker:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self.client = _Client(messages, on_drained)

    def __call__(self, _host: str, _port: int) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _Bus:
    def __init__(self, registered: dict[str, Any] | None = None) -> None:
        self.registry: dict[str, Any] = dict(registered or {})

    def register_contract(self, contract: Any) -> None:
        self.registry[contract.name] = contract


class _Run:
    """One drive of the listener, and everything it reached for."""

    def __init__(self, main: MainActor, bus: _Bus) -> None:
        self.main = main
        self.bus = bus
        self.subscribed: list[str] = []
        self.removed_from_registry: list[str] = []
        self.cleared_manifests: list[str] = []
        self.deletions: list[tuple[str, str]] = []
        self.notifications: list[dict[str, Any]] = []

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return self.main.nodes.known


def heartbeat(node: str = "rpi", **over: Any) -> _Message:
    """A node's heartbeat as its runner publishes it."""
    body: dict[str, Any] = {
        "agents": ["collector"],
        "node_id": f"{node}-id",
        "pid": 4242,
        "uptime_s": 900,
        "cpu_pct": 11.5,
        "mem_used_mb": 300,
        "mem_free_mb": 700,
        **over,
    }
    return _Message(f"nodes/{node}/heartbeat", json.dumps(body).encode())


def migrate_result(**over: Any) -> _Message:
    body: dict[str, Any] = {"success": True, "agent": "collector", "to_node": "rpi-2", **over}
    return _Message("nodes/rpi/migrate_result", json.dumps(body).encode())


async def run_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[_Message],
    *,
    spawn_registry: dict[str, dict[str, Any]] | None = None,
    manifests: dict[str, Any] | None = None,
    bus: _Bus | None = None,
) -> _Run:
    """Drive the real listener over `messages` until they are exhausted."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.migration = Migration(main, main.nodes)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883
    main._node_agent_misses = {}
    main._agent_manifests = dict(manifests or {})
    setattr(main, "_registry", None)

    run = _Run(main, bus or _Bus())

    setattr(main, "_get_spawn_registry", lambda: dict(spawn_registry or {}))
    setattr(main, "_remove_from_spawn_registry", run.removed_from_registry.append)
    setattr(
        main, "_record_agent_deletion", lambda name, reason="": run.deletions.append((name, reason))
    )
    setattr(main, "_queue_notification", run.notifications.append)

    async def _clear(name: str, actor_id: str | None = None) -> None:
        run.cleared_manifests.append(name)

    setattr(main, "_clear_agent_manifest", _clear)

    def _stop() -> None:
        main.state = ActorState.STOPPED

    broker = _Broker(messages, _stop)
    monkeypatch.setattr("wactorz.agents.nodes.mqtt_client", broker)
    monkeypatch.setattr("wactorz.core.topic_bus.get_topic_bus", lambda: run.bus)

    await asyncio.wait_for(main._node_heartbeat_listener(), timeout=5)
    run.subscribed = broker.client.subscribed
    return run


class TestTheNodeTable:
    async def test_a_heartbeat_records_the_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat("rpi")])

        assert "rpi" in run.nodes

    async def test_it_records_the_agents_the_node_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat(agents=["a", "b"])])

        assert run.nodes["rpi"]["agents"] == ["a", "b"]

    async def test_it_records_the_system_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These reach the dashboard, so a dropped key is invisible here and
        # visible there.
        run = await run_heartbeats(monkeypatch, [heartbeat()])

        entry = run.nodes["rpi"]
        assert (entry["pid"], entry["cpu_pct"], entry["mem_free_mb"]) == (4242, 11.5, 700)
        assert entry["node_id"] == "rpi-id"
        assert entry["uptime_s"] == 900

    async def test_a_later_heartbeat_replaces_the_earlier_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat(agents=["a"]), heartbeat(agents=["b"])])

        assert run.nodes["rpi"]["agents"] == ["b"]

    async def test_two_nodes_are_tracked_separately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat("rpi"), heartbeat("nuc")])

        assert set(run.nodes) == {"rpi", "nuc"}

    async def test_it_subscribes_to_both_node_topics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Subscribing to only heartbeats would leave migrations unreported.
        run = await run_heartbeats(monkeypatch, [])

        assert run.subscribed == ["nodes/+/heartbeat", "nodes/+/migrate_result"]


class TestPruningAnAgentThatStoppedAppearing:
    """A single miss is a gap; a run of them is a death.

    Deleting on the first miss removes live agents whenever a heartbeat is
    late, so the threshold is the whole safety of this path.
    """

    @staticmethod
    def _registry() -> dict[str, dict[str, Any]]:
        return {"collector": {"name": "collector", "node": "rpi"}}

    async def test_one_missed_heartbeat_deletes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), heartbeat(agents=[])],
            spawn_registry=self._registry(),
        )

        assert not run.removed_from_registry

    async def test_the_threshold_is_what_triggers_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        absent = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD)]

        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), *absent],
            spawn_registry=self._registry(),
        )

        assert run.removed_from_registry == ["collector"]

    async def test_reappearing_resets_the_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A node that flaps must not accumulate misses towards a deletion.
        misses = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD - 1)]

        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), *misses, heartbeat(agents=["collector"]), *misses],
            spawn_registry=self._registry(),
        )

        assert not run.removed_from_registry

    async def test_an_agent_that_never_appeared_is_never_pruned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Just spawned, first heartbeat still pending: absent, but not lost.
        absent = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD + 2)]

        run = await run_heartbeats(monkeypatch, absent, spawn_registry=self._registry())

        assert not run.removed_from_registry

    async def test_an_agent_the_registry_puts_elsewhere_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Migrated away: absent from this node because it lives on another one.
        absent = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD + 2)]

        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), *absent],
            spawn_registry={"collector": {"node": "other-node"}},
        )

        assert not run.removed_from_registry

    async def test_a_prune_clears_every_trace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        absent = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD)]

        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), *absent],
            spawn_registry=self._registry(),
        )

        assert run.removed_from_registry == ["collector"]
        assert run.cleared_manifests == ["collector"]
        assert [name for name, _ in run.deletions] == ["collector"]

    async def test_the_deletion_note_says_why(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The note goes into conversation history so the model does not keep
        # believing an agent it spawned is still there.
        absent = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD)]

        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), *absent],
            spawn_registry=self._registry(),
        )

        assert "rpi" in run.deletions[0][1]

    async def test_it_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        absent = [heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD)]

        with caplog.at_level(logging.WARNING, logger="wactorz.agents.nodes"):
            await run_heartbeats(
                monkeypatch,
                [heartbeat(agents=["collector"]), *absent],
                spawn_registry=self._registry(),
            )

        assert "collector" in caplog.text


class TestTheProvisionalContract:
    """A heartbeat can arrive before the agent's retained manifest does.

    The manifest is authoritative — it carries the real topics and schemas — so
    this only fills a gap, and only where there is nothing better.
    """

    @staticmethod
    def _registry() -> dict[str, dict[str, Any]]:
        # A registry entry carries its own name; the contract is built from it.
        return {
            "collector": {
                "name": "collector",
                "node": "rpi",
                "publishes": ["sensors/x"],
                "subscribes": [],
            }
        }

    async def test_a_spawn_config_with_topics_is_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat()], spawn_registry=self._registry())

        assert "collector" in run.bus.registry

    async def test_an_existing_contract_is_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The manifest listener already registered the real thing; the spawn
        # config only says what was asked for, which may never have been true.
        real = _Bus({"collector": "from-the-manifest"})

        run = await run_heartbeats(
            monkeypatch, [heartbeat()], spawn_registry=self._registry(), bus=real
        )

        assert run.bus.registry["collector"] == "from-the-manifest"

    async def test_a_cached_manifest_means_the_listener_is_about_to_register(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(
            monkeypatch,
            [heartbeat()],
            spawn_registry=self._registry(),
            manifests={"collector": {"name": "collector"}},
        )

        assert not run.bus.registry

    async def test_an_agent_with_no_spawn_config_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat()], spawn_registry={})

        assert not run.bus.registry

    async def test_a_config_declaring_no_topics_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty contract tells the planner nothing and only clutters the bus.
        run = await run_heartbeats(
            monkeypatch,
            [heartbeat()],
            spawn_registry={"collector": {"name": "collector", "node": "rpi"}},
        )

        assert not run.bus.registry


class TestReportingAMigration:
    async def test_a_success_is_announced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [migrate_result(success=True)])

        assert run.notifications[0]["severity"] == "info"
        assert "succeeded" in run.notifications[0]["message"]

    async def test_a_failure_is_announced_with_its_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(
            monkeypatch, [migrate_result(success=False, error="no route to host")]
        )

        assert run.notifications[0]["severity"] == "warning"
        assert "no route to host" in run.notifications[0]["message"]

    async def test_it_names_the_agent_and_the_destination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [migrate_result()])

        assert "collector" in run.notifications[0]["message"]
        assert "rpi-2" in run.notifications[0]["message"]


class TestMessagesThatCannotBeUsed:
    @pytest.mark.parametrize("payload", [b"{not json", b"", b"[1, 2]"])
    async def test_they_are_ignored(self, monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
        run = await run_heartbeats(monkeypatch, [_Message("nodes/rpi/heartbeat", payload)])

        assert not run.nodes

    async def test_a_topic_with_too_few_parts_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [_Message("heartbeat", b"{}")])

        assert not run.nodes

    async def test_a_later_good_heartbeat_is_still_taken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(
            monkeypatch, [_Message("nodes/rpi/heartbeat", b"{not json"), heartbeat()]
        )

        assert "rpi" in run.nodes
