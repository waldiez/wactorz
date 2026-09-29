"""The catalog: ready-made agents spawned by name.

Requests arrive in many shapes — a structured action, a `spawn` key, or a line
of text typed at `@catalog` — and every shape lands on the same three actions.
Names are resolved loosely, because people say "the weather agent" rather than
`weather-agent`, but a name that matches nothing is refused with the list of
what exists rather than guessed at.

A spawn is recorded in main's spawn registry, marked trusted, so the agent comes
back after a restart without going through the validator that generated code
faces. Missing dependencies go to the installer first; an installer that is not
running is not a reason to refuse the spawn.
"""

import asyncio
import importlib.metadata
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import catalog_agent
from wactorz.agents.catalog_agent import (
    CatalogAgent,
    _dependency_is_satisfied,
    _load_recipe,
    get_native_factory,
)
from wactorz.agents.dynamic import DynamicAgent
from wactorz.catalogue_agents.weather_agent import WeatherAgent
from wactorz.core.actor import Message, MessageType


class _Actor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"id-{name}"


class _Installer(_Actor):
    """Answers every install request straight away, through main's future."""

    def __init__(self, main: "_Main") -> None:
        super().__init__("installer")
        self._main = main
        self.requests: list[Message] = []

    async def receive(self, msg: Message) -> None:
        self.requests.append(msg)
        self._main._result_futures[msg.payload["_task_id"]].set_result({"ok": True})


class _Registry:
    def __init__(self, *actors: _Actor) -> None:
        self._actors = list(actors)

    def find_by_name(self, name: str) -> _Actor | None:
        return next((a for a in self._actors if a.name == name), None)


class _Main(_Actor):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__("main")
        self.llm = object()
        self._persistence_dir = tmp_path / "state" / "main"
        self._result_futures: dict[str, asyncio.Future[Any]] = {}
        self._agent_manifests: dict[str, dict[str, Any]] = {}
        self.spawn_registry: list[dict[str, Any]] = []

    def _save_to_spawn_registry(self, cfg: dict[str, Any]) -> None:
        self.spawn_registry.append(cfg)


class _Broker:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.topics.append(topic)


@pytest.fixture(name="catalog")
def catalog_fixture(tmp_path: Path) -> CatalogAgent:
    catalog = CatalogAgent(persistence_dir=str(tmp_path))
    catalog._mqtt_client = _Broker()
    return catalog


@pytest.fixture(name="main")
def main_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Main:
    main = _Main(tmp_path)
    monkeypatch.setattr(catalog_agent, "find_main_actor", lambda _registry: main)
    return main


class _Spawner:
    """Replaces `Actor.spawn`, recording what would have been started."""

    def __init__(self, result: Any = "spawned", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[type, dict[str, Any]]] = []

    async def __call__(self, actor_class: type, **kwargs: Any) -> Any:
        self.calls.append((actor_class, kwargs))
        if self.error:
            raise self.error
        return self.result


