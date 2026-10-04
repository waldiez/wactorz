"""Which built-ins start: the Home Assistant agents only where Home Assistant is, and
a minimal profile with none of the orchestration for a deployment that brings
its own agents."""

import asyncio
import dataclasses
import os
import sys
from pathlib import Path

import pytest

from wactorz import app as app_module
from wactorz.agents.function_agent import spec_of
from wactorz.cli import get_args
from wactorz.config import CONFIG, _env_choice
from wactorz.errors import StartupError
from wactorz.monitoring import log_buffer


def _settings(**overrides: object) -> object:
    return dataclasses.replace(CONFIG, **overrides)


class TestHomeAssistantAgents:
    def test_auto_follows_the_configuration(self) -> None:
        on = _settings(ha_agents="auto", ha_url="http://ha:8123", ha_token="t")
        off = _settings(ha_agents="auto", ha_url="", ha_token="")
        half = _settings(ha_agents="auto", ha_url="http://ha:8123", ha_token="")

        assert app_module.home_assistant_agents_enabled(on)  # type: ignore[arg-type]  # a replaced CONFIG
        assert not app_module.home_assistant_agents_enabled(off)  # type: ignore[arg-type]  # a replaced CONFIG
        assert not app_module.home_assistant_agents_enabled(half)  # type: ignore[arg-type]  # a replaced CONFIG

    def test_on_and_off_decide_outright(self) -> None:
        forced = _settings(ha_agents="on", ha_url="", ha_token="")
        refused = _settings(ha_agents="off", ha_url="http://ha:8123", ha_token="t")

        assert app_module.home_assistant_agents_enabled(forced)  # type: ignore[arg-type]  # a replaced CONFIG
        assert not app_module.home_assistant_agents_enabled(refused)  # type: ignore[arg-type]  # a replaced CONFIG

    @pytest.mark.parametrize(
        ("raw", "expected"), [("ON", "on"), ("off", "off"), ("maybe", "auto"), ("", "auto")]
    )
    def test_the_setting_takes_only_its_three_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ) -> None:
        monkeypatch.setenv("WACTORZ_TEST_CHOICE", raw)

        assert _env_choice("WACTORZ_TEST_CHOICE", "auto", ("auto", "on", "off")) == expected


class TestTheMinimalProfile:
    def test_the_flag_parses(self) -> None:
        assert get_args(["--minimal"]).minimal
        assert not get_args([]).minimal

    def test_serve_builds_the_settings_its_arguments_stand_for(self) -> None:
        args = app_module.serve_args(
            web=False,
            minimal=True,
            monitor_port=9000,
            mqtt_broker="broker",
            mqtt_port=1884,
            llm="none",
        )

        assert args.no_monitor and args.minimal
        assert args.monitor_port == 9000
        assert args.mqtt_broker == "broker" and args.mqtt_port == 1884
        assert args.llm == "none"

    def test_defaults_are_the_command_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Whatever the process was started with is not what a library call means.
        monkeypatch.setattr(sys, "argv", ["prog", "--minimal", "--no-monitor", "--llm", "none"])

        assert vars(app_module.serve_args()) == vars(get_args([]))
        assert not app_module.serve_args().minimal


class TestServe:
    """`serve` is the entry point for a host with a loop of its own; `run` wraps it."""

    async def test_serve_registers_what_it_is_given_and_awaits_the_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz import plugins
        from wactorz.agents.function_agent import agent

        plugins.clear()
        seen: dict[str, object] = {}

        async def fake_app(
            args: object, *, handle_signals: bool = True, configure_logging: bool = True
        ) -> None:
            seen["args"] = args
            seen["handle_signals"] = handle_signals
            seen["configure_logging"] = configure_logging

        monkeypatch.setattr(app_module, "app", fake_app)

        @agent
        def probe(payload: dict) -> None:
            return None

        await app_module.serve([probe], minimal=True, web=False)

        assert "probe" in plugins.discover()
        # A host's signals and logging are its own.
        assert seen["handle_signals"] is False
        assert seen["configure_logging"] is False
        args = seen["args"]
        assert getattr(args, "minimal") and getattr(args, "no_monitor")
        plugins.clear()

    async def test_serve_sets_the_state_dir_for_the_run_without_touching_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from wactorz.core import paths

        monkeypatch.setenv("WACTORZ_STATE_DIR", "/from/the/environment")
        seen: dict[str, str] = {}

        async def fake_app(args: object, **_: object) -> None:
            seen["state_dir"] = paths.resolve_state_dir()

        monkeypatch.setattr(app_module, "app", fake_app)

        await app_module.serve(state_dir=tmp_path / "state")

        assert seen["state_dir"] == str(tmp_path / "state")
        assert os.environ["WACTORZ_STATE_DIR"] == "/from/the/environment"
        # Over once the run is: the next one starts from the environment again.
        assert paths.resolve_state_dir() == "/from/the/environment"

    def test_run_takes_the_signals_and_the_logging_and_the_same_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        async def fake_app(
            args: object, *, handle_signals: bool = True, configure_logging: bool = True
        ) -> None:
            seen["handle_signals"] = handle_signals
            seen["configure_logging"] = configure_logging
            seen["port"] = getattr(args, "monitor_port")

        monkeypatch.setattr(app_module, "app", fake_app)

        app_module.run(monitor_port=9100)

        assert seen == {"handle_signals": True, "configure_logging": True, "port": 9100}

    def test_the_package_exports_the_library_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import inspect

        import wactorz

        assert inspect.iscoroutinefunction(wactorz.serve)
        assert callable(wactorz.run)
        assert wactorz.spec_of is spec_of
        assert wactorz.StartupError is StartupError
        # Whatever another test left built: outside a run there is no system.
        monkeypatch.setattr(app_module, "_current_system", None)
        assert wactorz.system() is None
        running = object()
        monkeypatch.setattr(app_module, "_current_system", running)
        assert wactorz.system() is running


