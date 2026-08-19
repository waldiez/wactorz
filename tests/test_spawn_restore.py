"""Bringing agents back at startup, and conjuring one on demand.

Restore reads the spawn registry and puts back what should be running. The
guard that matters is the one against the Supervisor: both it and this method
spawn user agents at startup, and both register under the same deterministic
actor id. The dict entry is simply overwritten, so a double spawn looks fine
while the first instance's MQTT subscriptions keep running and every message
arrives twice. Nothing about that is visible from the restore itself, which is
why the skip is pinned here rather than left to be noticed in a broker log.

Resolving is the other direction: an agent named in a delegation that is not
running yet may exist as a catalogue recipe, and is spawned from it. A name
that matches nothing resolves to nothing, and the caller is told whether the
name was spawnable so it can say which of the two happened.
"""

from typing import Any

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.nodes import NodeManager
from wactorz.agents.spawns import SpawnService

_SPAWN_FAILED = "agent failed to start"
_NODE_UNREACHABLE = "node unreachable"
_RECIPE_FAILED = "recipe blew up"


class _Spec:
    """One Supervisor entry: an agent it holds a factory for."""

    def __init__(self, actor: object | None = None, retired: bool = False) -> None:
        self.actor = actor
        self.retired = retired


class _Supervisor:
    def __init__(self, specs: dict[str, _Spec]) -> None:
        self._specs = specs


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


class _Catalogue:
    """The catalogue agent, which can spawn a recipe by name."""

    def __init__(self, *, result: dict[str, Any] | None = None, raises: bool = False) -> None:
        self.name = "catalog"
        self.spawned: list[str] = []
        self._result = result
        self._raises = raises

    async def _action_spawn(self, name: str, _options: dict[str, Any]) -> dict[str, Any]:
        self.spawned.append(name)
        if self._raises:
            raise RuntimeError(_RECIPE_FAILED)
        return self._result if self._result is not None else {"ok": True}


class _Registry:
    def __init__(
        self,
        running: tuple[str, ...] = (),
        supervisor: _Supervisor | None = None,
        catalogue: _Catalogue | None = None,
        appears_after_spawn: str | None = None,
    ) -> None:
        self._by: dict[str, Any] = {n: _Agent(n) for n in running}
        if catalogue is not None:
            self._by["catalog"] = catalogue
        self._supervisor_ref = supervisor
        self._appears = appears_after_spawn
        self._catalogue = catalogue

    def find_by_name(self, name: str) -> Any:
        # A recipe that spawns successfully is in the registry by the time the
        # caller looks again, which is the only reason the second lookup differs.
        if (
            self._appears
            and name == self._appears
            and self._catalogue is not None
            and self._catalogue.spawned
        ):
            return _Agent(name)
        return self._by.get(name)


