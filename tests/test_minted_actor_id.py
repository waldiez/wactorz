"""An agent's id is minted once, kept in main's spawn registry, and followed wherever it runs.

Two agents may come to share a name (docs/design/agent-identity.md), so the id
can no longer be the name's. It is minted when an agent is first spawned and
kept in its registry entry; a restart, a replace and a move keep it, and a node
runs the agent under it. An entry from before ids were kept reads back with the
id its agent already had, derived from its name, so an upgrade changes nothing.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.test_spawn_remote import _Main, llm_agent
from wactorz import plugins
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.alongside import settle
from wactorz.agents.main.nodes import NodeManager, remote_actor_id
from wactorz.agents.main.spawn_registry import NAME_KEYED_REGISTRY_KEY, SpawnRegistry
from wactorz.agents.main.spawns import SpawnService, id_on_node, ids_refused
from wactorz.agents.mixins import SpawnMixin
from wactorz.core.actor import Actor, ActorMetrics, Message, build_actor, derive_actor_id
from wactorz.core.addresses import Address
from wactorz.core.identity import given_actor_id
from wactorz.core.registry import ActorRegistry, Supervisor
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner

MINTED = "5f0c7a52-2f3e-4a51-9d0e-6b1f2a3c4d5e"


class _Probe(Actor):
    """A real actor, for what is decided about its id."""

    async def handle_message(self, msg: Message) -> None:
        pass


class _NoIdParameter(Actor):
    """An agent whose author wrote a constructor that takes no ``actor_id``."""

    def __init__(self, *, name: str, persistence_dir: str | None = None) -> None:
        super().__init__(name=name, persistence_dir=persistence_dir)

    async def handle_message(self, msg: Message) -> None:
        pass


class _Running:
    """The live actors main can see, by name."""

    def __init__(self, **ids: str) -> None:
        self._actors = {name: SimpleNamespace(name=name, actor_id=i) for name, i in ids.items()}

    def find_by_name(self, name: str) -> Any:
        return self._actors.get(name)


def _main(store: dict[str, Any], running: _Running | None = None) -> MainActor:
    """A MainActor whose registry is real, kept in ``store``."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.actor_id = "main-id"
    main.spawns = SpawnService(main)
    main.nodes = NodeManager()
    main.metrics = ActorMetrics()
    setattr(main, "_registry", running or _Running())
    setattr(main, "recall", lambda key, default=None: store.get(key, default))
    setattr(main, "persist", store.__setitem__)
    return main


class TestBuildingAnActor:
    def test_a_constructor_that_takes_the_id_is_given_it(self) -> None:
        actor = build_actor(_Probe, MINTED, {"name": "counter"})

        assert actor.actor_id == MINTED
        assert actor.stable_id

    def test_one_that_does_not_still_runs_under_it(self) -> None:
        actor = build_actor(_NoIdParameter, MINTED, {"name": "counter"})

        assert actor.actor_id == MINTED
        assert actor.stable_id, "an id main keeps survives a restart, so it holds a session"

    def test_without_one_the_name_decides(self) -> None:
        assert build_actor(_Probe, None, {"name": "counter"}).actor_id == derive_actor_id("counter")

    async def test_a_restart_rebuilds_it_under_the_same_id(self) -> None:
        registry = ActorRegistry()
        supervisor = Supervisor(registry, lambda _actor: None)
        registry._supervisor_ref = supervisor
        parent = _Probe(name="parent")
        parent._registry = registry
        child = await parent.spawn(_NoIdParameter, actor_id=MINTED, name="counter")
        try:
            rebuilt = await cast("Any", supervisor._specs["counter"].factory)()

            assert rebuilt.actor_id == MINTED
        finally:
            await child.stop()

    def test_a_library_class_without_the_parameter_runs_under_it(self) -> None:
        plugin = plugins.plugin_from(_NoIdParameter)

        assert plugin.build(name="counter", actor_id=MINTED).actor_id == MINTED


class TestAnIdAsASpawnSaysIt:
    def test_a_uuid_is_taken_in_its_canonical_spelling(self) -> None:
        assert given_actor_id(MINTED.upper()) == MINTED

    # It becomes a level of every topic the agent uses.
    @pytest.mark.parametrize("value", ["agents/x", "#", "", 5, None])
    def test_anything_else_is_no_id(self, value: object) -> None:
        assert given_actor_id(value) is None


