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

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import VANISH_MISS_THRESHOLD, NodeManager
from wactorz.agents.main.placement import Placement
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.actor import ActorState
from wactorz.core.mqtt import publish_properties
from wactorz.core.node_publications import PublicationSigner
from wactorz.core.node_signing import node_key

from .node_messages import SignedMessage


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
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

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _Contracts(dict[str, Any]):
    """The bus's registry, as the heartbeat reads it: one agent per name."""

    def at(self, name: str, _node: str) -> Any | None:
        return self.get(name)


class _Bus:
    def __init__(self, registered: dict[str, Any] | None = None) -> None:
        self.registry = _Contracts(registered or {})

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
        #: (node, agent) the listener asked to drop from a node's desired state.
        self.desired_state_drops: list[tuple[str, str]] = []

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return self.main.nodes.known


def heartbeat(node: str = "rpi", **over: Any) -> SignedMessage:
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
    return SignedMessage(f"nodes/{node}/heartbeat", json.dumps(body).encode())


def migrate_result(**over: Any) -> SignedMessage:
    body: dict[str, Any] = {"success": True, "agent": "collector", "to_node": "rpi-2", **over}
    return SignedMessage("nodes/rpi/migrate_result", json.dumps(body).encode())


async def run_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[Any],
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
    main.lifecycle = LifecycleService(main)
    main.placement = Placement(main)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883
    main._node_agent_misses = {}
    main._agent_manifests = dict(manifests or {})
    setattr(main, "_registry", None)

    run = _Run(main, bus or _Bus())

    setattr(
        main, "_get_spawn_registry", lambda: SpawnRegistry.from_names(dict(spawn_registry or {}))
    )
    setattr(
        main,
        "_remove_from_spawn_registry",
        lambda name, *_, **__: run.removed_from_registry.append(name),
    )
    setattr(
        main, "_record_agent_deletion", lambda name, reason="": run.deletions.append((name, reason))
    )
    setattr(main, "_queue_notification", run.notifications.append)

    def _withdraw(about: str) -> None:
        run.notifications[:] = [n for n in run.notifications if n.get("about") != about]

    setattr(main, "_withdraw_notifications", _withdraw)

    async def _clear(name: str, actor_id: str | None = None) -> None:
        run.cleared_manifests.append(name)

    setattr(main, "_clear_agent_manifest", _clear)

    async def _drop_from_desired_state(
        node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        if remove_name:
            run.desired_state_drops.append((node, remove_name))

    setattr(main, "_update_node_desired_state", _drop_from_desired_state)

    def _stop() -> None:
        main.state = ActorState.STOPPED

    broker = _Broker(messages, _stop)
    monkeypatch.setattr("wactorz.agents.main.nodes.mqtt_client", broker)
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

    async def test_it_records_how_close_the_node_is_to_running_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        readings = {
            "swap_used_mb": 12,
            "load_1m": 0.5,
            "load_5m": 0.25,
            "disk_free_mb": 3000,
            "temp_c": 61.2,
            "throttled": ["under_voltage"],
        }
        run = await run_heartbeats(monkeypatch, [heartbeat(**readings)])

        entry = run.nodes["rpi"]
        assert {key: entry[key] for key in readings} == readings

    async def test_a_runner_older_than_the_readings_leaves_them_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unknown, not zero: admission would read zero as nothing left.
        run = await run_heartbeats(monkeypatch, [heartbeat()])

        entry = run.nodes["rpi"]
        assert (entry["disk_free_mb"], entry["load_1m"], entry["throttled"]) == (None, None, None)

    async def test_a_reading_that_is_not_one_is_left_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(disk_free_mb="lots", temp_c=True, throttled="under_voltage")],
        )

        entry = run.nodes["rpi"]
        assert (entry["disk_free_mb"], entry["temp_c"], entry["throttled"]) == (None, None, None)

    async def test_a_later_heartbeat_replaces_the_earlier_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat(agents=["a"]), heartbeat(agents=["b"])])

        assert run.nodes["rpi"]["agents"] == ["b"]

    async def test_two_nodes_are_tracked_separately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat("rpi"), heartbeat("nuc")])

        assert set(run.nodes) == {"rpi", "nuc"}

    async def test_it_subscribes_to_every_node_topic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Subscribing to only heartbeats would leave migrations unreported.
        run = await run_heartbeats(monkeypatch, [])

        assert run.subscribed == ["nodes/+/heartbeat", "nodes/+/migrate_result", "nodes/+/manifest"]


