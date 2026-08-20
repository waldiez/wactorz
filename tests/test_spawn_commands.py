"""Spawning agents the model asked for in its reply.

The model writes a `<spawn>` block and this executes it. The guard that matters
most is the catalogue check: asked for something the catalogue already
provides, it spawns the maintained recipe rather than the version the model
just wrote. Without it, "spawn the smart energy agent" produces fresh
unreviewed code every time, under a name that only looks like the real one.

The fallbacks are ordered so the safe answer is tried first: a recipe already
running is reused, then the catalogue is asked to spawn it, and only if that
fails does the model's own code run. `replace` is the deliberate way past all
of it.

A config that cannot work is skipped rather than spawned — a dynamic agent with
no code is a model that stopped mid-sentence, not an agent.
"""

import json
from typing import Any

import pytest

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.nodes import NodeManager
from wactorz.agents.spawns import SpawnService, _parse_spawn_config

_SPAWN_FAILED = "spawn failed"


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


class _Catalogue:
    """The catalogue agent, holding the maintained recipes."""

    def __init__(self, *, ok: bool = True) -> None:
        self.name = "catalog"
        self.spawned: list[str] = []
        self._ok = ok

    async def _action_spawn(self, name: str, _options: dict[str, Any]) -> dict[str, Any]:
        self.spawned.append(name)
        return {"ok": self._ok, "message": "" if self._ok else "recipe unavailable"}


class _Registry:
    def __init__(
        self,
        running: tuple[str, ...] = (),
        catalogue: _Catalogue | None = None,
        appears_after_spawn: str | None = None,
    ) -> None:
        self._by: dict[str, Any] = {n: _Agent(n) for n in running}
        if catalogue is not None:
            self._by["catalog"] = catalogue
        self._catalogue = catalogue
        self._appears = appears_after_spawn

    def find_by_name(self, name: str) -> Any:
        if (
            self._appears
            and name == self._appears
            and self._catalogue is not None
            and self._catalogue.spawned
        ):
            return _Agent(name)
        return self._by.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._by.values())