class TestTheRegistry:
    def test_an_entry_from_before_ids_reads_back_with_the_id_it_had(self) -> None:
        store = {NAME_KEYED_REGISTRY_KEY: {"counter": {"name": "counter", "code": "..."}}}

        entry = _main(store).spawns._get_spawn_registry().only("counter")

        assert entry is not None and entry["actor_id"] == derive_actor_id("counter")

    def test_a_save_keeps_the_id_the_entry_has(self) -> None:
        store = {NAME_KEYED_REGISTRY_KEY: {"counter": {"name": "counter", "actor_id": MINTED}}}
        main = _main(store)

        # A code update, say, handing over a config read from somewhere else.
        main._save_to_spawn_registry({"name": "counter", "actor_id": str(uuid.uuid4())})

        assert main._registered_actor_id("counter") == MINTED

    def test_a_first_save_without_an_id_takes_the_running_agents(self) -> None:
        main = _main({}, _Running(counter=MINTED))

        main._save_to_spawn_registry({"name": "counter"})

        assert main._registered_actor_id("counter") == MINTED

    def test_a_first_save_with_nothing_running_takes_the_spawns(self) -> None:
        main = _main({})

        main._save_to_spawn_registry({"name": "counter", "actor_id": MINTED})

        assert main._registered_actor_id("counter") == MINTED

    def test_an_agent_deleted_and_spawned_again_is_a_new_one(self) -> None:
        store = {NAME_KEYED_REGISTRY_KEY: {"counter": {"name": "counter", "actor_id": MINTED}}}
        main = _main(store)
        main._remove_from_spawn_registry("counter")

        main._save_to_spawn_registry({"name": "counter", "actor_id": (fresh := str(uuid.uuid4()))})

        assert main._registered_actor_id("counter") == fresh


class TestTheIdASpawnHereRunsUnder:
    def test_the_registry_keeps_it_across_a_restart_or_replace(self) -> None:
        store = {NAME_KEYED_REGISTRY_KEY: {"counter": {"name": "counter", "actor_id": MINTED}}}

        assert _main(store)._spawn_actor_id("counter", running=None) == MINTED

    def test_replacing_an_agent_main_never_spawned_keeps_its_id(self) -> None:
        running = derive_actor_id("counter")

        assert _main({})._spawn_actor_id("counter", running=running) == running

    def test_a_new_agent_gets_a_new_id_not_its_names(self) -> None:
        main = _main({})

        first = main._spawn_actor_id("counter", running=None)
        second = main._spawn_actor_id("counter", running=None)

        assert derive_actor_id("counter") not in (first, second)
        assert first != second

    def test_with_no_main_to_keep_it_the_name_decides(self) -> None:
        # The id would change at every start, and with it every retained topic.
        nobody = cast("SpawnMixin", SimpleNamespace(_registry=None))

        assert SpawnMixin._spawn_actor_id(nobody, "counter", None) == derive_actor_id("counter")

    async def test_the_spawn_configs_own_id_is_never_taken(self, tmp_path: Path) -> None:
        # A spawn config is often written by a model; one naming main's own id
        # would otherwise take over main's retained topics and commands.
        main = _main({})
        main._persistence_dir = tmp_path / "main"
        built: list[dict[str, Any]] = []

        async def _spawn_llm(config: dict[str, Any], name: str) -> Any:
            built.append(config)
            return SimpleNamespace(name=name, actor_id=config["actor_id"])

        setattr(main, "_spawn_llm_agent", _spawn_llm)

        claimed = {**llm_agent(), "actor_id": derive_actor_id("main")}
        await main._spawn_local_from_config(claimed)

        (config,) = built
        assert config["actor_id"] != derive_actor_id("main")
        assert main._registered_actor_id("helper") == config["actor_id"]


class TestTheIdOnANode:
    def test_a_new_agent_gets_a_fresh_id_where_the_node_honours_one(self) -> None:
        given = id_on_node("counter", None, takes_ids=True)

        assert given is not None
        assert given != derive_actor_id("counter")

    @pytest.mark.parametrize("takes_ids", [False, None])
    def test_elsewhere_it_gets_the_one_every_node_arrives_at(self, takes_ids: bool | None) -> None:
        assert id_on_node("counter", None, takes_ids) == derive_actor_id("counter")

    def test_an_agent_keeps_the_id_it_has(self) -> None:
        assert id_on_node("counter", MINTED, takes_ids=True) == MINTED

    def test_an_agent_with_its_own_id_is_refused_by_a_node_that_ignores_it(self) -> None:
        assert ids_refused("counter", MINTED, takes_ids=False)

    def test_a_node_not_yet_heard_from_is_not_refused(self) -> None:
        # At a restart, main restores before every retained manifest arrives.
        assert not ids_refused("counter", MINTED, takes_ids=None)

    def test_a_derived_id_goes_anywhere(self) -> None:
        assert not ids_refused("counter", derive_actor_id("counter"), takes_ids=False)

    @pytest.mark.parametrize(
        ("manifest", "says"),
        [(None, None), ({"agents": []}, False), ({"actor_ids": True}, True)],
    )
    def test_the_manifest_says_whether_it_honours_one(
        self, manifest: dict[str, Any] | None, says: bool | None
    ) -> None:
        nodes = NodeManager()
        if manifest is not None:
            nodes.node_manifests["rpi"] = manifest

        assert nodes.takes_actor_ids("rpi") is says

    def test_a_remote_agents_id_is_its_entrys(self) -> None:
        registry = SpawnRegistry.from_names(
            {"counter": {"name": "counter", "node": "rpi", "actor_id": MINTED}}
        )

        assert remote_actor_id("counter", "rpi", registry) == MINTED
        assert remote_actor_id("counter", "pc", registry) == derive_actor_id("counter")
        assert remote_actor_id("stranger", "rpi", registry) == derive_actor_id("stranger")


