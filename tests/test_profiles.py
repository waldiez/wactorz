"""Which built-ins start: the Home Assistant agents only where Home Assistant is, and
a minimal profile with none of the orchestration for a deployment that brings
its own agents."""

import asyncio
import dataclasses
import logging
import os
import sys
from pathlib import Path
from typing import Any

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
            seen["registered"] = "probe" in plugins.discover()

        monkeypatch.setattr(app_module, "app", fake_app)

        @agent
        def probe(payload: dict) -> None:
            return None

        await app_module.serve([probe], minimal=True, web=False)

        assert seen["registered"] is True
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


class TestTheInterfaceOfALibraryCall:
    """`serve` takes no stdin unless asked; `run`, for a script, keeps the command's interface."""

    @pytest.fixture
    def interface_seen(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        seen: list[object] = []

        async def fake_app(args: object, **_: object) -> None:
            seen.append(getattr(args, "interface"))

        monkeypatch.setattr(app_module, "app", fake_app)
        return seen

    async def test_serve_runs_no_interface_by_default(self, interface_seen: list[object]) -> None:
        await app_module.serve()

        assert interface_seen == [app_module.HEADLESS]

    async def test_serve_runs_the_interface_it_is_given(self, interface_seen: list[object]) -> None:
        await app_module.serve(interface="rest")

        assert interface_seen == ["rest"]

    def test_run_keeps_the_commands_interface(
        self, monkeypatch: pytest.MonkeyPatch, interface_seen: list[object]
    ) -> None:
        monkeypatch.setattr(app_module, "CONFIG", dataclasses.replace(CONFIG, interface="cli"))

        app_module.run()
        app_module.run(interface="rest")

        assert interface_seen == ["cli", "rest"]

    @pytest.fixture
    def built(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """`app()` with a fake system whose main exists, and a CLI that says if it was made."""
        from wactorz.interfaces import chat_interfaces

        record: dict[str, object] = {"cli": False, "ran": False}

        class FakeSystem:
            _running = False

            async def run_forever(self) -> None:
                record["ran"] = True

        class RecordingCLI:
            def __init__(self, main: object) -> None:
                record["cli"] = True

            async def run(self) -> None:
                return None

        fake_system = FakeSystem()

        async def build(args: object) -> tuple[object, object, None]:
            return fake_system, object(), None

        async def shut_down(system: object) -> None:
            return None

        monkeypatch.setattr(app_module, "exposure_refusal", lambda host, key: "")
        monkeypatch.setattr(app_module, "prepare_broker_files", lambda: "")
        monkeypatch.setattr(app_module, "_build_system_or_stop", build)
        monkeypatch.setattr(app_module, "_shut_down", shut_down)
        monkeypatch.setattr(chat_interfaces, "build_social_companions", lambda main, primary: [])
        monkeypatch.setattr(chat_interfaces, "CLIInterface", RecordingCLI)
        # A terminal, so the CLI would start if the interface asked for it.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        return record

    async def test_headless_reads_no_stdin_even_at_a_terminal(
        self, built: dict[str, object]
    ) -> None:
        args = app_module.serve_args(web=False, interface=app_module.HEADLESS)

        await app_module.app(args, configure_logging=False)

        assert built == {"cli": False, "ran": True}

    async def test_the_cli_still_starts_when_asked_for(self, built: dict[str, object]) -> None:
        args = app_module.serve_args(web=False, interface="cli")

        await app_module.app(args, configure_logging=False)

        assert built["cli"] is True

    async def test_an_unknown_interface_is_a_startup_error(self, built: dict[str, object]) -> None:
        args = app_module.serve_args(web=False, interface="carrier-pigeon")

        with pytest.raises(StartupError, match="carrier-pigeon"):
            await app_module.app(args, configure_logging=False)


class TestServeRegistersForTheRunOnly:
    """A second `serve` in the same process starts what it is given, not what the first was."""

    @pytest.fixture(autouse=True)
    def _fresh(self) -> Any:
        from wactorz import pipelines, plugins

        plugins.clear()
        pipelines.clear()
        yield
        plugins.clear()
        pipelines.clear()

    @pytest.fixture
    def started(self, monkeypatch: pytest.MonkeyPatch) -> list[set[str]]:
        from wactorz import pipelines, plugins

        runs: list[set[str]] = []

        async def fake_app(args: object, **_: object) -> None:
            runs.append(set(plugins.discover(env="")) | set(pipelines.discover(env="")))

        monkeypatch.setattr(app_module, "app", fake_app)
        return runs

    async def test_the_second_run_starts_only_its_own_agents(self, started: list[set[str]]) -> None:
        from wactorz import plugins
        from wactorz.agents.function_agent import agent

        @agent
        def first(payload: dict) -> None:
            return None

        @agent
        def second(payload: dict) -> None:
            return None

        await app_module.serve([first])
        await app_module.serve([second])

        assert started == [{"first"}, {"second"}]
        assert plugins.discover(env="") == {}

    async def test_a_name_registered_before_the_run_gets_its_plugin_back(
        self, started: list[set[str]]
    ) -> None:
        from wactorz import plugins
        from wactorz.agents.function_agent import agent

        @agent(name="shared")
        def before(payload: dict) -> None:
            return None

        @agent(name="shared")
        def during(payload: dict) -> None:
            return None

        kept = plugins.register(before)
        await app_module.serve([during])

        assert plugins.discover(env="")["shared"] is kept

    async def test_pipelines_handed_to_serve_are_for_that_run(
        self, started: list[set[str]]
    ) -> None:
        from wactorz import pipelines
        from wactorz.agents.function_agent import agent

        @agent(subscribes="in/x")
        def step(payload: dict) -> None:
            return None

        pipe = pipelines.pipeline("watch", steps=[step])
        pipelines.clear()  # as if it were declared elsewhere and handed over

        await app_module.serve(pipelines_=[pipe])

        assert "watch" in started[0]
        assert pipelines.discover(env="") == {}


class TestBuiltInNames:
    """An agent under a built-in's name would replace it in the supervisor; it is refused."""

    @pytest.fixture(autouse=True)
    def _fresh(self) -> Any:
        from wactorz import pipelines, plugins

        plugins.clear()
        pipelines.clear()
        yield
        plugins.clear()
        pipelines.clear()

    @pytest.mark.parametrize("name", ["main", "monitor", "installer", "catalog"])
    def test_a_plugin_under_a_built_in_name_is_not_started(
        self, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        from wactorz import plugins
        from wactorz.agents.function_agent import agent

        @agent(name=name)
        def impostor(payload: dict) -> None:
            return None

        @agent
        def honest(payload: dict) -> None:
            return None

        found = [plugins.plugin_from(impostor), plugins.plugin_from(honest)]
        with caplog.at_level(logging.ERROR):
            chosen = app_module.startable_plugins(found)

        assert [p.name for p in chosen] == ["honest"]
        assert f"'{name}' not started" in caplog.text

    def test_a_plugin_not_for_autostart_waits_as_before(self) -> None:
        from wactorz import plugins
        from wactorz.agents.function_agent import agent

        @agent(autostart=False)
        def later(payload: dict) -> None:
            return None

        assert app_module.startable_plugins([plugins.plugin_from(later)]) == []

    def test_a_pipeline_with_a_built_in_name_inside_is_not_started(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from wactorz import pipelines
        from wactorz.agents.function_agent import agent

        @agent(name="monitor", subscribes="in/x")
        def clashing(payload: dict) -> None:
            return None

        @agent(subscribes="in/y")
        def fine(payload: dict) -> None:
            return None

        bad = pipelines.pipeline("bad", steps=[clashing])
        good = pipelines.pipeline("good", steps=[fine])
        with caplog.at_level(logging.ERROR):
            chosen = app_module.startable_pipelines([bad, good])

        assert chosen == [good]
        assert "'bad' not started: monitor" in caplog.text


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

        async def built(args: object) -> tuple[object, object, None]:
            return fake_system, fake_main, None

        async def shut_down(system: object) -> None:
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


class TestTheLoopCheck:
    """A loop that cannot watch sockets is refused up front, with the way out."""

    async def test_the_running_loop_here_is_fine(self) -> None:
        assert app_module.loop_refusal(asyncio.get_running_loop()) == ""

    def test_a_loop_without_add_reader_is_refused_naming_the_uvicorn_flag(self) -> None:
        class ProactorEventLoop(asyncio.AbstractEventLoop):
            """What Windows' proactor loop is: the abstract add_reader, never overridden."""

        refused = app_module.loop_refusal(ProactorEventLoop())  # pyright: ignore[reportAbstractUsage]
        assert "ProactorEventLoop" in refused
        assert "--loop asyncio:SelectorEventLoop" in refused
        assert "WindowsSelectorEventLoopPolicy" in refused

    async def test_app_refuses_on_it_before_anything_is_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_module, "loop_refusal", lambda loop: "wrong loop")
        monkeypatch.setattr(app_module, "exposure_refusal", lambda host, key: "")
        built: list[object] = []

        async def build(args: object) -> None:
            built.append(args)

        monkeypatch.setattr(app_module, "_build_system_or_stop", build)

        with pytest.raises(StartupError, match="wrong loop"):
            await app_module.app(get_args([]), configure_logging=False)
        assert built == []


class TestWarningsStayVisibleInAHost:
    """A library call leaves logging alone, and a bare host still sees warnings.

    Against a logger of the test's own: pytest keeps capture handlers on the
    real root logger, so it is never bare here.
    """

    @pytest.fixture(autouse=True)
    def no_fallback_yet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from wactorz.monitoring import log_setup

        monkeypatch.setattr(log_setup, "_fallback", None)

    def test_a_bare_root_gets_a_stderr_handler_for_warnings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from wactorz.monitoring import log_setup

        root = logging.getLogger("tests.bare-host")
        root.handlers.clear()
        root.propagate = False
        handler = log_setup.install_fallback(root)
        assert handler is not None and root.handlers == [handler]
        assert handler.level == logging.WARNING
        assert log_setup.install_fallback(root) is None, "once"

        root.info("quiet")
        root.warning("the broker is exposed")
        err = capsys.readouterr().err
        assert "the broker is exposed" in err and "quiet" not in err

        log_setup.uninstall_fallback(root)
        assert root.handlers == []

    def test_a_host_with_its_own_handler_is_left_alone(self) -> None:
        from wactorz.monitoring import log_setup

        root = logging.getLogger("tests.configured-host")
        root.handlers.clear()
        hosts = logging.NullHandler()
        root.addHandler(hosts)
        assert log_setup.install_fallback(root) is None
        assert root.handlers == [hosts]

    async def test_app_adds_it_before_the_buffer_and_removes_it_at_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        monkeypatch.setattr(app_module, "install_fallback", lambda: order.append("fallback"))
        monkeypatch.setattr(app_module, "install_log_buffer", lambda: order.append("buffer"))
        monkeypatch.setattr(app_module, "uninstall_fallback", lambda: order.append("removed"))
        monkeypatch.setattr(app_module, "exposure_refusal", lambda host, key: "refused")

        with pytest.raises(StartupError):
            await app_module.app(get_args([]), configure_logging=False)

        assert order == ["fallback", "buffer", "removed"]

    async def test_the_command_does_not_need_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[str] = []
        monkeypatch.setattr(app_module, "setup_logging", lambda: called.append("setup"))
        monkeypatch.setattr(app_module, "install_fallback", lambda: called.append("fallback"))
        monkeypatch.setattr(app_module, "exposure_refusal", lambda host, key: "refused")

        with pytest.raises(StartupError):
            await app_module.app(get_args([]))

        assert called == ["setup"]


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
