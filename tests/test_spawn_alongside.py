"""Two agents of one name on different hosts: the registry that keeps them apart, and who acts on which.

A name is unique on one host. The spawn registry keeps each agent by its id, so
``counter`` on main and ``counter`` on a node are two entries; everything that
acts on one agent -- speaking for it, copying it, moving it -- finds the one an
address names, and never the other.
"""

import json
from types import SimpleNamespace
from typing import Any, ClassVar, cast

from wactorz.agents.main.commands import agents as agent_cmds
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.agents.main.hosts import VoiceHost
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.planning import PlanningMixin
from wactorz.agents.main.spawn_registry import SpawnRegistry, address_of, placed_on
from wactorz.agents.main.turn_actions import TurnActions
from wactorz.agents.main.voices import placed_node, speaks_for
from wactorz.agents.mixins import KeptHere, PlacedOnNode
from wactorz.core.addresses import Address

#: The agent called counter on main, and the one on node pi.
HERE = {
    "name": "counter",
    "node": "",
    "code": "x = 1",
    "actor_id": "11111111-1111-4111-8111-111111111111",
}
ON_PI = {
    "name": "counter",
    "node": "pi",
    "code": "x = 1",
    "actor_id": "22222222-2222-4222-8222-222222222222",
}


def _registry(*entries: dict[str, Any]) -> SpawnRegistry:
    return SpawnRegistry({entry["actor_id"]: entry for entry in entries})


class TestTheRegistry:
    def test_two_agents_of_a_name_are_two_entries(self) -> None:
        registry = _registry(HERE, ON_PI)

        assert len(registry) == 2
        assert registry.names() == {"counter"}

    def test_each_is_found_on_its_host(self) -> None:
        registry = _registry(HERE, ON_PI)

        assert (registry.at("counter", "pi") or {})["actor_id"] == ON_PI["actor_id"]
        assert (registry.at("counter", "main") or {})["actor_id"] == HERE["actor_id"]
        assert (registry.at("counter", "local") or {})["actor_id"] == HERE["actor_id"]
        assert registry.at("counter", "pc") is None

    def test_a_bare_name_finds_one_only_while_there_is_one(self) -> None:
        assert (_registry(ON_PI).only("counter") or {})["node"] == "pi"
        assert _registry(HERE, ON_PI).only("counter") is None

    def test_every_one_of_a_name_is_listed_main_first(self) -> None:
        assert [placed_on(e) for e in _registry(ON_PI, HERE).named("counter")] == ["", "pi"]

    def test_an_entry_has_a_full_address(self) -> None:
        assert address_of(HERE) == Address("counter", "main")
        assert address_of(ON_PI) == Address("counter", "pi")

    def test_what_it_hands_out_is_a_copy(self) -> None:
        registry = _registry(HERE)

        found = registry.by_id(HERE["actor_id"])
        assert found is not None
        found["code"] = "changed"

        assert (registry.by_id(HERE["actor_id"]) or {})["code"] == "x = 1"

    def test_a_store_kept_by_name_is_read_with_the_ids_it_had(self) -> None:
        registry = SpawnRegistry.stored(
            None, {"counter": {"node": "pi", "actor_id": ON_PI["actor_id"]}}
        )

        assert (registry.at("counter", "pi") or {})["actor_id"] == ON_PI["actor_id"]

    def test_a_store_kept_by_id_wins_over_the_one_by_name_even_empty(self) -> None:
        # Once written, the registry by id is the registry: every agent deleted
        # leaves it empty, and the old store is not read again.
        assert not SpawnRegistry.stored({}, {"counter": {"node": "pi"}})

    def test_a_one_time_instruction_is_not_kept(self) -> None:
        registry = SpawnRegistry.from_ids({ON_PI["actor_id"]: {**ON_PI, "alongside": True}})

        assert "alongside" not in (registry.at("counter", "pi") or {})


class _Moves:
    def __init__(self, moving: dict[str, str] | None = None) -> None:
        self._moving = moving or {}

    def moving_to(self, name: str) -> str:
        return self._moving.get(name, "")


class _Here:
    def __init__(self, *names: str) -> None:
        self._names = names

    def find_by_name(self, name: str) -> Any:
        return (
            SimpleNamespace(name=name, actor_id=HERE["actor_id"]) if name in self._names else None
        )


def _voices(registry: SpawnRegistry, *here: str, moving: dict[str, str] | None = None) -> VoiceHost:
    host = SimpleNamespace(
        _registry=_Here(*here), migration=_Moves(moving), _get_spawn_registry=lambda: registry
    )
    return cast(VoiceHost, host)  # what deciding who speaks for an agent reads


