"""A running agent follows its model's pointer: when it moves, the agent serves the new version.

The pointer watcher hands each move to the hot swap, which re-pins every agent
whose pin follows that pointer and reloads it in place: the old version is
served until the new one is in memory, a version that will not load leaves the
agent as it was, and the program is told through ``on_model``. An agent on a
node is sent the new pin on a signed command and answers how it went; a swap
that landed is an action in the provenance graph, joined from the move.
``docs/design/model-pointers.md`` ("Hot swap") is the decision these pin.
"""

import asyncio
import hashlib
import json
import logging
import pickle
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.main import artifact_service as service_module
from wactorz.agents.main import hot_swap as hot_swap_module
from wactorz.agents.main.artifact_service import ArtifactService, Change
from wactorz.agents.main.hosts import ArtifactHost, SwapHost
from wactorz.agents.main.hot_swap import MAIN, TOKEN_TTL_S, HotSwap
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.actor import Actor, ActorState, Message, UnnamedModelError
from wactorz.core.artifacts import ArtifactStore, Pointer, pinned
from wactorz.core.model_swaps import SWAP, move_id, record_swap, swap_id, swaps_of
from wactorz.core.node_signing import CONTROL_LEAVES
from wactorz.core.persistence import WactorzDB
from wactorz.core.pointer_moves import history, record_move
from wactorz.core.provenance import ACTION, DONE, LED_TO, Provenance
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner

# ── The store ─────────────────────────────────────────────────────────────────


@pytest.fixture(name="state")
def state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main's state directory, where the swap and the watcher look for the store."""
    state = tmp_path / "state"
    monkeypatch.setattr(hot_swap_module, "resolve_state_dir", lambda: str(state))
    monkeypatch.setattr(service_module, "resolve_state_dir", lambda: str(state))
    return state


@pytest.fixture(name="store")
def store_fixture(state: Path, tmp_path: Path) -> ArtifactStore:
    """``imu`` in two versions of raw bytes, with ``active`` on the first."""
    store = ArtifactStore.in_state_dir(state)
    for version in (1, 2):
        weights = tmp_path / f"imu-{version}.bin"
        weights.write_bytes(f"imu weights {version}".encode())
        store.add(weights, "imu", format_name="raw")
    store.move("active", "imu:1", by="test")
    return store


def readable(store: ArtifactStore, reference: str, **extra: Any) -> dict[str, Any]:
    """``reference`` pinned, with the path an agent on main reads it at."""
    entry = {**pinned(store.resolve(reference)), **extra}
    return {**entry, "path": str(store.readable_path(entry))}


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "db" / "wactorz.db") as database:
        monkeypatch.setattr(hot_swap_module, "get_db", lambda: database)
        monkeypatch.setattr(service_module, "get_db", lambda: database)
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


# ── The actor ─────────────────────────────────────────────────────────────────


class Serving(Actor):
    """An actor that names one model and remembers what it was told of it."""

    MODELS: ClassVar[dict[str, str]] = {"model": "imu"}

    def __init__(self, name: str = "serving") -> None:
        super().__init__(name=name)
        self.told: list[tuple[str, Any]] = []

    async def handle_message(self, msg: Message) -> None:
        pass

    async def on_model_swapped(self, key: str, model: Any) -> None:
        self.told.append((key, model))


@pytest.fixture(name="serving")
def serving_fixture(store: ArtifactStore) -> Serving:
    actor = Serving()
    actor._artifacts = {"model": readable(store, "imu:1", follows="active")}
    return actor


