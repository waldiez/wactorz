# SPDX-License-Identifier: Apache-2.0
"""An agent's card goes only when its withdrawal stands, by main's rule.

A withdrawal is an empty payload on ``agents/<id>/manifest``, which anything
that can publish under ``agents/`` can send. Main weighs it by where the agent
runs (`LifecycleService.withdrawal_decision`): refused for an agent running in
its process, main itself included, which also puts the manifest back; held for
one its node still lists; standing otherwise. The dashboard follows that
decision rather than the payload: main tells it of every withdrawal that
stands, at once or when it settles, and only then does the card go, once.
While main is not there, a withdrawal is held and handed to main when it is
back. Only the profile without main takes the rest as they come.

Main here is its real lifecycle and manifest registry over a fake of the rest,
as in `test_forged_withdrawal`; the dashboard is the real web server's handling.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from wactorz.agents.main.lifecycle import KEPT, LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.spawn_registry import SpawnRegistry, with_actor_id
from wactorz.core.actor import derive_actor_id
from wactorz.web import events, mqtt, questions, runtime, withdrawals


class _Agent:
    """An agent running in this process, with the manifest it last sent."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = derive_actor_id(name)
        self._published_manifest: dict[str, Any] = {"name": name, "actor_id": self.actor_id}
        self.sent: list[dict[str, Any]] = []

    async def _send_manifest(self, manifest: dict[str, Any]) -> None:
        self.sent.append(manifest)

    def questions(self) -> dict[str, Any]:
        return {}


class _Live:
    """The live registry: the agents running here."""

    def __init__(self, *agents: _Agent) -> None:
        self.agents = {a.actor_id: a for a in agents}

    def get(self, actor_id: str) -> _Agent | None:
        return self.agents.get(actor_id)

    def find_by_name(self, name: str) -> _Agent | None:
        return next((a for a in self.agents.values() if a.name == name), None)

    def all_actors(self) -> list[_Agent]:
        return list(self.agents.values())


class _Host:
    """The parts of main a withdrawal touches."""

    def __init__(self, live: _Live, spawned: dict[str, dict[str, Any]]) -> None:
        self.name = "main"
        self._registry = live
        self._spawn_registry = spawned
        #: The ids of the agents on nodes main keeps stopped.
        self.stopped: list[str] = []
        self.listing: dict[str, str] = {}
        self._conversation_history: list[dict[str, Any]] = []
        self._agent_manifests: dict[str, dict[str, Any]] = {}
        self._topic_registry: dict[str, list[dict[str, Any]]] = {}
        self.published: list[tuple[str, Any]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        by_name = {name: with_actor_id(name, cfg) for name, cfg in self._spawn_registry.items()}
        return SpawnRegistry.stored(None, by_name, self.stopped)

    def _remove_from_spawn_registry(
        self, name: str, node: str | None = None, *, actor_id: str | None = None
    ) -> None:
        self._spawn_registry.pop(name, None)

    def _node_last_listing(self, name: str) -> str:
        return self.listing.get(name, "")

    async def _update_node_desired_state(self, node: str, **_: Any) -> None:
        return None

    async def _mqtt_publish(self, topic: str, payload: Any, **_: Any) -> None:
        self.published.append((topic, payload))

    def persist(self, key: str, value: Any) -> None:
        return None


class _Main:
    """Main as the dashboard reaches it: its lifecycle and its manifest registry."""

    def __init__(self, host: _Host) -> None:
        self.name = "main"
        self.host = host
        self.lifecycle = LifecycleService(host)  # pyright: ignore[reportArgumentType]  # _Host stands in for main
        self.manifests = ManifestRegistry(self)  # pyright: ignore[reportArgumentType]  # _Main stands in for main

    async def agent_withdrew(self, actor_id: str, name: str = "") -> bool:
        return await self.lifecycle.agent_withdrew(actor_id, name)

    async def hears(self, agent_id: str) -> None:
        """Main's own listener taking the withdrawal off the broker."""
        await self.manifests.receive_manifest(f"agents/{agent_id}/manifest", b"")


class _Setup:
    def __init__(self) -> None:
        self.main_agent = _Agent("main")
        self.worker = _Agent("worker")
        self.live = _Live(self.main_agent, self.worker)
        self.host = _Host(
            self.live, {"worker": {"code": "..."}, "cam": {"code": "...", "node": "rpi"}}
        )
        self.main: _Main | None = _Main(self.host)
        self.frames: list[dict[str, Any]] = []

    def card(self, name: str) -> str:
        agent_id = derive_actor_id(name)
        runtime.state["agents"][agent_id] = {"name": name, "state": "running"}
        return agent_id

    def deleted(self) -> list[str]:
        return [f["agent_id"] for f in self.frames if f.get("type") == events.DELETE_AGENT_FRAME]


@pytest.fixture(name="web")
def web_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Setup]:
    setup = _Setup()
    monkeypatch.setattr(
        runtime,
        "state",
        {"agents": {}, "nodes": {}, "alerts": [], "system_health": {}, "log_feed": []},
    )
    monkeypatch.setattr(runtime, "registry", setup.live)
    monkeypatch.setattr(runtime, "db", None)
    monkeypatch.setattr(runtime, "started_with_main", True)
    for module in (mqtt, withdrawals, questions):
        monkeypatch.setattr(module, "find_main_actor", lambda _registry: setup.main)

    async def record(frame: dict[str, Any]) -> None:
        setup.frames.append(frame)

    with (
        patch.object(mqtt.ws, "broadcast", new=record),
        patch.object(withdrawals.ws, "broadcast", new=record),
    ):
        yield setup