class TestWhoSpeaksForWhich:
    def test_the_node_an_agent_of_a_shared_name_is_on_speaks_for_it(self) -> None:
        # Main runs one called counter as well; that one is no node's.
        host = _voices(_registry(HERE, ON_PI), "counter")

        assert speaks_for(host, "pi", "counter")
        assert not speaks_for(host, "pc", "counter")

    def test_a_name_main_runs_and_keeps_nowhere_else_is_no_nodes(self) -> None:
        assert not speaks_for(_voices(_registry(HERE), "counter"), "pi", "counter")

    def test_a_name_main_has_no_record_of_is_its_nodes(self) -> None:
        assert speaks_for(_voices(_registry()), "pi", "counter")

    def test_a_node_an_agent_is_moving_to_speaks_for_it(self) -> None:
        host = _voices(_registry(HERE), "counter", moving={"counter": "pi"})

        assert speaks_for(host, "pi", "counter")

    def test_the_topics_id_says_which_agent_of_a_shared_name_it_is(self) -> None:
        host = _voices(_registry(HERE, ON_PI))

        assert placed_node(host, "counter", ON_PI["actor_id"]) == "pi"
        assert placed_node(host, "counter", HERE["actor_id"]) == ""
        # With no id to tell them apart, neither.
        assert placed_node(host, "counter") == ""