def _remote_main(manifest: dict[str, Any], registry: dict[str, Any] | None = None) -> _Main:
    main = _Main(known_nodes={"rpi": {}}, spawn_registry=registry)
    main.actor.nodes.node_manifests["rpi"] = manifest
    return main


class TestASpawnOnANode:
    async def test_a_new_agent_is_sent_with_a_fresh_id_and_saved_with_it(self) -> None:
        main = _remote_main({"actor_ids": True})

        await main.spawn(llm_agent())

        _, payload, _ = main.published_to("/spawn")
        assert payload["actor_id"] != derive_actor_id("helper")
        assert main.saved[0]["actor_id"] == payload["actor_id"]

    async def test_one_already_known_is_sent_with_the_id_it_keeps(self) -> None:
        main = _remote_main({"actor_ids": True}, {"helper": {**llm_agent(), "actor_id": MINTED}})

        await main.spawn(llm_agent())

        _, payload, _ = main.published_to("/spawn")
        assert payload["actor_id"] == MINTED

    async def test_one_with_its_own_id_is_refused_by_an_older_node(self) -> None:
        main = _remote_main({"agents": []}, {"helper": {**llm_agent(), "actor_id": MINTED}})

        await main.spawn(llm_agent())

        assert not [t for t, _, _ in main.published if t.endswith("/spawn")]
        assert not main.saved
        (message,) = [p["message"] for _, p, _ in main.published if p.get("type") == "error"]
        assert "/deploy rpi" in message


class TestAMoveOffMain:
    async def test_the_entry_stays_so_the_agent_keeps_its_id(self) -> None:
        store = {NAME_KEYED_REGISTRY_KEY: {"helper": {**llm_agent(), "actor_id": MINTED}}}
        main = _main(store, _Running(helper=MINTED))
        stopped: list[str] = []

        async def _stop(_existing: Any, name: str) -> None:
            stopped.append(name)

        setattr(main, "_stop_for_replace", _stop)

        settled = await settle(main, {**llm_agent(), "replace": True, "node": "rpi"})

        assert (settled.refused, settled.moved_from, stopped) == (
            None,
            Address("helper", "main"),
            ["helper"],
        )
        assert main._registered_actor_id("helper") == MINTED


class TestTheNode:
    def test_an_agent_runs_under_the_id_its_spawn_names(self, tmp_path: Path) -> None:
        runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))

        agent = NodeAgent({"name": "counter", "code": "", "actor_id": MINTED}, runner)

        assert agent.actor_id == MINTED

    @pytest.mark.parametrize("config", [{}, {"actor_id": "agents/#"}])
    def test_without_a_usable_one_it_derives_one(
        self, tmp_path: Path, config: dict[str, Any]
    ) -> None:
        runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))

        agent = NodeAgent({"name": "counter", "code": "", **config}, runner)

        assert agent.actor_id == derive_actor_id("counter")


class _NodeHost:
    """What `NodeManager` asks of main when a manifest arrives."""

    def __init__(self, registry: dict[str, dict[str, Any]]) -> None:
        self.registry = registry
        self.notices: list[dict[str, Any]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names(self.registry)

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.notices.append(notice)


def _manifest(**over: Any) -> dict[str, Any]:
    return {"manifest_v": 1, "agents": [], **over}


class TestANodeThatGivesAnotherId:
    """A minted id sent before the node's manifest arrived, as at a restart, to a
    node from before ids were kept: it runs the agent under the derived id."""

    def _nodes(self, **registry: dict[str, Any]) -> tuple[NodeManager, _NodeHost]:
        host = _NodeHost(registry)
        return NodeManager(cast("Any", host)), host

    def test_is_named_with_the_agents_once_its_manifest_says_so(self) -> None:
        nodes, host = self._nodes(counter={"name": "counter", "node": "rpi", "actor_id": MINTED})

        nodes.receive_manifest("rpi", _manifest())
        nodes.receive_manifest("rpi", _manifest())

        (notice,) = host.notices
        assert "'counter'" in notice["message"]
        assert "/deploy rpi" in notice["message"]

    def test_says_nothing_of_an_agent_running_under_its_derived_id(self) -> None:
        derived = derive_actor_id("counter")
        nodes, host = self._nodes(counter={"name": "counter", "node": "rpi", "actor_id": derived})

        nodes.receive_manifest("rpi", _manifest())

        assert host.notices == []

    def test_says_nothing_of_a_node_that_honours_ids(self) -> None:
        nodes, host = self._nodes(counter={"name": "counter", "node": "rpi", "actor_id": MINTED})

        nodes.receive_manifest("rpi", _manifest(actor_ids=True))

        assert host.notices == []