class TestAppAsALibraryCall:
    """`app()` refuses with an exception, so a host decides what an unstartable config means."""

    @pytest.fixture
    def quiet_app(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """`app()` with everything around the refusal check stubbed out."""
        calls = {"setup_logging": 0}

        def fake_setup_logging() -> None:
            calls["setup_logging"] += 1

        monkeypatch.setattr(app_module, "setup_logging", fake_setup_logging)
        monkeypatch.setattr(app_module, "exposure_refusal", lambda host, key: "exposed: no key")
        return calls

    async def test_a_refusal_is_a_startup_error_not_an_exit(
        self, quiet_app: dict[str, int]
    ) -> None:
        with pytest.raises(StartupError, match="exposed: no key"):
            await app_module.app(get_args([]), configure_logging=False)

        assert quiet_app["setup_logging"] == 0, "a host's logging is left alone"

    async def test_a_refused_start_leaves_the_process_as_it_found_it(
        self, quiet_app: dict[str, int]
    ) -> None:
        """The loop-lag thread and the log buffer's root handler go with the refusal."""
        hosts_own = asyncio.create_task(asyncio.sleep(30))
        try:
            with pytest.raises(StartupError):
                await app_module.app(get_args([]), configure_logging=False)

            assert app_module._loop_lag._thread is None  # pyright: ignore[reportPrivateUsage]
            assert log_buffer.get_buffer() is None
            assert not hosts_own.cancelled(), "a task of the host's is not ours to stop"
        finally:
            hosts_own.cancel()

    async def test_the_command_configures_logging(self, quiet_app: dict[str, int]) -> None:
        with pytest.raises(StartupError):
            await app_module.app(get_args([]))

        assert quiet_app["setup_logging"] == 1

    def test_the_command_exits_with_status_one_on_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from wactorz import cli

        async def refusing_app(args: object) -> None:
            raise StartupError("TELEGRAM_BOT_TOKEN not set.")

        monkeypatch.setattr("wactorz.app.app", refusing_app)
        monkeypatch.setattr(sys, "argv", ["wactorz"])

        with pytest.raises(SystemExit) as exited:
            cli.main()

        assert exited.value.code == 1

    @pytest.mark.parametrize(
        ("interface", "variable"),
        [("telegram", "TELEGRAM_BOT_TOKEN"), ("discord", "DISCORD_BOT_TOKEN")],
    )
    async def test_a_missing_token_is_a_startup_error_and_the_system_is_stopped(
        self, monkeypatch: pytest.MonkeyPatch, interface: str, variable: str
    ) -> None:
        """The system is built before the interface asks for its token, so it is stopped again."""
        from wactorz.interfaces import chat_interfaces

        class FakeSystem:
            _running = False

        fake_system, fake_main = FakeSystem(), object()
        stopped: list[object] = []

        async def built(args: object, spare: object = None) -> tuple[object, object, None]:
            return fake_system, fake_main, None

        async def shut_down(system: object, spare: object = None) -> None:
            stopped.append(system)

        monkeypatch.setattr(app_module, "exposure_refusal", lambda host, key: "")
        monkeypatch.setattr(app_module, "prepare_broker_files", lambda: "")
        monkeypatch.setattr(app_module, "_build_system_or_stop", built)
        monkeypatch.setattr(app_module, "_shut_down", shut_down)
        monkeypatch.setattr(chat_interfaces, "build_social_companions", lambda main, primary: [])
        monkeypatch.setattr(
            app_module,
            "CONFIG",
            dataclasses.replace(CONFIG, telegram_token="", discord_token=""),
        )

        with pytest.raises(StartupError, match=variable):
            await app_module.app(
                get_args(["--interface", interface, "--no-monitor"]), configure_logging=False
            )

        assert stopped == [fake_system]


class TestTheProviderUnderTheMinimalProfile:
    """`minimal=True` needs no model, so none is built unless one is named."""

    @pytest.fixture
    def factory(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Records the provider names `create_provider` is asked for; answers none."""
        from wactorz import llm_factory

        asked: list[str] = []

        def fake_create(name: str, model: str | None = None) -> None:
            asked.append(name)
            if name == "anthropic":
                raise ModuleNotFoundError("No module named 'anthropic'", name="anthropic")
            return

        monkeypatch.setattr(llm_factory, "create_provider", fake_create)
        return asked

    async def test_minimal_builds_no_provider(self, factory: list[str]) -> None:
        assert await app_module._build_provider(get_args(["--minimal"]), minimal=True) is None
        assert factory == ["none"]

    async def test_minimal_with_a_named_provider_builds_it(self, factory: list[str]) -> None:
        await app_module._build_provider(get_args(["--minimal", "--llm", "ollama"]), minimal=True)
        assert factory == ["ollama"]

    async def test_the_full_profile_builds_the_configured_one(self, factory: list[str]) -> None:
        with pytest.raises(StartupError) as refused:
            await app_module._build_provider(get_args([]), minimal=False)

        assert factory == [CONFIG.llm_provider]
        # The message names both ways out.
        assert "wactorz[anthropic]" in str(refused.value)
        assert "LLM_PROVIDER=none" in str(refused.value)