class _Main:
    """A MainActor with the surface restore and resolve touch, stubbed."""

    def __init__(
        self,
        *,
        spawn_registry: dict[str, dict[str, Any]] | None = None,
        manifests: dict[str, dict[str, Any]] | None = None,
        registry: _Registry | None = None,
        local_spawn_fails: tuple[str, ...] = (),
        remote_spawn_fails: tuple[str, ...] = (),
        catalog_match: str | None = None,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        main._agent_manifests = dict(manifests or {})
        setattr(main, "_registry", registry if registry is not None else _Registry())

        self.locally_spawned: list[tuple[str, bool, bool]] = []
        self.remotely_spawned: list[tuple[str, str, bool]] = []

        async def _local(config: dict[str, Any], save: bool = True, *, from_registry: bool = False):
            name = config.get("name", "?")
            if name in local_spawn_fails:
                raise RuntimeError(_SPAWN_FAILED)
            self.locally_spawned.append((name, save, from_registry))

        async def _remote(config: dict[str, Any], node: str, save: bool) -> None:
            name = config.get("name", "?")
            if name in remote_spawn_fails:
                raise RuntimeError(_NODE_UNREACHABLE)
            self.remotely_spawned.append((name, node, save))

        setattr(main.spawns, "_get_spawn_registry", lambda: dict(spawn_registry or {}))
        setattr(main.spawns, "_spawn_from_config", _local)
        setattr(main.spawns, "_spawn_remote", _remote)
        setattr(main, "_match_catalog_recipe", lambda name, capabilities=None: catalog_match)
        self.actor = main

    async def restore(self) -> None:
        await self.actor.spawns._restore_spawned_agents()

    async def resolve(self, name: str) -> tuple[Any, bool]:
        return await self.actor.spawns._resolve_or_spawn(name)

    @property
    def restored_names(self) -> list[str]:
        return [n for n, _, _ in self.locally_spawned]


def local(name: str, **over: Any) -> dict[str, Any]:
    return {"name": name, "code": "print(1)", **over}


def remote(name: str, node: str = "rpi", **over: Any) -> dict[str, Any]:
    return {"name": name, "code": "print(1)", "node": node, **over}


def recipe(**over: Any) -> dict[str, Any]:
    """A manifest entry the catalogue can spawn from."""
    return {"spawnable": True, "catalog": "catalog", **over}


class TestNotRacingTheSupervisor:
    """Both restore user agents at startup; only one of them may."""

    async def test_an_agent_the_supervisor_already_holds_is_left_alone(self) -> None:
        # The second spawn would take the same deterministic actor id, so the
        # duplicate is invisible in the registry while both instances keep
        # their MQTT subscriptions and every message arrives twice.
        main = _Main(
            spawn_registry={"collector": local("collector")},
            registry=_Registry(supervisor=_Supervisor({"collector": _Spec(actor=_Agent("c"))})),
        )

        await main.restore()

        assert not main.locally_spawned

    async def test_a_supervised_entry_with_no_live_actor_is_restored(self) -> None:
        # The Supervisor knows the name but has not brought it up, so nothing
        # else is going to.
        main = _Main(
            spawn_registry={"collector": local("collector")},
            registry=_Registry(supervisor=_Supervisor({"collector": _Spec(actor=None)})),
        )

        await main.restore()

        assert main.restored_names == ["collector"]

    async def test_a_retired_entry_is_restored(self) -> None:
        main = _Main(
            spawn_registry={"collector": local("collector")},
            registry=_Registry(
                supervisor=_Supervisor({"collector": _Spec(actor=_Agent("c"), retired=True)})
            ),
        )

        await main.restore()

        assert main.restored_names == ["collector"]

    async def test_only_the_supervised_names_are_skipped(self) -> None:
        main = _Main(
            spawn_registry={"a": local("a"), "b": local("b")},
            registry=_Registry(supervisor=_Supervisor({"a": _Spec(actor=_Agent("a"))})),
        )

        await main.restore()

        assert main.restored_names == ["b"]


class TestRestoringWhatWasRunning:
    """Local and remote agents come back by different routes."""

    async def test_an_empty_registry_does_nothing(self) -> None:
        main = _Main()

        await main.restore()

        assert not main.locally_spawned
        assert not main.remotely_spawned

    async def test_a_remote_agent_is_re_published_to_its_node(self) -> None:
        main = _Main(spawn_registry={"sensor": remote("sensor", "rpi-kitchen")})

        await main.restore()

        assert main.remotely_spawned == [("sensor", "rpi-kitchen", False)]
        assert not main.locally_spawned

    async def test_an_agent_already_running_locally_is_not_spawned_again(self) -> None:
        main = _Main(
            spawn_registry={"collector": local("collector")},
            registry=_Registry(running=("collector",)),
        )

        await main.restore()

        assert not main.locally_spawned

    async def test_restoring_never_writes_back_to_the_registry(self) -> None:
        # It is reading the registry to rebuild from it. Saving as it goes
        # would rewrite every entry on every startup.
        main = _Main(spawn_registry={"a": local("a"), "s": remote("s")})

        await main.restore()

        assert [save for _, save, _ in main.locally_spawned] == [False]
        assert [save for _, _, save in main.remotely_spawned] == [False]

    async def test_a_restored_agent_is_marked_as_coming_from_the_registry(self) -> None:
        main = _Main(spawn_registry={"a": local("a")})

        await main.restore()

        assert main.locally_spawned[0][2] is True

    async def test_one_agent_failing_does_not_strand_the_others(self) -> None:
        main = _Main(
            spawn_registry={"a": local("a"), "bad": local("bad"), "c": local("c")},
            local_spawn_fails=("bad",),
        )

        await main.restore()

        assert main.restored_names == ["a", "c"]

    async def test_a_remote_failure_does_not_strand_the_others(self) -> None:
        main = _Main(
            spawn_registry={"bad": remote("bad"), "good": remote("good")},
            remote_spawn_fails=("bad",),
        )

        await main.restore()

        assert [n for n, _, _ in main.remotely_spawned] == ["good"]


class TestResolvingAnAgentByName:
    """A delegation names an agent; it may not be running yet."""

    async def test_a_running_agent_resolves_to_itself(self) -> None:
        main = _Main(registry=_Registry(running=("weather",)))

        target, spawnable = await main.resolve("weather")

        assert target is not None
        assert spawnable is False

    async def test_an_unknown_name_resolves_to_nothing(self) -> None:
        main = _Main()

        target, spawnable = await main.resolve("ghost")

        assert target is None
        assert spawnable is False

    async def test_a_catalogue_recipe_is_spawned_on_demand(self) -> None:
        catalogue = _Catalogue()
        main = _Main(
            manifests={"weather": recipe()},
            registry=_Registry(catalogue=catalogue, appears_after_spawn="weather"),
        )

        target, spawnable = await main.resolve("weather")

        assert catalogue.spawned == ["weather"]
        assert spawnable is True
        assert target is not None

    async def test_a_manifest_that_is_not_spawnable_is_not_spawned(self) -> None:
        catalogue = _Catalogue()
        main = _Main(
            manifests={"weather": recipe(spawnable=False)},
            registry=_Registry(catalogue=catalogue),
        )

        _, spawnable = await main.resolve("weather")

        assert not catalogue.spawned
        assert spawnable is False

    async def test_a_loose_name_reaches_the_recipe_it_meant(self) -> None:
        # "smart energy agent" and "smart-energy-agent" both mean the
        # "smart-energy" recipe, and a delegation is written by a model.
        catalogue = _Catalogue()
        main = _Main(
            manifests={"smart-energy": recipe()},
            registry=_Registry(catalogue=catalogue, appears_after_spawn="smart-energy"),
            catalog_match="smart-energy",
        )

        _, spawnable = await main.resolve("smart energy agent")

        assert catalogue.spawned == ["smart-energy"]
        assert spawnable is True

    async def test_a_refused_spawn_leaves_the_caller_with_nothing(self) -> None:
        catalogue = _Catalogue(result={"ok": False, "message": "no room"})
        main = _Main(
            manifests={"weather": recipe()},
            registry=_Registry(catalogue=catalogue, appears_after_spawn="weather"),
        )

        target, spawnable = await main.resolve("weather")

        assert target is None
        assert spawnable is True

    async def test_a_recipe_that_raises_does_not_take_the_delegation_down(self) -> None:
        # The caller gets no target and says so; an exception here would end
        # the whole turn instead of the one delegation.
        catalogue = _Catalogue(raises=True)
        main = _Main(
            manifests={"weather": recipe()},
            registry=_Registry(catalogue=catalogue, appears_after_spawn="weather"),
        )

        target, spawnable = await main.resolve("weather")

        assert target is None
        assert spawnable is True