def _spawner(catalog: CatalogAgent, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Spawner:
    spawner = _Spawner(**kwargs)
    monkeypatch.setattr(catalog, "spawn", spawner)
    return spawner


def _topics(catalog: CatalogAgent) -> list[str]:
    broker = catalog._mqtt_client
    assert isinstance(broker, _Broker)
    return broker.topics


class TestDependencyCheck:
    def test_an_importable_module_is_satisfied(self) -> None:
        assert _dependency_is_satisfied("json")

    def test_a_missing_module_is_not(self) -> None:
        assert not _dependency_is_satisfied("definitely-not-installed-anywhere>=1.0")

    def test_extras_and_markers_are_ignored_when_importing(self) -> None:
        assert _dependency_is_satisfied("pytest[testing]>=1; python_version >= '3.8'")

    def test_an_exact_pin_must_match_the_installed_version(self) -> None:
        installed = importlib.metadata.version("pytest")

        assert _dependency_is_satisfied(f"pytest=={installed}")
        assert not _dependency_is_satisfied("pytest==0.0.1")

    def test_a_pin_on_a_module_with_no_distribution_is_not_satisfied(self) -> None:
        assert not _dependency_is_satisfied("json==1.0")

    def test_a_distribution_name_maps_to_its_import_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        imported: list[str] = []

        def _import(name: str) -> object:
            imported.append(name)
            return object()

        monkeypatch.setattr(catalog_agent.importlib, "import_module", _import)

        assert _dependency_is_satisfied("beautifulsoup4")
        assert _dependency_is_satisfied("some-package")
        assert imported == ["bs4", "some_package"]


class TestRecipeLoading:
    def test_a_recipe_file_that_does_not_exist_is_none(self) -> None:
        assert _load_recipe("no_such_recipe.py") is None

    def test_a_real_recipe_yields_its_program(self) -> None:
        code = _load_recipe("anomaly_detector_agent.py")

        assert isinstance(code, str)
        assert "async def" in code

    def test_a_file_that_cannot_be_imported_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_a: None)

        assert _load_recipe("anomaly_detector_agent.py") is None

    def test_a_file_that_raises_on_import_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: Any) -> Any:
            raise SyntaxError("bad recipe")

        monkeypatch.setattr(importlib.util, "spec_from_file_location", _boom)

        assert _load_recipe("anomaly_detector_agent.py") is None

    def test_native_factories_are_found_by_name(self) -> None:
        assert get_native_factory("weather-agent") is WeatherAgent
        assert get_native_factory("anomaly-detector") is None


class TestNameResolution:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("weather-agent", "weather-agent"),
            ("Weather Agent", "weather-agent"),
            ("anomaly_detector", "anomaly-detector"),
            ("smart-energy-agent", "smart-energy"),
            ("the energy smart one", "smart-energy"),
            ("teleporter", None),
            ("", None),
            ("agent", None),
        ],
    )
    def test_names_are_resolved_loosely(
        self, catalog: CatalogAgent, raw: str, expected: str | None
    ) -> None:
        assert catalog._resolve_name(raw) == expected


class TestRequestShapes:
    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "LIST"},
            "list",
            {"text": "list"},
            {"message": "something unrecognised"},
            None,
            42,
        ],
    )
    async def test_these_all_list_the_catalog(self, catalog: CatalogAgent, payload: Any) -> None:
        result = await catalog._handle(payload)

        assert result["message"].endswith("agent(s) available in catalog")
        assert result["show_experimental"] is False

    @pytest.mark.parametrize("payload", [{"action": "list", "filter": "beta"}, "list all"])
    async def test_a_list_can_ask_for_experimental_agents(
        self, catalog: CatalogAgent, payload: Any
    ) -> None:
        assert (await catalog._handle(payload))["show_experimental"] is True

    @pytest.mark.parametrize("payload", [{"action": "info", "agent": "weather"}, "info weather"])
    async def test_info_by_action_or_text(self, catalog: CatalogAgent, payload: Any) -> None:
        result = await catalog._handle(payload)

        assert result["ok"] is True
        assert result["message"] == "Recipe for 'weather-agent'"

    async def test_an_unknown_action_is_refused(self, catalog: CatalogAgent) -> None:
        result = await catalog._handle({"action": "delete"})

        assert result == {
            "ok": False,
            "message": "Unknown action 'delete'. Use: spawn | list | info",
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "spawn", "agent": "smart-energy"},
            {"spawn": "smart-energy"},
            "spawn smart-energy",
            {"query": "smart-energy"},
        ],
    )
    async def test_spawn_by_action_key_or_text(
        self, catalog: CatalogAgent, payload: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[str] = []

        async def _spawn(name: str, _payload: dict[str, Any]) -> dict[str, Any]:
            spawned.append(name)
            return {"ok": True}

        monkeypatch.setattr(catalog, "_action_spawn", _spawn)

        await catalog._handle(payload)

        assert spawned == ["smart-energy"]


class TestHandleMessage:
    async def test_only_a_task_is_answered(
        self, catalog: CatalogAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[Any] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, msg_type, payload))
            return True

        monkeypatch.setattr(catalog, "send", _send)

        await catalog.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="x"))
        await catalog.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="x",
                reply_to="main-id",
                payload={"action": "info", "agent": "weather", "_task_id": "t1"},
            )
        )
        await catalog.handle_message(Message(type=MessageType.TASK, sender_id="", payload=None))

        ((target, msg_type, result),) = sent
        assert (target, msg_type) == ("main-id", MessageType.RESULT)
        assert result["task"] == result["_task_id"] == "t1"