async def _forged(agent_id: str) -> None:
    """A withdrawal reaching the dashboard's broker loop."""
    await mqtt.handle_message(f"agents/{agent_id}/manifest", "", raw=b"", properties={})


class TestAnAgentRunningHere:
    async def test_mains_own_withdrawal_is_refused_and_its_manifest_put_back(
        self, web: _Setup
    ) -> None:
        main_id = web.card("main")
        assert web.main is not None

        await _forged(main_id)
        await web.main.hears(main_id)

        assert main_id in runtime.state["agents"]
        assert web.deleted() == []
        assert web.main_agent.sent == [web.main_agent._published_manifest]

    async def test_a_spawned_agents_forged_withdrawal_is_refused(self, web: _Setup) -> None:
        worker_id = web.card("worker")
        assert web.main is not None

        await _forged(worker_id)
        await web.main.hears(worker_id)

        assert worker_id in runtime.state["agents"]
        assert "worker" in web.host._spawn_registry
        assert web.worker.sent

    async def test_one_that_ended_itself_goes_once(self, web: _Setup) -> None:
        worker_id = web.card("worker")
        assert web.main is not None
        del web.live.agents[worker_id]  # an agent leaves the registry before it withdraws

        await _forged(worker_id)
        await web.main.hears(worker_id)

        assert worker_id not in runtime.state["agents"]
        assert web.deleted() == [worker_id]