def manifest(node: str = "rpi", payload: bytes | None = None, **over: Any) -> SignedMessage:
    """A node's retained manifest, or with ``payload=b""`` the clearing of it."""
    body = {"node": node, "manifest_v": 1, "arch": "aarch64", "ram_total_mb": 8064, **over}
    return SignedMessage(
        f"nodes/{node}/manifest", json.dumps(body).encode() if payload is None else payload
    )


class TestTheMachineANodeRunsOn:
    async def test_its_manifest_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [manifest()])

        assert run.main.nodes.node_manifests["rpi"]["ram_total_mb"] == 8064

    async def test_one_without_a_version_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [manifest(manifest_v=None)])

        assert "rpi" not in run.main.nodes.node_manifests

    async def test_a_cleared_manifest_is_forgotten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [manifest(), manifest(payload=b"")])

        assert "rpi" not in run.main.nodes.node_manifests

    async def test_a_later_manifest_replaces_the_earlier_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [manifest(), manifest(ram_total_mb=4096)])

        assert run.main.nodes.node_manifests["rpi"]["ram_total_mb"] == 4096


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
        # The retained desired_state is the fourth trace, and the one a reboot
        # reads: leaving it names an agent main has just forgotten, so the node
        # starts it again into a main with no registry entry for it.
        assert run.desired_state_drops == [("rpi", "collector")]

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

        with caplog.at_level(logging.WARNING, logger="wactorz.agents.main.nodes"):
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
    async def test_a_success_is_left_to_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The node reports one when it has handed the agent over, before it is
        # running anywhere; main announces the migration once it is.
        run = await run_heartbeats(monkeypatch, [migrate_result(success=True)])

        assert not run.notifications

    async def test_a_failure_is_announced_with_its_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(
            monkeypatch, [migrate_result(success=False, error="no route to host")]
        )

        assert run.notifications[0]["severity"] == "warning"
        assert "no route to host" in run.notifications[0]["message"]

    async def test_a_failure_names_the_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(monkeypatch, [migrate_result(success=False, error="x")])

        assert "collector" in run.notifications[0]["message"]


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


def unsigned_heartbeat(node: str = "rpi", **over: Any) -> _Message:
    """A heartbeat with no signature: from a node deployed before nodes signed, or a forgery."""
    signed = heartbeat(node, **over)
    return _Message(signed.topic, signed.payload)


def _signing(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("wactorz.agents.main.nodes.NODE_SIGNING", mode)
    monkeypatch.setattr("wactorz.core.node_publications.NODE_SIGNING", mode)


class TestASignedHeartbeat:
    async def test_it_is_taken_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Delivered again -- replayed by something that read it -- it would mark
        # a node that has since died as seen just now.
        first = heartbeat(agents=["collector"])
        replay = SignedMessage(first.topic, json.dumps({"agents": []}).encode())
        replay._properties = first.properties

        run = await run_heartbeats(monkeypatch, [first, replay])

        assert run.nodes["rpi"]["agents"] == ["collector"]

    async def test_one_from_a_new_run_of_the_runner_is_taken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A restarted runner starts counting again under a new run id.
        before = heartbeat(agents=["old"])
        after = SignedMessage(before.topic, json.dumps({"agents": ["new"]}).encode())
        after._properties = publish_properties(
            PublicationSigner(bytes.fromhex(node_key("rpi"))).properties(after.topic, after.payload)
        )

        run = await run_heartbeats(monkeypatch, [before, after])

        assert run.nodes["rpi"]["agents"] == ["new"]

    async def test_one_signed_by_another_node_is_not_signed_for_this_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "enforce")
        forged = SignedMessage(
            "nodes/rpi/heartbeat", json.dumps({"agents": ["camera"]}).encode(), node="sandbox"
        )

        run = await run_heartbeats(monkeypatch, [forged])

        assert run.nodes["rpi"]["agents"] == []
        assert run.nodes["rpi"]["signed"] is False