class TestTheActorSwaps:
    async def test_a_loaded_model_is_read_again_and_the_program_told(
        self, serving: Serving, store: ArtifactStore
    ) -> None:
        assert await serving.load_model("model") == b"imu weights 1"

        loaded = await serving.swap_model("model", readable(store, "imu:2"))

        assert loaded == b"imu weights 2"
        assert await serving.load_model("model") == b"imu weights 2"
        assert serving.artifacts["model"]["version"] == 2
        assert serving.artifact_path("model") == readable(store, "imu:2")["path"]
        assert serving.told == [("model", b"imu weights 2")]

    async def test_a_model_the_program_opens_itself_changes_path_only(
        self, serving: Serving, store: ArtifactStore
    ) -> None:
        loaded = await serving.swap_model("model", readable(store, "imu:2"))

        assert loaded is None
        assert serving.artifact_path("model") == readable(store, "imu:2")["path"]
        assert serving.told == [("model", None)]
        # Loaded afterwards, it is the new version.
        assert await serving.load_model("model") == b"imu weights 2"

    async def test_a_version_that_will_not_load_leaves_the_agent_as_it_was(
        self, serving: Serving, store: ArtifactStore
    ) -> None:
        await serving.load_model("model")
        gone = {**readable(store, "imu:2"), "path": str(store.root / "nowhere.bin")}

        with pytest.raises(FileNotFoundError):
            await serving.swap_model("model", gone)

        assert serving.artifacts["model"]["version"] == 1
        assert await serving.load_model("model") == b"imu weights 1"
        assert serving.told == []

    async def test_a_key_the_agent_does_not_name_is_refused(
        self, serving: Serving, store: ArtifactStore
    ) -> None:
        with pytest.raises(UnnamedModelError):
            await serving.swap_model("other", readable(store, "imu:2"))

    async def test_the_new_version_is_read_as_the_old_one_was(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A model loaded with ``allow_code`` is reloaded with it; the program said so once."""
        for version in (1, 2):
            path = tmp_path / f"clf-{version}.pkl"
            path.write_bytes(pickle.dumps({"version": version}))
            store.add(path, "clf", allow_code=True)
        actor = Serving()
        actor._artifacts = {"model": readable(store, "clf:1")}
        assert await actor.load_model("model", allow_code=True) == {"version": 1}

        assert await actor.swap_model("model", readable(store, "clf:2")) == {"version": 2}


# ── The program's hook ────────────────────────────────────────────────────────

KEEPS_IN_STATE = """
async def setup(agent):
    agent.state['weights'] = await agent.load_model('model')

async def on_model(agent, key, model):
    agent.state['weights'] = model
    agent.state['swapped'] = key
"""

READS_EACH_TIME = """
async def process(agent):
    agent.state['weights'] = await agent.load_model('model')
"""

HOOK_FAILS = """
async def setup(agent):
    pass

async def on_model(agent, key, model):
    raise RuntimeError('cannot take it')
"""


def _program(tmp_path: Path, code: str, store: ArtifactStore) -> DynamicAgent:
    agent = DynamicAgent(
        name="detector",
        code=code,
        poll_interval=0,
        persistence_dir=str(tmp_path),
        artifacts={"model": readable(store, "imu:1", follows="active")},
    )
    agent.state = ActorState.RUNNING
    assert agent._compile_code() is None
    return agent


class TestTheProgramIsTold:
    async def test_a_program_that_kept_the_model_replaces_it_in_the_hook(
        self, tmp_path: Path, store: ArtifactStore
    ) -> None:
        agent = _program(tmp_path, KEEPS_IN_STATE, store)
        await agent._ns["setup"](agent._api)
        assert agent._api.state["weights"] == b"imu weights 1"

        await agent.swap_model("model", readable(store, "imu:2"))

        assert agent._api.state["weights"] == b"imu weights 2"
        assert agent._api.state["swapped"] == "model"

    async def test_a_program_that_reads_each_time_needs_no_hook(
        self, tmp_path: Path, store: ArtifactStore
    ) -> None:
        agent = _program(tmp_path, READS_EACH_TIME, store)
        await agent._ns["process"](agent._api)
        assert agent._api.state["weights"] == b"imu weights 1"

        await agent.swap_model("model", readable(store, "imu:2"))
        await agent._ns["process"](agent._api)

        assert agent._api.state["weights"] == b"imu weights 2"

    async def test_a_hook_that_fails_is_the_programs_problem_and_the_swap_has_landed(
        self, tmp_path: Path, store: ArtifactStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        agent = _program(tmp_path, HOOK_FAILS, store)

        with caplog.at_level(logging.WARNING):
            await agent.swap_model("model", readable(store, "imu:2"))

        assert agent.artifacts["model"]["version"] == 2
        assert "on_model() error: cannot take it" in caplog.text


# ── Main's side ───────────────────────────────────────────────────────────────


class _Running:
    """An agent running on main, as the swap reaches it: its artifacts and `swap_model`."""

    def __init__(self, name: str, entry: dict[str, Any], *, fails: str | None = None) -> None:
        self.name = name
        self._artifacts = {"model": dict(entry)}
        self.fails = fails
        self.swapped: list[tuple[str, dict[str, Any]]] = []

    @property
    def artifacts(self) -> dict[str, dict[str, Any]]:
        return {key: dict(entry) for key, entry in self._artifacts.items()}

    async def swap_model(self, key: str, entry: dict[str, Any]) -> Any:
        if self.fails:
            raise OSError(self.fails)
        self.swapped.append((key, dict(entry)))
        self._artifacts[key] = dict(entry)
        return None


class _Registry:
    def __init__(self) -> None:
        self.running: dict[str, _Running] = {}

    def find_by_name(self, name: str) -> _Running | None:
        return self.running.get(name)

    def all_actors(self) -> list[_Running]:
        return list(self.running.values())


class _Host:
    """Main, as the swap sees it: the registries, a node's desired state and the broker."""

    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self._registry = _Registry()
        self.spawns: dict[str, dict[str, Any]] = {}
        self.desired: list[tuple[str, dict[str, Any] | None]] = []
        self.published: list[tuple[str, Any, int]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names({name: dict(cfg) for name, cfg in self.spawns.items()})

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        self.spawns[config["name"]] = dict(config)

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        self.desired.append((node, new_config))

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload, qos))

    def feed(self) -> list[str]:
        return [
            payload["message"]
            for topic, payload, _ in self.published
            if topic == "agents/main-id/logs" and payload.get("type") == "model"
        ]

    def commands(self, node: str) -> list[dict[str, Any]]:
        return [
            payload for topic, payload, _ in self.published if topic == f"nodes/{node}/swap_model"
        ]


