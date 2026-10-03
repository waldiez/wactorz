"""The agents a deployment brings: found, built and offered the way built-ins are.

An entry point, a `WACTORZ_AGENTS` target or an object handed to `wactorz.run`
each become a plugin that `build_system` supervises and the catalogue lists.
Only a registered target may be spawned by import path, because a spawn
config can be written by the model.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from wactorz import plugins
from wactorz.agents.function_agent import FunctionAgent, agent
from wactorz.core.actor import Actor, Message


class Probe(Actor):
    """Says what it is on the class, the way a packaged native agent does."""

    DESCRIPTION = "A probe."
    CAPABILITIES: ClassVar[list[str]] = ["probing"]
    REQUIRES: ClassVar[dict[str, int]] = {"ram_mb": 64}

    def __init__(self, name: str = "probe", persistence_dir: str | None = None) -> None:
        super().__init__(name=name, persistence_dir=persistence_dir)

    async def handle_message(self, msg: Message) -> None:
        return None


class WithProvider(Actor):
    def __init__(
        self,
        name: str = "with-provider",
        persistence_dir: str | None = None,
        llm_provider: Any = None,
        threshold: float = 1.0,
    ) -> None:
        super().__init__(name=name, persistence_dir=persistence_dir)
        self.llm_provider = llm_provider
        self.threshold = threshold

    async def handle_message(self, msg: Message) -> None:
        return None


@agent(subscribes="in/x", publishes="out/x")
def decorated(reading: dict) -> dict:
    return reading


@pytest.fixture(autouse=True)
def _fresh_registry() -> Any:
    plugins.clear()
    yield
    plugins.clear()


class TestPluginFrom:
    def test_a_decorated_function(self) -> None:
        plugin = plugins.plugin_from(decorated, "tests.test_plugins:decorated")

        assert plugin.kind == "function"
        assert plugin.name == "decorated"
        assert plugin.target == "tests.test_plugins:decorated"

    def test_an_actor_class_reads_its_class_attributes(self) -> None:
        plugin = plugins.plugin_from(Probe)

        assert plugin.kind == "actor"
        assert plugin.name == "probe"
        assert plugin.description == "A probe."
        assert plugin.capabilities == ("probing",)
        assert plugin.requires == {"ram_mb": 64}

    def test_a_docstring_describes_an_actor_without_a_description(self) -> None:
        class Quiet(Actor):
            """First line is the description.

            The rest is not.
            """

            async def handle_message(self, msg: Message) -> None:
                return None

        assert plugins.plugin_from(Quiet).description == "First line is the description."

    def test_anything_else_is_refused_by_name(self) -> None:
        with pytest.raises(TypeError, match="json:dumps"):
            plugins.plugin_from(plugins.resolve_target("json:dumps"), "json:dumps")


class TestBuild:
    def test_a_function_plugin_builds_a_function_agent(self, tmp_path: Path) -> None:
        actor = plugins.plugin_from(decorated).build(persistence_dir=str(tmp_path))

        assert isinstance(actor, FunctionAgent)
        assert actor.name == "decorated"

    def test_an_actor_plugin_is_told_only_what_it_accepts(self, tmp_path: Path) -> None:
        provider = object()
        plain = plugins.plugin_from(Probe).build(
            name="p1", persistence_dir=str(tmp_path), llm_provider=provider, options={"x": 1}
        )
        rich = plugins.plugin_from(WithProvider).build(
            name="p2",
            persistence_dir=str(tmp_path),
            llm_provider=provider,
            options={"threshold": 2.5},
        )

        assert isinstance(plain, Probe) and plain.name == "p1"
        assert isinstance(rich, WithProvider)
        assert rich.llm_provider is provider
        assert rich.threshold == 2.5

    def test_a_recipe_is_what_the_catalogue_shows(self) -> None:
        recipe = plugins.plugin_from(Probe, "tests.test_plugins:Probe").recipe()

        assert recipe["type"] == "native"
        assert recipe["name"] == "probe"
        assert recipe["plugin"] == "tests.test_plugins:Probe"
        assert recipe["requires"] == {"ram_mb": 64}
        assert callable(recipe["factory"])


class TestDiscover:
    def test_targets_named_in_the_environment_are_loaded(self) -> None:
        found = plugins.discover(env="tests.test_plugins:Probe, tests.test_plugins:decorated")

        assert set(found) == {"probe", "decorated"}
        assert found["probe"].target == "tests.test_plugins:Probe"

    def test_a_bad_target_is_skipped_not_fatal(self, caplog: pytest.LogCaptureFixture) -> None:
        found = plugins.discover(env="no.such.module:Thing json:dumps tests.test_plugins:Probe")

        assert set(found) == {"probe"}
        assert "no.such.module:Thing" in caplog.text
        assert "json:dumps" in caplog.text
        assert "PYTHONPATH" in caplog.text

    def test_targets_are_split_on_commas_and_spaces_once_each(self) -> None:
        assert plugins.targets_in(" a:b, c:d a:b\nc:d ") == ["a:b", "c:d"]

    def test_a_registered_object_survives_a_refresh(self) -> None:
        plugins.register(decorated)

        assert "decorated" in plugins.discover(env="")
        assert "decorated" in plugins.discover(env="", refresh=True)

    def test_by_target_finds_only_registered_targets(self) -> None:
        plugins.discover(env="tests.test_plugins:Probe")

        assert plugins.for_target("tests.test_plugins:Probe") is not None
        assert plugins.for_target("tests.test_plugins:WithProvider") is None
        assert plugins.for_target("") is None
        assert plugins.for_name("probe") is not None

    def test_entry_points_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = SimpleNamespace(value="tests.test_plugins:decorated", load=lambda: decorated)
        monkeypatch.setattr(plugins, "_entry_points", lambda: [entry])

        found = plugins.discover(env="")

        assert found["decorated"].target == "tests.test_plugins:decorated"


class TestTheCatalogue:
    def test_plugins_appear_beside_the_packaged_recipes(self) -> None:
        from wactorz.agents.catalog_agent import _build_native_catalog, get_native_factory

        plugins.register(Probe)

        catalog = _build_native_catalog()

        assert catalog["probe"]["plugin"] == "tests.test_plugins:Probe"
        assert get_native_factory("probe") is not None

    def test_a_packaged_name_is_not_shadowed(self, caplog: pytest.LogCaptureFixture) -> None:
        from wactorz.agents.catalog_agent import _build_native_catalog

        packaged = next(iter(_build_native_catalog()))

        class Impostor(Actor):
            AGENT_NAME = packaged

            async def handle_message(self, msg: Message) -> None:
                return None

        plugins.register(Impostor)
        catalog = _build_native_catalog()

        assert "plugin" not in catalog[packaged]
        assert "shadowed" in caplog.text