class _Main:
    """A MainActor with the surface `_process_spawn_commands` touches."""

    def __init__(
        self,
        *,
        registry: _Registry | None = None,
        catalog_match: str | None = None,
        spawn_fails: tuple[str, ...] = (),
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        setattr(main, "_registry", registry if registry is not None else _Registry())

        self.configs: list[dict[str, Any]] = []

        async def _spawn(config: dict[str, Any], save: bool = True, **_kw: Any) -> Any:
            name = config.get("name", "?")
            if name in spawn_fails:
                raise RuntimeError(_SPAWN_FAILED)
            self.configs.append(config)
            return _Agent(name)

        setattr(main.spawns, "_spawn_from_config", _spawn)
        setattr(main, "_match_catalog_recipe", lambda name, capabilities=None: catalog_match)
        self.actor = main

    async def process(self, response: str) -> tuple[str, list[Any]]:
        return await self.actor.spawns._process_spawn_commands(response)

    @property
    def spawned_names(self) -> list[Any]:
        return [c.get("name") for c in self.configs]


def block(**config: Any) -> str:
    """A spawn block as the model writes one."""
    return f"<spawn>{json.dumps(config)}</spawn>"


def dynamic(name: str = "helper", **over: Any) -> dict[str, Any]:
    return {"name": name, "type": "dynamic", "code": "print(1)", **over}


class TestNotLettingTheModelRewriteARecipe:
    """The catalogue's version wins over one the model just wrote."""

    async def test_a_running_recipe_is_reused(self) -> None:
        main = _Main(
            registry=_Registry(running=("smart-energy",)),
            catalog_match="smart-energy",
        )

        _, spawned = await main.process(block(**dynamic("smart-energy-agent")))

        assert not main.configs
        assert [a.name for a in spawned] == ["smart-energy"]

    async def test_the_catalogue_is_asked_to_spawn_its_own_version(self) -> None:
        catalogue = _Catalogue()
        main = _Main(
            registry=_Registry(catalogue=catalogue, appears_after_spawn="smart-energy"),
            catalog_match="smart-energy",
        )

        _, spawned = await main.process(block(**dynamic("smart-energy-agent")))

        assert catalogue.spawned == ["smart-energy"]
        assert not main.configs
        assert [a.name for a in spawned] == ["smart-energy"]

    async def test_the_models_code_runs_only_if_the_catalogue_could_not(self) -> None:
        # The fallback exists so a broken catalogue does not block the user
        # entirely, which is also why it is last.
        catalogue = _Catalogue(ok=False)
        main = _Main(
            registry=_Registry(catalogue=catalogue),
            catalog_match="smart-energy",
        )

        await main.process(block(**dynamic("smart-energy-agent")))

        assert catalogue.spawned == ["smart-energy"]
        assert main.spawned_names == ["smart-energy-agent"]

    async def test_replace_is_the_way_past_the_guard(self) -> None:
        catalogue = _Catalogue()
        main = _Main(
            registry=_Registry(running=("smart-energy",), catalogue=catalogue),
            catalog_match="smart-energy",
        )

        await main.process(block(**dynamic("smart-energy-agent", replace=True)))

        assert not catalogue.spawned
        assert main.spawned_names == ["smart-energy-agent"]

    async def test_a_name_matching_no_recipe_spawns_as_written(self) -> None:
        main = _Main(catalog_match=None)

        await main.process(block(**dynamic("kettle-watcher")))

        assert main.spawned_names == ["kettle-watcher"]


class TestRefusingAConfigThatCannotRun:
    """A block that describes nothing runnable is skipped, not spawned."""

    async def test_a_dynamic_agent_with_no_code_is_skipped(self) -> None:
        main = _Main()

        _, spawned = await main.process(block(name="empty", type="dynamic"))

        assert not main.configs
        assert not spawned

    async def test_a_dynamic_agent_with_blank_code_is_skipped(self) -> None:
        main = _Main()

        await main.process(block(name="empty", type="dynamic", code="   "))

        assert not main.configs

    async def test_an_llm_agent_needs_no_code(self) -> None:
        main = _Main()

        await main.process(block(name="helper", type="llm", system_prompt="Be brief."))

        assert main.spawned_names == ["helper"]

    async def test_an_llm_agent_without_a_prompt_is_still_spawned(self) -> None:
        # It gets a default rather than being refused: an agent that answers
        # generically is more useful than one that never arrives.
        main = _Main()

        await main.process(block(name="helper", type="llm"))

        assert main.spawned_names == ["helper"]


class TestWhatTheUserIsLeftReading:
    """The blocks are machinery and are stripped from the reply."""

    async def test_the_block_is_removed_from_the_response(self) -> None:
        main = _Main()

        clean, _ = await main.process(f"Setting that up. {block(**dynamic())} Done.")

        assert "<spawn>" not in clean
        assert "Setting that up." in clean

    async def test_a_reply_with_no_blocks_is_unchanged(self) -> None:
        main = _Main()

        clean, spawned = await main.process("Just answering the question.")

        assert clean == "Just answering the question."
        assert not spawned

    async def test_every_block_in_one_reply_is_executed(self) -> None:
        main = _Main()

        await main.process(block(**dynamic("a")) + block(**dynamic("b")))

        assert main.spawned_names == ["a", "b"]

    async def test_one_failing_block_does_not_lose_the_others(self) -> None:
        main = _Main(spawn_fails=("bad",))

        _, spawned = await main.process(
            block(**dynamic("a")) + block(**dynamic("bad")) + block(**dynamic("c"))
        )

        assert main.spawned_names == ["a", "c"]
        assert len(spawned) == 2

    async def test_a_block_that_is_not_json_does_not_stop_the_rest(self) -> None:
        main = _Main()

        await main.process("<spawn>not json at all</spawn>" + block(**dynamic("a")))

        assert main.spawned_names == ["a"]


class TestChoosingLocalOrRemote:
    """`_spawn_from_config` routes on whether the config names a node."""

    async def _route(self, config: dict[str, Any]) -> tuple[list[Any], list[Any]]:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.spawns = SpawnService(main)
        local: list[Any] = []
        remote: list[Any] = []

        async def _local(cfg: dict[str, Any], register: bool = True, **_kw: Any) -> None:
            local.append((cfg, register))

        async def _remote(cfg: dict[str, Any], node: str, save: bool) -> None:
            remote.append((cfg, node, save))

        setattr(main, "_spawn_local_from_config", _local)
        setattr(main.spawns, "_spawn_remote", _remote)
        await main.spawns._spawn_from_config(config, save=True)
        return local, remote

    async def test_a_config_naming_a_node_goes_out_over_mqtt(self) -> None:
        local, remote = await self._route(dynamic("sensor", node="rpi"))

        assert not local
        assert remote[0][1] == "rpi"

    async def test_a_config_with_no_node_is_spawned_here(self) -> None:
        local, remote = await self._route(dynamic("helper"))

        assert not remote
        assert local

    async def test_whitespace_is_not_a_node(self) -> None:
        # A blank node field is how a local config arrives from several
        # callers; treating it as a node name publishes to `nodes//spawn`.
        local, remote = await self._route(dynamic("helper", node="   "))

        assert not remote
        assert local


class TestParseSpawnConfig:
    """Three strategies, tried in order, for a config carrying raw agent code."""

    def test_strategy_one_plain_json(self) -> None:
        config = _parse_spawn_config('{"name": "x", "code": "print(1)"}')
        assert config == {"name": "x", "code": "print(1)"}

    def test_surrounding_whitespace_is_stripped_first(self) -> None:
        assert _parse_spawn_config('\n  {"name": "x", "code": "y"}  \n')["name"] == "x"

    def test_strategy_two_backtick_delimited_code(self) -> None:
        config = _parse_spawn_config('{"name": "x", "code": `line1\nline2`}')
        assert config["code"] == "line1\nline2"
        assert config["name"] == "x"

    def test_strategy_three_raw_multiline_code(self) -> None:
        """Unescaped newlines defeat strategies 1 and 2; the quote scan handles it."""
        config = _parse_spawn_config('{"name": "x", "code": "def f():\n    return 1\n"}')
        assert config["code"] == "def f():\n    return 1\n"

    def test_code_containing_double_quotes_is_not_truncated(self) -> None:
        """The reason the scan runs right-to-left.

        A forward scan stops at the first inner quote and truncates the code —
        this is the case that motivated the current implementation, so it is
        pinned rather than left to a comment.
        """
        raw = '{"name": "x", "code": "print(\'phrases like "Vamos!"\')\n"}'
        assert "Vamos!" in _parse_spawn_config(raw)["code"]

    def test_a_field_after_code_is_silently_swallowed(self) -> None:
        """KNOWN DEFECT, pinned as-is: a key after `code` is absorbed into it.

        The right-most quote here is the one closing `"trailing"`, and dropping
        the placeholder in at that point yields `{"code": "__CODE__"}` — valid
        JSON — so the scan stops on its first try and everything between the two
        outermost quotes becomes the code value.

        The `name` key does not survive, and the corruption is silent: the agent
        spawns with JSON fragments inside its source and fails at exec time, far
        from here. Convention says code is the last field, which is why this
        rarely bites — but these configs are model-authored, so the convention is
        a hope rather than a guarantee.

        This test pins the defect, it does not endorse it. Fixing it needs a
        decision about what a config with a trailing field should mean; when
        that is made, this test is the one to invert.
        """
        raw = '{"code": "a\nb", "name": "trailing"}'
        config = _parse_spawn_config(raw)
        assert "name" not in config
        assert config["code"] == 'a\nb", "name": "trailing'

    def test_escape_sequences_in_raw_code_are_decoded_once(self) -> None:
        r"""`\n` and `\t` become real characters and `\\` collapses to one."""
        raw = '{"name": "x", "code": "a\\nb\\tc\\\\d\ne"}'
        assert _parse_spawn_config(raw)["code"] == "a\nb\tc\\d\ne"

    def test_a_missing_code_key_is_refused_by_name(self) -> None:
        """Note the input must also defeat strategy 1 to reach the check.

        A raw newline *between* tokens is legal JSON whitespace, so
        `{"name": "x"\\n}` parses cleanly and never reaches strategy 3. The
        newline has to sit inside a string value for that to happen.
        """
        with pytest.raises(ValueError, match="No 'code' key"):
            _parse_spawn_config('{"name": "x", "nope": "y\n"}')

    def test_code_that_never_yields_valid_json_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no closing quote"):
            _parse_spawn_config('{"name": , "code": "x\n')