@pytest.fixture(name="host")
def host_fixture() -> _Host:
    return _Host()


@pytest.fixture(name="swap")
def swap_fixture(host: _Host) -> HotSwap:
    # The fake declares what the swap reads; it stands in for MainActor.
    return HotSwap(cast("SwapHost", host))


def _move(store: ArtifactStore, graph: Provenance, reference: str, which: str = "active") -> Change:
    """Move ``which`` to ``reference``, recorded as the watcher records it; the change it reports."""
    name, _, _ = reference.partition(":")
    was = store.pointers(name).get(which)
    now = store.move(which, reference, by="dashboard", reason="better recall")
    record_move(graph, now)
    return (name, which, now, was)


def _spawned(host: _Host, name: str, store: ArtifactStore, *, node: str = "", **extra: Any) -> None:
    """``name`` in the spawn registry, following ``active`` of ``imu``, on ``node`` or on main."""
    entry = {**pinned(store.resolve("imu:1")), **extra}
    config: dict[str, Any] = {"name": name, "type": "dynamic", "artifacts": {"model": entry}}
    if node:
        config["node"] = node
    host.spawns[name] = config


class TestAgentsOnMainFollow:
    async def test_an_agent_following_active_is_swapped_and_the_graph_says_so(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, follows="active")
        running = _Running("detector", readable(store, "imu:1", follows="active"))
        host._registry.running["detector"] = running
        change = _move(store, graph, "imu:2")

        await swap.follow([change])

        ((key, entry),) = running.swapped
        assert (key, entry["version"], entry["follows"]) == ("model", 2, "active")
        assert entry["path"] == readable(store, "imu:2")["path"]
        assert host.spawns["detector"]["artifacts"]["model"]["version"] == 2
        assert host.feed() == ["detector on main now serves imu:2 as model (was imu:1)"]
        (node,) = [n for n in graph.nodes(ACTION) if n.payload.get("what") == SWAP]
        assert (node.status, node.node, node.actor) == (DONE, MAIN, "dashboard")
        assert (node.payload["from"], node.payload["to"], node.payload["agent"]) == (
            1,
            2,
            "detector",
        )
        now = change[2]
        assert now is not None
        cause = move_id("imu", "active", 2, now.moved_at)
        assert [(e.src, e.relation) for e in graph.edges(node.id) if e.dst == node.id] == [
            (cause, LED_TO)
        ]

    async def test_an_agent_that_named_a_version_is_left_alone(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "fixed", store)
        running = _Running("fixed", readable(store, "imu:1"))
        host._registry.running["fixed"] = running

        await swap.follow([_move(store, graph, "imu:2")])

        assert running.swapped == []
        assert host.spawns["fixed"]["artifacts"]["model"]["version"] == 1
        assert host.feed() == []

    async def test_an_agent_following_the_other_pointer_is_left_alone(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "trial", store, follows="shadow")
        running = _Running("trial", readable(store, "imu:1", follows="shadow"))
        host._registry.running["trial"] = running

        await swap.follow([_move(store, graph, "imu:2")])

        assert running.swapped == []

    async def test_a_shadow_follower_follows_shadow(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "trial", store, follows="shadow")
        running = _Running("trial", readable(store, "imu:1", follows="shadow"))
        host._registry.running["trial"] = running

        await swap.follow([_move(store, graph, "imu:2", which="shadow")])

        assert [entry["version"] for _, entry in running.swapped] == [2]
        assert host.feed() == ["trial on main now serves imu:2 as model (was imu:1)"]

    async def test_a_version_that_will_not_load_is_said_and_not_recorded(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, follows="active")
        running = _Running(
            "detector", readable(store, "imu:1", follows="active"), fails="no onnxruntime here"
        )
        host._registry.running["detector"] = running

        await swap.follow([_move(store, graph, "imu:2")])

        assert host.feed() == ["detector on main keeps imu:1 as model: no onnxruntime here"]
        assert host.spawns["detector"]["artifacts"]["model"]["version"] == 1
        assert swaps_of(graph, "imu") == []

    async def test_an_agent_not_running_is_left_to_its_restart(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        """A pin that follows is re-pinned when the agent starts again; nothing to do now."""
        _spawned(host, "stopped", store, follows="active")

        await swap.follow([_move(store, graph, "imu:2")])

        assert host.spawns["stopped"]["artifacts"]["model"]["version"] == 1
        assert host.feed() == []
        assert swaps_of(graph, "imu") == []

    async def test_a_library_agent_with_no_record_follows_too(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        """One the system started from its plugin: running here, in no registry."""
        running = _Running("imu-anomaly", readable(store, "imu:1", follows="active"))
        host._registry.running["imu-anomaly"] = running

        await swap.follow([_move(store, graph, "imu:2")])

        assert [entry["version"] for _, entry in running.swapped] == [2]
        assert host.feed() == ["imu-anomaly on main now serves imu:2 as model (was imu:1)"]
        assert "imu-anomaly" not in host.spawns

    async def test_an_agent_already_on_the_version_is_passed_over(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        """What a restore did while main was away: re-pinned already, with a stale record."""
        _spawned(host, "detector", store, follows="active")
        running = _Running("detector", readable(store, "imu:2", follows="active"))
        host._registry.running["detector"] = running

        await swap.follow([_move(store, graph, "imu:2")])

        assert running.swapped == []
        assert host.spawns["detector"]["artifacts"]["model"]["version"] == 2
        assert host.feed() == []

    async def test_a_cleared_pointer_moves_nobody(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, follows="active")
        running = _Running("detector", readable(store, "imu:1", follows="active"))
        host._registry.running["detector"] = running
        was = store.clear("active", "imu", by="dashboard")

        await swap.follow([("imu", "active", None, was)])

        assert running.swapped == []
        assert host.feed() == []


class TestAgentsOnNodesFollow:
    async def test_the_node_is_sent_the_new_pin_after_its_records_say_so(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")

        await swap.follow([_move(store, graph, "imu:2")])

        (command,) = host.commands("rpi")
        assert (command["agent"], command["key"]) == ("detector", "model")
        assert (command["entry"]["version"], command["entry"]["follows"]) == (2, "active")
        assert command["entry"]["sha256"] == store.resolve("imu:2").sha256
        assert command["token"] in swap.pending
        assert host.spawns["detector"]["artifacts"]["model"]["version"] == 2
        ((node, desired),) = host.desired
        assert node == "rpi"
        assert desired is not None
        assert desired["artifacts"]["model"]["version"] == 2
        # Nothing landed yet: no feed line, nothing in the graph.
        assert host.feed() == []
        assert swaps_of(graph, "imu") == []

    async def test_the_command_is_a_signed_control_topic(self) -> None:
        assert "swap_model" in CONTROL_LEAVES

    async def test_the_nodes_yes_is_recorded_against_the_move(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")
        change = _move(store, graph, "imu:2")
        await swap.follow([change])
        (command,) = host.commands("rpi")

        await swap.receive_ack(
            "nodes/rpi/swap_ack",
            json.dumps(
                {"token": command["token"], "agent": "detector", "key": "model", "ok": True}
            ).encode(),
        )

        assert host.feed() == ["detector on rpi now serves imu:2 as model (was imu:1)"]
        (swapped,) = swaps_of(graph, "imu")
        assert (swapped["agent"], swapped["host"], swapped["from"], swapped["to"]) == (
            "detector",
            "rpi",
            1,
            2,
        )
        assert swap.pending == {}
        node = graph.get(swap_id(command["token"]))
        assert node is not None
        now = change[2]
        assert now is not None
        assert graph.get(move_id("imu", "active", 2, now.moved_at)) is not None
        assert [e.relation for e in graph.edges(node.id)] == [LED_TO]

    async def test_the_nodes_no_is_said_and_not_recorded(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")
        await swap.follow([_move(store, graph, "imu:2")])
        (command,) = host.commands("rpi")

        await swap.receive_ack(
            "nodes/rpi/swap_ack",
            json.dumps(
                {"token": command["token"], "ok": False, "reason": "its artifacts did not arrive"}
            ).encode(),
        )

        assert host.feed() == ["detector on rpi keeps imu:1 as model: its artifacts did not arrive"]
        assert swaps_of(graph, "imu") == []
        assert swap.pending == {}

    @pytest.mark.parametrize(
        ("topic", "token"),
        [
            ("nodes/other/swap_ack", None),
            ("nodes/rpi/swap_ack", "not-a-token"),
            ("nodes/rpi/swap_ack", ""),
        ],
    )
    async def test_an_answer_main_did_not_ask_for_is_ignored(
        self,
        host: _Host,
        swap: HotSwap,
        store: ArtifactStore,
        graph: Provenance,
        topic: str,
        token: str | None,
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")
        await swap.follow([_move(store, graph, "imu:2")])
        (command,) = host.commands("rpi")
        quoted = command["token"] if token is None else token

        await swap.receive_ack(topic, json.dumps({"token": quoted, "ok": True}).encode())
        await swap.receive_ack("nodes/rpi/swap_ack", b"not json")
        await swap.receive_ack("nodes/rpi/swap_ack", None)

        assert host.feed() == []
        assert swaps_of(graph, "imu") == []
        assert command["token"] in swap.pending

    async def test_an_answer_heard_twice_is_one_swap(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")
        await swap.follow([_move(store, graph, "imu:2")])
        (command,) = host.commands("rpi")
        answer = json.dumps({"token": command["token"], "ok": True}).encode()

        await swap.receive_ack("nodes/rpi/swap_ack", answer)
        await swap.receive_ack("nodes/rpi/swap_ack", answer)

        assert len(swaps_of(graph, "imu")) == 1
        assert len(host.feed()) == 1

    async def test_a_node_that_never_answers_is_not_waited_on_for_ever(
        self,
        host: _Host,
        swap: HotSwap,
        store: ArtifactStore,
        graph: Provenance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")
        await swap.follow([_move(store, graph, "imu:2")])
        (command,) = host.commands("rpi")
        sent_at = swap.pending[command["token"]]["sent_at"]
        monkeypatch.setattr(hot_swap_module.time, "time", lambda: sent_at + TOKEN_TTL_S + 1)

        await swap.receive_ack(
            "nodes/rpi/swap_ack", json.dumps({"token": command["token"], "ok": True}).encode()
        )

        assert swap.pending == {}
        assert swaps_of(graph, "imu") == []

    async def test_a_node_agent_already_on_the_version_is_not_asked(
        self, host: _Host, swap: HotSwap, store: ArtifactStore, graph: Provenance
    ) -> None:
        _spawned(host, "detector", store, node="rpi", follows="active")
        host.spawns["detector"]["artifacts"]["model"] = {
            **pinned(store.resolve("imu:2")),
            "follows": "active",
        }

        await swap.follow([_move(store, graph, "imu:2")])

        assert host.commands("rpi") == []
        assert host.desired == []


class TestTheWatcherHandsOverEachChange:
    async def test_a_move_the_watcher_finds_reaches_the_swap(
        self, host: _Host, store: ArtifactStore, graph: Provenance
    ) -> None:
        handed: list[list[Change]] = []

        async def follow(changes: list[Change]) -> None:
            handed.append(changes)

        # The fake declares what the watcher reads; it stands in for MainActor.
        service = ArtifactService(cast("ArtifactHost", host), follow=follow)
        await service.publish_pointers()
        store.move("active", "imu:2", by="cli")

        await service.publish_pointers()

        assert [
            [(name, which, now and now.version) for name, which, now, _ in c] for c in handed
        ] == [
            [("imu", "active", 1)],
            [("imu", "active", 2)],
        ]

    async def test_a_swap_that_fails_does_not_stop_the_watcher(
        self, host: _Host, store: ArtifactStore, graph: Provenance, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def follow(changes: list[Change]) -> None:
            raise RuntimeError("registry unreadable")

        service = ArtifactService(cast("ArtifactHost", host), follow=follow)

        with caplog.at_level(logging.WARNING):
            await service.publish_pointers()

        assert "registry unreadable" in caplog.text
        assert service.announced[("imu", "active")] is not None


# ── The graph ─────────────────────────────────────────────────────────────────


class TestTheRecord:
    def test_a_swap_is_an_action_about_the_model_joined_from_its_move(
        self, graph: Provenance
    ) -> None:
        now = Pointer("imu", "active", 2, "a" * 64, 1, 1_760_000_000.0, "dashboard", "better")
        record_move(graph, now)

        node = record_swap(
            graph,
            token="t1",
            agent="detector",
            host="rpi",
            key="model",
            name="imu",
            pointer="active",
            from_version=1,
            to_version=2,
            sha256=now.sha256,
            by=now.by,
            at=now.moved_at + 3,
            moved_at=now.moved_at,
        )

        assert node.id == swap_id("t1")
        assert (node.kind, node.status, node.actor, node.node) == (ACTION, DONE, "dashboard", "rpi")
        assert [(e.src, e.dst, e.relation) for e in graph.edges(node.id)] == [
            (move_id("imu", "active", 2, now.moved_at), node.id, LED_TO)
        ]

    def test_a_swap_whose_move_is_not_in_the_graph_stands_alone(self, graph: Provenance) -> None:
        node = record_swap(
            graph,
            token="t2",
            agent="detector",
            host=MAIN,
            key="model",
            name="imu",
            pointer="active",
            from_version=None,
            to_version=2,
            sha256="b" * 64,
            by="cli",
            at=1_760_000_010.0,
            moved_at=1_760_000_000.0,
        )

        assert graph.edges(node.id) == []

    def test_the_pointer_history_leaves_swaps_out(self, graph: Provenance) -> None:
        now = Pointer("imu", "active", 2, "a" * 64, 1, 1_760_000_000.0, "dashboard", None)
        record_move(graph, now)
        record_swap(
            graph,
            token="t3",
            agent="detector",
            host=MAIN,
            key="model",
            name="imu",
            pointer="active",
            from_version=1,
            to_version=2,
            sha256=now.sha256,
            by=now.by,
            at=now.moved_at + 1,
            moved_at=now.moved_at,
        )

        assert [m["change"] for m in history(graph, "imu")] == ["moved"]
        assert [(s["agent"], s["host"], s["to"]) for s in swaps_of(graph, "imu")] == [
            ("detector", MAIN, 2)
        ]


# ── The node ──────────────────────────────────────────────────────────────────

ON_NODE = """
async def setup(agent):
    agent.state['weights'] = await agent.load_model('model')

async def on_model(agent, key, model):
    agent.state['weights'] = model
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(data: bytes, version: int, **extra: Any) -> dict[str, Any]:
    return {
        "name": "imu",
        "version": version,
        "sha256": _sha(data),
        "format": "raw",
        "size": len(data),
        "runs_code": False,
        **extra,
    }


@pytest.fixture(name="runner")
def runner_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NodeRunner:
    runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path / "node"))
    answers: list[dict[str, Any]] = []

    async def publish(topic: str, data: Any, retain: bool = False) -> None:
        answers.append({"topic": topic, **data})

    monkeypatch.setattr(runner, "publish", publish)
    runner.answers = answers  # pyright: ignore[reportAttributeAccessIssue]  # the test's record of what it published
    return runner


def _cached(runner: NodeRunner, data: bytes, tmp_path: Path) -> None:
    arrived = tmp_path / f"arrived-{_sha(data)[:8]}"
    arrived.write_bytes(data)
    runner.artifacts.take(_sha(data), arrived)


async def _hosted(runner: NodeRunner, data: bytes, tmp_path: Path) -> NodeAgent:
    """An agent running here on version 1, as a spawn left it."""
    _cached(runner, data, tmp_path)
    config = {
        "name": "detector",
        "code": ON_NODE,
        "artifacts": {"model": _entry(data, 1, follows="active")},
    }
    agent = NodeAgent(config, runner)
    agent.state = ActorState.RUNNING
    assert agent._compile_code() is None
    await agent._ns["setup"](agent._api)
    runner._configs["detector"] = config
    await runner.registry.register(agent)
    return agent


def _answers(runner: NodeRunner) -> list[dict[str, Any]]:
    return [a for a in runner.answers if a["topic"] == "nodes/rpi/swap_ack"]  # pyright: ignore[reportAttributeAccessIssue]


class TestTheNodeSwaps:
    async def test_a_cached_version_is_served_and_the_yes_quotes_the_token(
        self, runner: NodeRunner, tmp_path: Path
    ) -> None:
        agent = await _hosted(runner, b"imu weights 1", tmp_path)
        _cached(runner, b"imu weights 2", tmp_path)

        await runner._swap_and_answer(
            {
                "agent": "detector",
                "key": "model",
                "entry": _entry(b"imu weights 2", 2, follows="active"),
                "token": "t9",
            }
        )

        (answer,) = _answers(runner)
        assert (answer["ok"], answer["token"], answer["from"], answer["to"]) == (True, "t9", 1, 2)
        assert agent._api.state["weights"] == b"imu weights 2"
        assert agent.artifacts["model"]["version"] == 2
        assert runner._configs["detector"]["artifacts"]["model"]["version"] == 2

    async def test_a_version_the_node_cannot_fetch_leaves_the_agent_as_it_was(
        self, runner: NodeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await _hosted(runner, b"imu weights 1", tmp_path)

        async def fetch(name: str, config: dict[str, Any]) -> str | None:
            return "its artifacts did not arrive: nothing came"

        monkeypatch.setattr(runner, "_fetch_artifacts", fetch)

        await runner._swap_and_answer(
            {
                "agent": "detector",
                "key": "model",
                "entry": _entry(b"imu weights 2", 2),
                "token": "t9",
            }
        )

        (answer,) = _answers(runner)
        assert (answer["ok"], answer["reason"]) == (
            False,
            "its artifacts did not arrive: nothing came",
        )
        assert agent._api.state["weights"] == b"imu weights 1"
        assert runner._configs["detector"]["artifacts"]["model"]["version"] == 1

    async def test_an_agent_not_here_is_answered_so(self, runner: NodeRunner) -> None:
        await runner._swap_and_answer(
            {"agent": "nobody", "key": "model", "entry": _entry(b"x", 2), "token": "t9"}
        )

        (answer,) = _answers(runner)
        assert (answer["ok"], answer["reason"], answer["token"]) == (
            False,
            "not running here",
            "t9",
        )

    async def test_a_pin_that_is_not_one_is_refused(
        self, runner: NodeRunner, tmp_path: Path
    ) -> None:
        await _hosted(runner, b"imu weights 1", tmp_path)

        await runner._swap_and_answer(
            {"agent": "detector", "key": "model", "entry": {"name": "imu"}, "token": "t9"}
        )

        (answer,) = _answers(runner)
        assert answer["ok"] is False
        assert answer["reason"].startswith("not a pinned artifact")

    async def test_the_command_runs_off_the_subscriber_loop(
        self, runner: NodeRunner, tmp_path: Path
    ) -> None:
        await _hosted(runner, b"imu weights 1", tmp_path)
        _cached(runner, b"imu weights 2", tmp_path)

        await runner._on_swap_model(
            "nodes/rpi/swap_model",
            {
                "agent": "detector",
                "key": "model",
                "entry": _entry(b"imu weights 2", 2),
                "token": "t9",
            },
            None,
        )
        for _ in range(50):
            if _answers(runner):
                break
            await asyncio.sleep(0.01)

        (answer,) = _answers(runner)
        assert answer["ok"] is True
