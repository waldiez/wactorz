"""Which built-ins start: the Home Assistant agents only where Home Assistant is, and
a minimal profile with none of the orchestration for a deployment that brings
its own agents."""

import dataclasses

import pytest

from wactorz import app as app_module
from wactorz.cli import get_args
from wactorz.config import CONFIG, _env_choice


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

    def test_run_builds_the_command_line_it_stands_for(self) -> None:
        argv = app_module.run_argv(
            web=False,
            minimal=True,
            monitor_port=9000,
            mqtt_broker="broker",
            mqtt_port=1884,
            llm="none",
        )

        args = get_args(argv)
        assert args.no_monitor and args.minimal
        assert args.monitor_port == 9000
        assert args.mqtt_broker == "broker" and args.mqtt_port == 1884
        assert args.llm == "none"

    def test_defaults_add_nothing(self) -> None:
        assert app_module.run_argv(True, False, None, None, None, None) == []
