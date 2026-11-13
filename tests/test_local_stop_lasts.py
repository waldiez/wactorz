# SPDX-License-Identifier: Apache-2.0
"""An agent of main's own that was stopped stays stopped when main starts again.

A stop on purpose is something main remembers, by the agent's id, beside its
spawn registry. At its next start main puts such an agent back as it was left:
built, registered and known to the supervisor, and not started, so it has a
card and `/start` reaches it. A start is what clears the stop.

Main and the agents are real here, with a real registry and supervisor and no
broker. A restart is a second main over what the first one kept.
"""

import dataclasses
import inspect
from pathlib import Path
from typing import Any, ClassVar

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz import config, plugins
from wactorz.agents import catalog_agent
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.spawn_registry import SPAWN_REGISTRY_KEY, STOPPED_AGENTS_KEY
from wactorz.core import topic_bus
from wactorz.core.actor import Actor, ActorState, Message, born_stopped
from wactorz.core.registry import ActorRegistry, Supervisor
from wactorz.core.topic_bus import TopicBus
from wactorz.web import events, runtime
from wactorz.web.app import build_app

KEY = "a-key-for-the-local-stop-tests"

TARGET = "tests.test_local_stop_lasts:Lamp"
#: A program that would fail the moment it was run.
NEVER_RUN = "raise RuntimeError('the program of a stopped agent was run')\n"
#: A program that does nothing.
QUIET = "async def setup(agent):\n    pass\n"
NODE = "rpi"


def _declared(bus: TopicBus) -> list[tuple[str, str | None, list[str]]]:
    """Each contract on ``bus``: whose it is, by name and id, and what it publishes."""
    return [(c.name, c.actor_id, list(c.publishes)) for c in bus.registry.all_contracts()]


class Lamp(Actor):
    """An agent that counts how often it was started and stopped."""

    started: ClassVar[list[str]] = []
    stopped: ClassVar[list[str]] = []
    fail_start: ClassVar[bool] = False

    async def on_start(self) -> None:
        if Lamp.fail_start:
            raise RuntimeError("the lamp will not light")
        Lamp.started.append(self.name)

    async def on_stop(self) -> None:
        Lamp.stopped.append(self.name)

    async def handle_message(self, message: Message) -> None:
        return None


def _inject(_actor: Actor) -> None:
    """Stand-in for ActorSystem's MQTT injection, which needs no broker here."""


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    plugins.clear()
    plugins.register(Lamp, target=TARGET)
    Lamp.started, Lamp.stopped, Lamp.fail_start = [], [], False
    yield
    plugins.clear()


class System:
    """Main over a registry and a supervisor of its own, with what a run leaves behind to stop."""

    def __init__(self, state_dir: Path) -> None:
        self.registry = ActorRegistry()
        self.supervisor = Supervisor(self.registry, _inject, poll_interval=0.01)
        self.registry._supervisor_ref = self.supervisor
        self.main = MainActor(llm_provider=None, persistence_dir=str(state_dir))
        self.main._registry = self.registry

    async def up(self, kept: "System | None" = None) -> "System":
        """Register main, with what ``kept`` remembered, and put its agents back."""
        await self.registry.register(self.main)
        if kept is not None:
            for key in (SPAWN_REGISTRY_KEY, STOPPED_AGENTS_KEY):
                self.main.persist(key, kept.main.recall(key))
            await self.main._restore_spawned_agents()
        return self

    async def spawn(self, name: str = "lamp", **config: Any) -> Actor:
        actor = await self.main._spawn_from_config(
            {"name": name, "type": "module", "target": TARGET, **config}
        )
        assert isinstance(actor, Actor)
        return actor

    def agent(self, name: str = "lamp") -> Actor:
        actor = self.registry.find_by_name(name)
        assert actor is not None
        return actor

    def is_stopped(self, actor_id: str) -> bool:
        return self.main._get_spawn_registry().is_stopped(actor_id)

    def watched(self, name: str = "lamp") -> bool:
        """Whether the supervisor is watching ``name``, as it does a running agent."""
        spec = self.supervisor._specs.get(name)
        return spec is not None and spec.actor is not None and not spec.retired

    async def down(self) -> None:
        for actor in self.registry.all_actors():
            if actor is not self.main and actor.state == ActorState.RUNNING:
                await actor.stop()


