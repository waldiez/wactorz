"""Choosing the orchestrator: a script's, a deployment's, main's, or the model-free one.

A deployment supplies its own orchestrator two ways, ``run(orchestrator=...)``
from a script and ``WACTORZ_ORCHESTRATOR=package.module:attr`` from the
environment, and either wins over the default. The custom orchestrator here is
the ten-line one the library guide promises: three methods, no base class, and a
turn typed into the dashboard reaches it.
"""

import dataclasses
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

import wactorz
from wactorz import app as app_module
from wactorz import orchestration
from wactorz.config import CONFIG
from wactorz.core.registry import ActorRegistry
from wactorz.errors import StartupError
from wactorz.orchestration import DirectOrchestrator, MainOrchestrator
from wactorz.web import chat, runtime


class Echo:
    """A whole orchestrator: it repeats the turn, and says which channel it came in on."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        self.turns.append((text, channel))
        return f"echo: {text}"

    async def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        yield await self.handle_turn(text, channel=channel, user=user)

    def commands(self) -> frozenset[str]:
        return frozenset({"/echo"})


class NeedsTheRegistry(Echo):
    """An orchestrator built by the start, handed the registry to reach the agents with."""

    def __init__(self, registry: ActorRegistry) -> None:
        super().__init__()
        self.registry = registry


def build(registry: ActorRegistry) -> Echo:
    """A factory function as a target: called with the registry, returns the orchestrator."""
    return NeedsTheRegistry(registry)


#: Targets a test names in ``WACTORZ_ORCHESTRATOR``; this module is importable.
HERE = __name__
NOT_AN_ORCHESTRATOR = 42


class _Registry:
    def find_by_name(self, name: str) -> Any:
        return None

    def all_actors(self) -> list[Any]:
        return []


def _system() -> Any:
    return SimpleNamespace(registry=_Registry())


@pytest.fixture(autouse=True)
def _clean_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "orchestrator", None)
    monkeypatch.setattr(app_module, "CONFIG", dataclasses.replace(CONFIG, orchestrator_env=""))


def _env(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    monkeypatch.setattr(app_module, "CONFIG", dataclasses.replace(CONFIG, orchestrator_env=target))


class TestWhatAScriptHandsOver:
    def test_an_orchestrator_object_is_installed_as_it_is(self) -> None:
        echo = Echo()

        chosen = app_module.install_orchestrator(_system(), None, requested=echo)

        assert chosen is echo
        assert runtime.orchestrator is echo

    def test_a_class_is_called_with_the_registry(self) -> None:
        system = _system()

        chosen = app_module.install_orchestrator(system, None, requested=NeedsTheRegistry)

        assert isinstance(chosen, NeedsTheRegistry)
        assert chosen.registry is system.registry

    def test_a_factory_function_is_called_with_the_registry(self) -> None:
        chosen = app_module.install_orchestrator(_system(), None, requested=build)

        assert isinstance(chosen, NeedsTheRegistry)

    def test_it_wins_over_main_and_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, f"{HERE}:build")
        echo = Echo()

        chosen = app_module.install_orchestrator(_system(), object(), requested=echo)

        assert chosen is echo

    def test_something_that_is_not_one_is_refused_by_name(self) -> None:
        with pytest.raises(StartupError, match="int, which is neither an Orchestrator"):
            app_module.install_orchestrator(_system(), None, requested=NOT_AN_ORCHESTRATOR)

    def test_a_factory_that_builds_something_else_is_refused(self) -> None:
        with pytest.raises(StartupError, match="built str, which is not an Orchestrator"):
            app_module.install_orchestrator(_system(), None, requested=lambda registry: "no")

    def test_a_factory_that_fails_is_refused_with_its_error(self) -> None:
        def boom(registry: ActorRegistry) -> Echo:
            raise ValueError("no model configured")

        with pytest.raises(StartupError, match="could not be built: no model configured"):
            app_module.install_orchestrator(_system(), None, requested=boom)


class TestWhatTheEnvironmentNames:
    def test_a_target_is_loaded_and_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch, f"{HERE}:NeedsTheRegistry")

        chosen = app_module.install_orchestrator(_system(), object(), requested=None)

        assert isinstance(chosen, NeedsTheRegistry)
        assert runtime.orchestrator is chosen

    def test_an_instance_target_is_used_as_it_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch, f"{HERE}:READY")

        assert app_module.install_orchestrator(_system(), None) is READY

    def test_a_module_that_does_not_exist_names_the_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, "no.such.module:thing")

        with pytest.raises(
            StartupError, match=r"WACTORZ_ORCHESTRATOR names 'no\.such\.module:thing'"
        ):
            app_module.install_orchestrator(_system(), None)

    def test_a_malformed_target_names_the_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch, "justaname")

        with pytest.raises(StartupError, match="'justaname'"):
            app_module.install_orchestrator(_system(), None)

    def test_a_target_that_is_not_an_orchestrator_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(monkeypatch, f"{HERE}:NOT_AN_ORCHESTRATOR")

        with pytest.raises(StartupError, match="NOT_AN_ORCHESTRATOR"):
            app_module.install_orchestrator(_system(), None)

    def test_the_setting_is_read_from_the_environment(self) -> None:
        # In a process of its own: the configuration is read once, at import.
        env = {**os.environ, "WACTORZ_ORCHESTRATOR": " pkg.mod:thing "}
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from wactorz.config import CONFIG; print(CONFIG.orchestrator_env)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )

        assert out.stdout.strip() == "pkg.mod:thing"


READY = Echo()


class TestTheDefaults:
    def test_main_when_main_runs(self) -> None:
        assert isinstance(app_module.install_orchestrator(_system(), object()), MainOrchestrator)

    def test_the_model_free_one_otherwise(self) -> None:
        assert isinstance(app_module.install_orchestrator(_system(), None), DirectOrchestrator)


class TestFromRunAndServe:
    def test_serve_args_carries_the_object_only_when_given(self) -> None:
        echo = Echo()

        assert app_module.serve_args(orchestrator=echo).orchestrator is echo
        assert not hasattr(app_module.serve_args(), "orchestrator")

    async def test_serve_hands_it_to_the_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        async def fake_app(args: Any, **_kwargs: Any) -> None:
            seen["orchestrator"] = args.orchestrator

        monkeypatch.setattr(app_module, "app", fake_app)
        echo = Echo()

        await wactorz.serve([], minimal=True, web=False, orchestrator=echo)

        assert seen["orchestrator"] is echo

    def test_run_hands_it_to_serve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        async def fake_serve(*_args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)

        monkeypatch.setattr(app_module, "serve", fake_serve)
        echo = Echo()

        wactorz.run([], minimal=True, web=False, orchestrator=echo)

        assert seen["orchestrator"] is echo


class TestTheTurnReachesIt:
    async def test_a_dashboard_turn_reaches_the_custom_orchestrator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ten-line orchestrator, selected the way run() selects it, answers the dashboard."""
        system = _system()
        monkeypatch.setattr(runtime, "registry", system.registry)
        echo = cast(Echo, app_module.install_orchestrator(system, None, requested=Echo()))
        said: list[str] = []

        async def reply(text: str) -> None:
            said.append(text)

        await chat.route_chat("turn on the lights", reply)
        await chat.route_chat("/echo now", reply)
        await chat.route_chat("/plans", reply)

        assert echo.turns == [("turn on the lights", "dashboard"), ("/echo now", "dashboard")]
        assert said == [
            "echo: turn on the lights",
            "echo: /echo now",
            "Unknown command. Type /help for available commands.",
        ]


class TestTheExports:
    def test_the_package_exports_the_seam(self) -> None:
        assert wactorz.Orchestrator is orchestration.Orchestrator
        assert wactorz.DirectOrchestrator is DirectOrchestrator
        assert wactorz.MainOrchestrator is MainOrchestrator
        assert {"Orchestrator", "DirectOrchestrator", "MainOrchestrator"} <= set(wactorz.__all__)
        assert isinstance(Echo(), wactorz.Orchestrator)