class _Copier:
    """Main as `/agents copy` reaches it: its registry, and a spawn that records what it is given."""

    def __init__(self, *entries: dict[str, Any]) -> None:
        self.registry = _registry(*entries)
        self.spawned: list[dict[str, Any]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        return self.registry

    async def spawn_placed(self, config: dict[str, Any]) -> Any:
        self.spawned.append(config)
        return PlacedOnNode(config["name"], config.get("node", ""), True)


def _ctx(main: _Copier) -> CommandContext:
    return CommandContext(actor=cast(Any, main))  # the two members the command reaches


class TestCopyingAnAgent:
    async def test_it_spawns_a_second_one_from_the_first_ones_config(self) -> None:
        main = _Copier(HERE)

        said = await agent_cmds.copy_agent(_ctx(main), "counter pi")

        (config,) = main.spawned
        assert config["alongside"] is True and config["node"] == "pi"
        assert config["code"] == HERE["code"]
        assert said == "Spawned 'counter' on node 'pi'"

    async def test_the_copy_takes_none_of_the_first_ones_identity(self) -> None:
        main = _Copier(ON_PI)

        await agent_cmds.copy_agent(_ctx(main), "counter main")

        (config,) = main.spawned
        assert "actor_id" not in config and "node" not in config

    async def test_a_shared_name_is_named_by_its_address(self) -> None:
        main = _Copier(HERE, ON_PI)

        refused = await agent_cmds.copy_agent(_ctx(main), "counter pc")
        await agent_cmds.copy_agent(_ctx(main), "counter@pi pc")

        assert "`counter@main` or `counter@pi`" in refused
        assert [c["node"] for c in main.spawned] == ["pc"]

    async def test_an_agent_main_did_not_spawn_has_nothing_to_copy(self) -> None:
        said = await agent_cmds.copy_agent(_ctx(_Copier()), "catalog pi")

        assert "holds no config" in said

    async def test_without_a_node_it_says_how(self) -> None:
        assert (await agent_cmds.copy_agent(_ctx(_Copier(HERE)), "counter")).startswith("Usage:")

    def test_the_question_names_the_copy(self) -> None:
        said = TurnActions((KeptHere("counter", "pi", ("counter@main",)),)).summary("")

        assert "/agents copy counter@main pi" in said


class _NodeTable:
    def __init__(self, known: dict[str, dict[str, Any]]) -> None:
        self.known = known


def _migration(
    registry: SpawnRegistry, *here: str, known: dict[str, Any] | None = None
) -> Migration:
    host = SimpleNamespace(_registry=_Here(*here), _get_spawn_registry=lambda: registry)
    return Migration(cast(Any, host), cast(Any, _NodeTable(known or {})))


class TestMovingOneOfASharedName:
    async def test_a_bare_name_kept_in_two_places_moves_neither(self) -> None:
        result = await _migration(_registry(HERE, ON_PI)).migrate_agent("counter", "pc")

        assert result["success"] is False
        assert "`counter@main` or `counter@pi`" in result["message"]

    def test_a_node_that_runs_the_name_takes_no_second_one(self) -> None:
        migration = _migration(_registry(ON_PI))

        assert migration._occupied("counter", "pi") == "counter@pi already runs."
        assert migration._occupied("counter", "pc") is None

    def test_a_node_whose_heartbeat_lists_the_name_takes_no_second_one(self) -> None:
        migration = _migration(_registry(), known={"pc": {"agents": ["counter"]}})

        assert migration._occupied("counter", "pc") == "counter@pc already runs."

    def test_the_moving_agents_own_copy_is_not_another_one(self) -> None:
        # Main keeps its copy of an agent it moved out until the node confirms,
        # so a move home in that window finds it here, under its own id.
        migration = _migration(_registry(ON_PI), "counter")

        assert migration._occupied("counter", "", HERE["actor_id"]) is None
        assert migration._occupied("counter", "pi", ON_PI["actor_id"]) is None
        assert migration._occupied("counter", "", ON_PI["actor_id"]) == "counter@main already runs."

    def test_main_running_the_name_takes_no_second_one(self) -> None:
        assert _migration(_registry(), "counter")._occupied("counter", "") == (
            "counter@main already runs."
        )


class _Local:
    """Main's own agent called counter, and the manifest it published itself."""

    actor_id = HERE["actor_id"]
    _published_manifest: ClassVar[dict[str, Any]] = {
        "name": "counter",
        "actor_id": HERE["actor_id"],
        "publishes": ["custom/counter/here"],
    }


class _ManifestHost:
    def __init__(self) -> None:
        self._registry = SimpleNamespace(
            find_by_name=lambda name: _Local() if name == "counter" else None
        )
        self.withdrawn: list[str] = []

    async def agent_withdrew(self, actor_id: str, name: str = "") -> bool:
        self.withdrawn.append(actor_id)
        return True


def _manifest(entry: dict[str, Any], topic: str) -> bytes:
    return json.dumps(
        {
            "name": entry["name"],
            "actor_id": entry["actor_id"],
            "node": entry["node"] or "",
            "publishes": [topic],
        }
    ).encode()


async def _both(registry: ManifestRegistry) -> None:
    """Main's counter and pi's counter announce themselves, as over the broker."""
    await registry.receive_manifest(
        f"agents/{HERE['actor_id']}/manifest", _manifest(HERE, "custom/counter/here")
    )
    await registry.receive_manifest(
        f"agents/{ON_PI['actor_id']}/manifest", _manifest(ON_PI, "custom/counter/pi")
    )


class TestTheManifestsOfASharedName:
    async def test_a_nodes_agent_keeps_its_own_manifest_beside_mains(self) -> None:
        registry = ManifestRegistry(cast(Any, _ManifestHost()))

        await _both(registry)

        ids = {key: m["actor_id"] for key, m in registry.manifests.items()}
        assert ids == {"counter": HERE["actor_id"], "counter@pi": ON_PI["actor_id"]}

    async def test_each_publishes_its_own_topics(self) -> None:
        registry = ManifestRegistry(cast(Any, _ManifestHost()))

        await _both(registry)

        assert [m["actor_id"] for m in registry.topic_registry["custom/counter/pi"]] == [
            ON_PI["actor_id"]
        ]
        assert [m["actor_id"] for m in registry.topic_registry["custom/counter/here"]] == [
            HERE["actor_id"]
        ]

    async def test_a_republish_replaces_only_its_own(self) -> None:
        registry = ManifestRegistry(cast(Any, _ManifestHost()))
        await _both(registry)

        await registry.receive_manifest(
            f"agents/{ON_PI['actor_id']}/manifest", _manifest(ON_PI, "custom/counter/pi")
        )

        assert len(registry.manifests) == 2

    async def test_one_withdrawing_leaves_the_other(self) -> None:
        host = _ManifestHost()
        registry = ManifestRegistry(cast(Any, host))
        await _both(registry)

        await registry.receive_manifest(f"agents/{ON_PI['actor_id']}/manifest", b"")

        assert host.withdrawn == [ON_PI["actor_id"]]
        assert list(registry.manifests) == ["counter"]
        assert "custom/counter/pi" not in registry.topic_registry
        assert "custom/counter/here" in registry.topic_registry


class _RuleOwner:
    """What deleting a pipeline rule reaches on main, recorded."""

    def __init__(self) -> None:
        self.removed: list[tuple[str, Any]] = []
        self.rules = {"r1": {"agents": ["counter"], "task": "count"}}
        self._registry = SimpleNamespace(
            find_by_name=lambda name: _Running(name), unregister=self._unregister
        )

    async def _unregister(self, actor_id: str) -> None:
        return None

    def get_pipeline_rules(self) -> dict[str, Any]:
        return self.rules

    def _remove_from_spawn_registry(
        self, name: str, node: str | None = None, *, actor_id: str | None = None
    ) -> None:
        self.removed.append((name, actor_id))

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        return None

    def _record_agent_deletion(self, name: str, reason: str = "") -> None:
        return None

    def persist(self, key: str, value: Any) -> None:
        return None


class _Running:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = HERE["actor_id"]

    async def stop(self) -> None:
        return None


class TestDeletingAPipelineRule:
    async def test_its_agent_leaves_the_registry_by_its_id(self) -> None:
        # With a second counter on a node, the bare name would name neither and
        # leave this one's entry to be restored at the next start.
        owner = _RuleOwner()

        await PlanningMixin.delete_pipeline_rule(cast(Any, owner), "r1")

        assert owner.removed == [("counter", HERE["actor_id"])]