class TestAnUnsignedHeartbeat:
    """Under enforce, evidence the node is up and nothing more."""

    async def test_the_node_counts_as_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _signing(monkeypatch, "enforce")

        run = await run_heartbeats(monkeypatch, [unsigned_heartbeat()])

        assert run.main.nodes.is_online("rpi")

    async def test_what_it_says_about_agents_is_not_taken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "enforce")

        run = await run_heartbeats(monkeypatch, [unsigned_heartbeat(agents=["camera"])])

        assert run.nodes["rpi"]["agents"] == []

    async def test_it_prunes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Refused outright, the node would read as silent, and a silent node's
        # agents are deleted: a node that only needs deploying again would lose
        # them. Taken whole, a forgery could have them pruned.
        _signing(monkeypatch, "enforce")
        absent = [unsigned_heartbeat(agents=[]) for _ in range(VANISH_MISS_THRESHOLD + 1)]

        run = await run_heartbeats(
            monkeypatch,
            [heartbeat(agents=["collector"]), *absent],
            spawn_registry={"collector": {"node": "rpi"}},
        )

        assert not run.removed_from_registry

    async def test_the_chat_is_told_once_to_deploy_the_node_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "enforce")

        run = await run_heartbeats(monkeypatch, [unsigned_heartbeat(), unsigned_heartbeat()])

        (notice,) = run.notifications
        assert "/deploy rpi" in notice["message"]

    async def test_tasks_for_the_node_are_refused_with_the_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "enforce")

        run = await run_heartbeats(monkeypatch, [unsigned_heartbeat()])

        refusal = run.main.nodes.refuses_tasks("rpi")
        assert refusal is not None and "/deploy rpi" in refusal

    async def test_the_notice_is_withdrawn_once_the_node_signs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deployed again before anyone chatted: the queued notice would reach
        # the chat after the deploy that made it untrue.
        _signing(monkeypatch, "enforce")

        run = await run_heartbeats(monkeypatch, [unsigned_heartbeat(), heartbeat()])

        assert run.notifications == []
        assert run.main.nodes.refuses_tasks("rpi") is None

    async def test_one_after_a_signed_one_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The node signs, so this came from something else, which must not be
        # able to make main refuse the node's tasks.
        _signing(monkeypatch, "enforce")

        run = await run_heartbeats(
            monkeypatch, [heartbeat(agents=["collector"]), unsigned_heartbeat(agents=[])]
        )

        assert run.nodes["rpi"]["agents"] == ["collector"]
        assert run.main.nodes.refuses_tasks("rpi") is None
        assert not run.notifications

    async def test_under_warn_it_is_taken_and_the_chat_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "warn")

        run = await run_heartbeats(monkeypatch, [unsigned_heartbeat(agents=["camera"])])

        assert run.nodes["rpi"]["agents"] == ["camera"]
        assert run.main.nodes.refuses_tasks("rpi") is None
        assert "WACTORZ_NODE_SIGNING=warn" in run.notifications[0]["message"]


class TestAnUnsignedReport:
    async def test_a_machine_manifest_is_refused_under_enforce(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "enforce")
        signed = manifest()

        run = await run_heartbeats(monkeypatch, [_Message(signed.topic, signed.payload)])

        assert "rpi" not in run.main.nodes.node_manifests

    async def test_a_machine_manifest_is_taken_under_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _signing(monkeypatch, "warn")
        signed = manifest()

        run = await run_heartbeats(monkeypatch, [_Message(signed.topic, signed.payload)])

        assert "rpi" in run.main.nodes.node_manifests


class TestWhereATaskGoes:
    async def test_a_node_listing_an_agent_main_places_elsewhere_is_not_sent_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another node claiming the Pi's camera agent, to be sent its tasks.
        run = await run_heartbeats(
            monkeypatch,
            [heartbeat("pc", agents=["camera"]), heartbeat("rpi", agents=["camera"])],
            spawn_registry={"camera": {"node": "rpi"}},
        )

        assert run.main.nodes.nodes_for("camera") == ["rpi"]

    async def test_an_agent_main_placed_nowhere_goes_to_the_node_listing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_heartbeats(monkeypatch, [heartbeat("rpi", agents=["local-only"])])

        assert run.main.nodes.nodes_for("local-only") == ["rpi"]

    async def test_an_agent_on_main_goes_to_no_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_heartbeats(
            monkeypatch,
            [heartbeat("pc", agents=["planner"])],
            spawn_registry={"planner": {"node": ""}},
        )

        assert run.main.nodes.nodes_for("planner") == []


class TestWithdrawingANotice:
    def test_only_the_notices_about_it_go(self) -> None:
        main = MainActor.__new__(MainActor)
        main._pending_notifications = [
            {"about": "unsigned-node:rpi", "message": "rpi does not sign"},
            {"about": "unsigned-node:pc", "message": "pc does not sign"},
            {"message": "something else"},
        ]

        main._withdraw_notifications("unsigned-node:rpi")

        assert [n["message"] for n in main._pending_notifications] == [
            "pc does not sign",
            "something else",
        ]