class TestAnAgentOnANode:
    async def test_one_its_node_still_lists_stays_until_the_node_stops_listing_it(
        self, web: _Setup
    ) -> None:
        cam_id = web.card("cam")
        assert web.main is not None
        web.host.listing = {"cam": "rpi"}

        await _forged(cam_id)
        await web.main.hears(cam_id)
        assert cam_id in runtime.state["agents"]

        await web.main.lifecycle.settle_withdrawals("rpi", set())
        # Main clears the retained manifest as it settles, and hears that too.
        await web.main.hears(cam_id)

        assert cam_id not in runtime.state["agents"]
        assert web.deleted() == [cam_id]

    async def test_main_deciding_after_the_node_stopped_listing_it_takes_the_card(
        self, web: _Setup
    ) -> None:
        # The dashboard hears the withdrawal while the node still lists the
        # agent; a heartbeat without it comes; only then does main decide.
        cam_id = web.card("cam")
        assert web.main is not None
        web.host.listing = {"cam": "rpi"}

        await _forged(cam_id)
        web.host.listing = {}
        await web.main.hears(cam_id)

        assert cam_id not in runtime.state["agents"]
        assert web.deleted() == [cam_id]

    @pytest.mark.parametrize("listing", [{}, {"cam": "rpi"}])
    async def test_one_kept_stopped_keeps_its_card_and_its_entry(
        self, web: _Setup, listing: dict[str, str]
    ) -> None:
        # Stopping is what made it withdraw: it went quiet, and is not gone.
        # Whether or not its node's last heartbeat still listed it.
        cam_id = web.card("cam")
        assert web.main is not None
        web.host.stopped = [cam_id]
        web.host.listing = listing
        told: list[str] = []

        async def _watcher(actor_id: str) -> None:
            told.append(actor_id)

        web.main.lifecycle.watchers.append(_watcher)

        await _forged(cam_id)
        await web.main.hears(cam_id)
        await web.main.lifecycle.settle_withdrawals("rpi", set())

        assert web.main.lifecycle.withdrawal_decision(cam_id).verdict == KEPT
        assert told == []
        assert web.deleted() == []
        assert cam_id in runtime.state["agents"]
        assert "cam" in web.host._spawn_registry

    async def test_one_stopped_while_its_withdrawal_waited_keeps_its_card(
        self, web: _Setup
    ) -> None:
        cam_id = web.card("cam")
        assert web.main is not None
        web.host.listing = {"cam": "rpi"}
        await _forged(cam_id)
        await web.main.hears(cam_id)
        # Held for the node's next heartbeat; stopped before it comes.
        web.host.stopped = [cam_id]

        await web.main.lifecycle.settle_withdrawals("rpi", set())
        await web.main.hears(cam_id)

        assert web.deleted() == []
        assert cam_id in runtime.state["agents"]
        assert "cam" in web.host._spawn_registry

    async def test_another_servers_agent_goes(self, web: _Setup) -> None:
        neighbour_id = web.card("neighbour")
        assert web.main is not None

        await _forged(neighbour_id)
        await web.main.hears(neighbour_id)

        assert web.deleted() == [neighbour_id]


class TestWhileMainIsNotThere:
    async def test_a_withdrawal_during_a_main_restart_is_handed_to_the_new_main(
        self, web: _Setup
    ) -> None:
        cam_id = web.card("cam")
        web.main = None

        await _forged(cam_id)
        assert cam_id in runtime.state["agents"]

        restarted = _Main(web.host)
        restarted.manifests.manifests["cam"] = {"name": "cam", "actor_id": cam_id}
        web.main = restarted
        await mqtt.handle_message("agents/main-id/heartbeat", '{"state": "running"}')

        assert cam_id not in runtime.state["agents"]
        assert web.deleted() == [cam_id]
        assert "cam" not in restarted.manifests.manifests
        assert "cam" not in web.host._spawn_registry

    async def test_before_the_system_has_started_a_withdrawal_waits(
        self, web: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cam_id = web.card("cam")
        monkeypatch.setattr(runtime, "started_with_main", None)

        await _forged(cam_id)
        assert cam_id in runtime.state["agents"]

        await mqtt.system_started(with_main=True)

        assert web.deleted() == [cam_id]

    async def test_a_main_watched_only_now_has_its_earlier_decision_read(self, web: _Setup) -> None:
        # Main decided before the dashboard watched it, and told no one.
        neighbour_id = web.card("neighbour")
        assert web.main is not None
        await web.main.hears(neighbour_id)
        assert web.deleted() == []

        await _forged(neighbour_id)

        assert web.deleted() == [neighbour_id]


class TestWithoutMain:
    async def test_withdrawals_held_before_a_start_without_main_are_taken_then(
        self, web: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "started_with_main", None)
        web.main = None
        worker_id = web.card("worker")
        neighbour_id = web.card("neighbour")
        await _forged(worker_id)
        await _forged(neighbour_id)
        assert web.deleted() == []

        await mqtt.system_started(with_main=False)

        assert worker_id in runtime.state["agents"]
        assert web.deleted() == [neighbour_id]
        assert not withdrawals.holding()

    async def test_an_agent_running_here_is_refused_and_the_rest_go(
        self, web: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "started_with_main", False)
        web.main = None
        worker_id = web.card("worker")
        neighbour_id = web.card("neighbour")

        await _forged(worker_id)
        await _forged(neighbour_id)

        assert worker_id in runtime.state["agents"]
        assert web.deleted() == [neighbour_id]