class TestInfo:
    def test_a_name_is_required(self, catalog: CatalogAgent) -> None:
        assert catalog._action_info("")["ok"] is False

    def test_an_unknown_name_lists_what_exists(self, catalog: CatalogAgent) -> None:
        result = catalog._action_info("teleporter")

        assert result["ok"] is False
        assert "weather-agent" in result["message"]

    def test_code_and_factory_are_never_returned(self, catalog: CatalogAgent) -> None:
        for name in ("weather-agent", "anomaly-detector"):
            recipe = catalog._action_info(name)["recipe"]

            assert "factory" not in recipe
            assert "code" not in recipe

    def test_an_experimental_recipe_says_so(self, catalog: CatalogAgent) -> None:
        assert "beta" in catalog._action_info("reachy-mini")["message"]


class TestSpawnRefusals:
    async def test_a_name_is_required(self, catalog: CatalogAgent) -> None:
        assert (await catalog._action_spawn("", {}))["ok"] is False

    async def test_an_unknown_name_is_refused(self, catalog: CatalogAgent) -> None:
        result = await catalog._action_spawn("teleporter", {})

        assert result["ok"] is False
        assert "not in catalog" in result["message"]

    async def test_without_a_registry_nothing_is_spawned(self, catalog: CatalogAgent) -> None:
        result = await catalog._action_spawn("smart-energy", {})

        assert result == {"ok": False, "message": "No registry available — cannot spawn"}

    async def test_an_agent_already_running_is_not_spawned_again(
        self, catalog: CatalogAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry(_Actor("smart-energy"))  # pyright: ignore[reportAttributeAccessIssue]
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("smart energy", {})

        assert result == {"ok": True, "message": "'smart-energy' is already running"}
        assert spawner.calls == []


class TestNativeSpawn:
    async def test_it_is_started_with_mains_model_and_saved_without_its_factory(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("weather", {})

        assert result == {
            "ok": True,
            "message": "'weather-agent' spawned and running",
            "agent": "weather-agent",
        }
        ((cls, kwargs),) = spawner.calls
        assert cls is WeatherAgent
        assert kwargs == {
            "name": "weather-agent",
            "persistence_dir": str(tmp_path / "state"),
            "llm_provider": main.llm,
        }
        (saved,) = main.spawn_registry
        assert "factory" not in saved
        assert saved["trusted"] is True

    async def test_without_main_it_still_spawns_but_is_not_saved(
        self, catalog: CatalogAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(catalog_agent, "find_main_actor", lambda _registry: None)
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("weather-agent", {})

        assert result["ok"] is True
        assert "llm_provider" not in spawner.calls[0][1]

    async def test_a_spawn_returning_nothing_is_a_failure(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _spawner(catalog, monkeypatch, result=None)

        result = await catalog._action_spawn("weather-agent", {})

        assert result == {"ok": False, "message": "Spawn returned no actor for 'weather-agent'"}
        assert main.spawn_registry == []

    async def test_a_recipe_without_a_factory_is_refused(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        catalog._catalog["weather-agent"]["factory"] = None
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("weather-agent", {})

        assert result == {"ok": False, "message": "Native recipe 'weather-agent' has no factory"}
        assert spawner.calls == []


class TestDynamicSpawn:
    async def test_a_recipe_without_dependencies_is_spawned_trusted(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("smart-energy", {})

        assert result["ok"] is True
        ((cls, kwargs),) = spawner.calls
        assert cls is DynamicAgent
        assert kwargs["trusted"] is True
        assert kwargs["code"] == catalog._catalog["smart-energy"]["code"]
        assert main.spawn_registry[0]["trusted"] is True
        assert _topics(catalog).count(f"agents/{catalog.actor_id}/logs") == 2

    async def test_installed_dependencies_skip_the_installer(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installer = _Installer(main)
        catalog._registry = _Registry(installer)  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(catalog_agent, "_dependency_is_satisfied", lambda _req: True)
        _spawner(catalog, monkeypatch)

        await catalog._action_spawn("timeseries-collector", {})

        assert installer.requests == []

    async def test_missing_dependencies_are_installed_before_spawning(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installer = _Installer(main)
        catalog._registry = _Registry(installer)  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(catalog_agent, "_dependency_is_satisfied", lambda req: req != "numpy")
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("anomaly-detector", {})

        (request,) = installer.requests
        assert request.payload["packages"] == ["numpy"]
        assert request.reply_to == main.actor_id
        assert result["ok"] is True
        assert len(spawner.calls) == 1

    async def test_a_missing_installer_does_not_block_the_spawn(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(catalog_agent, "_dependency_is_satisfied", lambda _req: False)
        spawner = _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("anomaly-detector", {})

        assert result["ok"] is True
        assert len(spawner.calls) == 1

    async def test_an_experimental_agent_raises_an_alert_and_warns_in_the_reply(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(catalog_agent, "_dependency_is_satisfied", lambda _req: True)
        _spawner(catalog, monkeypatch)

        result = await catalog._action_spawn("reachy-mini", {})

        assert f"agents/{catalog.actor_id}/alert" in _topics(catalog)
        assert "\n\nWarning: " in result["message"]

    async def test_a_spawn_returning_nothing_is_a_failure(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _spawner(catalog, monkeypatch, result=None)

        result = await catalog._action_spawn("smart-energy", {})

        assert result == {"ok": False, "message": "Spawn returned no actor for 'smart-energy'"}

    async def test_a_spawn_that_raises_is_reported(
        self, catalog: CatalogAgent, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _spawner(catalog, monkeypatch, error=RuntimeError("no memory"))

        result = await catalog._action_spawn("smart-energy", {})

        assert result == {"ok": False, "message": "Failed to spawn 'smart-energy': no memory"}


class TestOnStart:
    async def test_recipe_manifests_are_injected_into_main(
        self, catalog: CatalogAgent, main: _Main
    ) -> None:
        await catalog.on_start()

        assert set(main._agent_manifests) == set(catalog.list_recipes())
        weather = main._agent_manifests["weather-agent"]
        assert weather["spawnable"] is True
        assert weather["catalog"] == catalog.name
        assert main._agent_manifests["reachy-mini"]["experimental"] is True
        assert f"agents/{catalog.actor_id}/manifest" in _topics(catalog)

    async def test_without_main_it_gives_up_after_waiting(
        self, catalog: CatalogAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(catalog_agent, "find_main_actor", lambda _registry: None)
        waits: list[float] = []
        real_sleep = asyncio.sleep

        async def _sleep(delay: float) -> None:
            waits.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(catalog_agent.asyncio, "sleep", _sleep)

        await catalog.on_start()

        assert len(waits) == 20


class TestPublicSurface:
    def test_recipes_can_be_listed_and_read(self, catalog: CatalogAgent) -> None:
        assert "weather-agent" in catalog.list_recipes()
        recipe = catalog.get_recipe("weather-agent")
        assert recipe is not None
        assert recipe["type"] == "native"
        assert catalog.get_recipe("teleporter") is None

    def test_the_task_description_counts_recipes(self, catalog: CatalogAgent) -> None:
        count = len(catalog.list_recipes())

        assert catalog._current_task_description() == f"catalog ({count} recipes)"