@pytest.fixture(name="systems")
async def systems_fixture(tmp_path: Path) -> Any:
    made: list[System] = []

    def _make() -> System:
        made.append(System(tmp_path))
        return made[-1]

    yield _make
    for system in made:
        await system.down()


@pytest.fixture(name="first")
async def first_fixture(systems: Any) -> System:
    return await systems().up()


class TestAStopIsRemembered:
    async def test_by_the_agents_id_once_it_has_stopped(self, first: System) -> None:
        lamp = await first.spawn()

        assert await lamp.apply_command("stop")

        assert first.is_stopped(lamp.actor_id)
        assert first.main.recall(STOPPED_AGENTS_KEY) == [lamp.actor_id]

    async def test_a_start_forgets_it_once_it_has_started(self, first: System) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")

        assert await lamp.apply_command("start")

        assert not first.is_stopped(lamp.actor_id)
        assert first.watched()

    async def test_a_stop_that_fails_is_not_remembered(
        self, first: System, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lamp = await first.spawn()
        really_stop = lamp.stop

        async def _fails() -> None:
            raise RuntimeError("it would not stop")

        monkeypatch.setattr(lamp, "stop", _fails)

        with pytest.raises(RuntimeError):
            await lamp.apply_command("stop")

        assert not first.is_stopped(lamp.actor_id)
        monkeypatch.setattr(lamp, "stop", really_stop)

    async def test_a_start_that_fails_leaves_it_remembered(self, first: System) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        Lamp.fail_start = True

        with pytest.raises(RuntimeError):
            await lamp.apply_command("start")

        assert first.is_stopped(lamp.actor_id)
        assert lamp.state == ActorState.STOPPED

    async def test_only_for_the_agent_stopped_where_its_name_is_kept_on_a_node_too(
        self, first: System
    ) -> None:
        lamp = await first.spawn()
        first.main._save_to_spawn_registry(
            {"name": "lamp", "type": "dynamic", "code": "x = 1", "node": NODE, "alongside": True}
        )
        on_node = first.main._get_spawn_registry().at("lamp", NODE)
        assert on_node is not None

        await lamp.apply_command("stop")

        assert first.is_stopped(lamp.actor_id)
        assert not first.is_stopped(on_node["actor_id"])

    async def test_an_agent_main_did_not_spawn_is_stopped_and_nothing_is_kept(
        self, first: System, tmp_path: Path
    ) -> None:
        # One the application brought: nothing in the spawn registry is its.
        own = Lamp(name="app-owned", persistence_dir=str(tmp_path))
        own._registry = first.registry
        await first.registry.register(own)
        await own.start()

        assert await own.apply_command("stop")

        assert own.state == ActorState.STOPPED
        assert first.main.recall(STOPPED_AGENTS_KEY) in (None, [])


class TestAfterARestart:
    async def test_it_is_put_back_stopped_and_was_never_started(
        self, first: System, systems: Any
    ) -> None:
        lamp = await first.spawn()
        await first.spawn("other")
        await lamp.apply_command("stop")
        await first.down()
        Lamp.started, Lamp.stopped = [], []

        second = await systems().up(kept=first)

        again = second.agent()
        assert again is not lamp and again.actor_id == lamp.actor_id
        assert again.state == ActorState.STOPPED
        # The other is running again; the stopped one ran neither hook.
        assert Lamp.started == ["other"] and Lamp.stopped == []
        assert second.agent("other").state == ActorState.RUNNING
        assert second.is_stopped(lamp.actor_id)
        assert not second.watched() and "lamp" in second.supervisor._specs

    async def test_it_starts_when_asked_and_is_supervised_and_no_longer_kept_stopped(
        self, first: System, systems: Any
    ) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        second = await systems().up(kept=first)
        Lamp.started = []

        assert await second.agent().apply_command("start")

        assert second.agent().state == ActorState.RUNNING
        assert Lamp.started == ["lamp"]
        assert second.watched()
        assert not second.is_stopped(lamp.actor_id)

    async def test_a_start_that_fails_then_leaves_it_stopped_and_unwatched(
        self, first: System, systems: Any
    ) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        second = await systems().up(kept=first)
        Lamp.fail_start = True

        with pytest.raises(RuntimeError):
            await second.agent().apply_command("start")

        assert second.agent().state == ActorState.STOPPED
        assert second.is_stopped(lamp.actor_id) and not second.watched()

    async def test_a_generated_agents_program_is_not_run_and_declares_nothing(
        self, first: System, systems: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        config = {"name": "gen", "type": "dynamic", "code": NEVER_RUN, "publishes": ["lamps/x"]}
        first.main._save_to_spawn_registry(config)
        entry = first.main._get_spawn_registry().only("gen")
        assert entry is not None
        first.main._keep_stopped(entry["actor_id"], True)

        second = await systems().up(kept=first)

        gen = second.agent("gen")
        assert isinstance(gen, DynamicAgent) and gen.state == ActorState.STOPPED
        assert bus.registry.all_contracts() == []

    async def test_started_then_it_declares_what_its_spawn_declared(
        self, first: System, systems: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        config = {"name": "gen", "type": "dynamic", "code": QUIET, "publishes": ["lamps/x"]}
        first.main._save_to_spawn_registry(config)
        entry = first.main._get_spawn_registry().only("gen")
        assert entry is not None
        first.main._keep_stopped(entry["actor_id"], True)
        second = await systems().up(kept=first)

        assert await second.agent("gen").apply_command("start")

        assert second.agent("gen").state == ActorState.RUNNING
        assert _declared(bus) == [("gen", entry["actor_id"], ["lamps/x"])]

    async def test_one_stopped_and_started_in_place_declares_it_again_too(
        self, first: System, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        gen = await first.main._spawn_from_config(
            {"name": "gen", "type": "dynamic", "code": QUIET, "publishes": ["lamps/x"]}
        )
        assert isinstance(gen, DynamicAgent)
        assert _declared(bus) == [("gen", gen.actor_id, ["lamps/x"])]

        await gen.apply_command("stop")
        assert _declared(bus) == []
        await gen.apply_command("start")

        assert _declared(bus) == [("gen", gen.actor_id, ["lamps/x"])]

    async def test_a_native_agent_is_built_and_not_started(
        self, first: System, systems: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            catalog_agent, "get_native_factory", lambda name: Lamp if name == "nat" else None
        )
        first.main._save_to_spawn_registry({"name": "nat", "type": "native"})
        entry = first.main._get_spawn_registry().only("nat")
        assert entry is not None
        first.main._keep_stopped(entry["actor_id"], True)

        second = await systems().up(kept=first)

        assert second.agent("nat").state == ActorState.STOPPED
        assert Lamp.started == []

    async def test_one_that_cannot_be_built_does_not_strand_the_rest(
        self, first: System, systems: Any
    ) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        first.main._save_to_spawn_registry({"name": "gone", "type": "module", "target": "no:such"})
        gone = first.main._get_spawn_registry().only("gone")
        assert gone is not None
        first.main._keep_stopped(gone["actor_id"], True)

        second = await systems().up(kept=first)

        assert second.registry.find_by_name("gone") is None
        assert second.agent().state == ActorState.STOPPED


class TestAGeneratedAgentsContract:
    """What its spawn config declared is on the bus while it runs, however it came to run."""

    @pytest.fixture(name="bus")
    def bus_fixture(self, monkeypatch: pytest.MonkeyPatch) -> TopicBus:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        return bus

    async def test_one_the_supervisor_builds_again_declares_it_under_the_same_id(
        self, first: System, bus: TopicBus
    ) -> None:
        gen = await first.main._spawn_from_config(
            {"name": "gen", "type": "dynamic", "code": QUIET, "publishes": ["lamps/x"]}
        )
        assert isinstance(gen, DynamicAgent)
        factory = first.supervisor._specs["gen"].factory
        # As a crash leaves it: stopped, its contract off the bus.
        await gen.stop()
        assert _declared(bus) == []

        built = factory()
        rebuilt = await built if inspect.isawaitable(built) else built
        rebuilt._registry = first.registry
        await first.registry.register(rebuilt)
        await rebuilt.start()

        assert rebuilt is not gen
        assert _declared(bus) == [("gen", gen.actor_id, ["lamps/x"])]

    async def test_one_that_declares_no_topics_puts_nothing_on_the_bus(
        self, first: System, bus: TopicBus
    ) -> None:
        await first.main._spawn_from_config({"name": "gen", "type": "dynamic", "code": QUIET})

        assert _declared(bus) == []


class TestWhatASaveDoesToAStop:
    async def test_a_rewritten_config_of_a_stopped_agent_keeps_it_stopped(
        self, first: System
    ) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        entry = first.main._get_spawn_registry().by_id(lamp.actor_id)
        assert entry is not None

        first.main._save_to_spawn_registry({**entry, "description": "the hall lamp"})

        assert first.is_stopped(lamp.actor_id)

    async def test_spawning_it_again_in_its_place_runs_it(self, first: System) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")

        replaced = await first.spawn(replace=True)

        assert replaced.state == ActorState.RUNNING
        assert not first.is_stopped(replaced.actor_id)

    async def test_one_brought_home_from_the_node_it_was_stopped_on_runs(
        self, first: System
    ) -> None:
        first.main._save_to_spawn_registry(
            {"name": "lamp", "type": "module", "target": TARGET, "node": NODE}
        )
        entry = first.main._get_spawn_registry().at("lamp", NODE)
        assert entry is not None
        first.main._keep_stopped(entry["actor_id"], True)

        await first.spawn(actor_id=entry["actor_id"])

        assert first.agent().state == ActorState.RUNNING
        assert not first.is_stopped(entry["actor_id"])


class TestAMoveIsRefused:
    async def test_one_stopped_on_main_says_so_and_how_to_start_it(self, first: System) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        first.main._known_nodes[NODE] = {"agents": [], "status": "online"}

        result = await first.main.migrate_agent("lamp", NODE)

        assert result["success"] is False
        assert "'lamp' is stopped on main. Start it first (/start lamp)" in result["message"]


class TestBuiltAndNotStarted:
    async def test_a_child_spawned_so_is_registered_stopped_and_known_to_the_supervisor(
        self, first: System, tmp_path: Path
    ) -> None:
        with born_stopped():
            child = await first.main.spawn(Lamp, name="child", persistence_dir=str(tmp_path))

        assert first.registry.find_by_name("child") is child
        assert child.state == ActorState.STOPPED
        assert Lamp.started == [] and Lamp.stopped == []
        assert not first.watched("child") and "child" in first.supervisor._specs

    async def test_outside_the_block_a_child_is_started_as_ever(
        self, first: System, tmp_path: Path
    ) -> None:
        with born_stopped():
            pass

        child = await first.main.spawn(Lamp, name="child", persistence_dir=str(tmp_path))

        assert child.state == ActorState.RUNNING and first.watched("child")


class TestTheDashboardListsIt:
    @pytest.fixture(autouse=True)
    def _runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            runtime,
            "state",
            {"agents": {}, "nodes": {}, "alerts": [], "system_health": {}, "log_feed": []},
        )
        monkeypatch.setattr(runtime, "deleted_agent_ids", [])
        monkeypatch.setattr(runtime, "hard_resetting", False)

    async def test_one_put_back_stopped_is_in_the_snapshot_as_stopped(
        self, first: System, systems: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        second = await systems().up(kept=first)
        monkeypatch.setattr(runtime, "registry", second.registry)

        listed = {a["name"]: a["state"] for a in events.snapshot(include_totals=False)["agents"]}

        assert listed == {"lamp": "stopped"}

    async def test_and_the_list_of_actors_says_stopped_not_starting(
        self, first: System, systems: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lamp = await first.spawn()
        await first.spawn("other")
        await lamp.apply_command("stop")
        await first.down()
        second = await systems().up(kept=first)
        monkeypatch.setattr(config, "CONFIG", dataclasses.replace(config.CONFIG, api_key=KEY))
        client: TestClient[web.Request, web.Application] = TestClient(TestServer(build_app()))
        await client.start_server()
        monkeypatch.setattr(runtime, "registry", second.registry)
        try:
            response = await client.get("/api/actors", headers={"X-API-Key": KEY})
            listed = {a["name"]: a["state"] for a in await response.json()}
        finally:
            await client.close()

        # The one running has not reported yet, and is starting as ever.
        assert (listed["lamp"], listed["other"]) == ("stopped", "starting")

    async def test_a_running_agent_that_has_not_reported_is_left_to_report(
        self, first: System, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await first.spawn()
        monkeypatch.setattr(runtime, "registry", first.registry)

        assert events.snapshot(include_totals=False)["agents"] == []

    async def test_one_that_was_deleted_is_not_listed_again(
        self, first: System, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lamp = await first.spawn()
        await lamp.apply_command("stop")
        monkeypatch.setattr(runtime, "registry", first.registry)
        runtime.mark_deleted(lamp.actor_id)

        assert events.snapshot(include_totals=False)["agents"] == []
