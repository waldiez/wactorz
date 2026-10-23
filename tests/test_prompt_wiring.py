"""The choice of prompt fragments travels from the configuration to main, and from
main to the planners it spawns.

The templates and the Home Assistant fragment are tested on their own in
``test_prompt_fragments.py``. What is tested here is that the choice reaches
the model: that main built for an installation without Home Assistant sends
prompts that never mention it and refuses its intents, that the planner neither
gathers nor shows Home Assistant's live context there, and that the same test
that starts the Home Assistant agents is the one that decides.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from wactorz import app as app_module
from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.main.actor import MainActor
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.prompts.fragments import DEFAULT_FRAGMENTS
from wactorz.agents.prompts.home_assistant_prompts import HOME_ASSISTANT_FRAGMENT
from wactorz.config import CONFIG


def _settings(**overrides: object) -> Any:
    """A replaced CONFIG, typed as Any because dataclasses.replace returns the
    frozen settings type the module-level functions take."""
    return dataclasses.replace(CONFIG, **overrides)


class TestTheConfigurationDecides:
    def test_home_assistant_is_included_exactly_when_its_agents_start(self) -> None:
        configured = _settings(ha_agents="auto", ha_url="http://ha:8123", ha_token="t")
        unconfigured = _settings(ha_agents="auto", ha_url="", ha_token="")
        forced_off = _settings(ha_agents="off", ha_url="http://ha:8123", ha_token="t")
        forced_on = _settings(ha_agents="on", ha_url="", ha_token="")

        assert app_module.prompt_fragments_for(configured) == DEFAULT_FRAGMENTS
        assert HOME_ASSISTANT_FRAGMENT not in app_module.prompt_fragments_for(unconfigured)
        assert HOME_ASSISTANT_FRAGMENT not in app_module.prompt_fragments_for(forced_off)
        assert HOME_ASSISTANT_FRAGMENT in app_module.prompt_fragments_for(forced_on)

    def test_it_agrees_with_the_agents_in_every_case(self) -> None:
        for ha_agents in ("auto", "on", "off"):
            for url, token in (("http://ha:8123", "t"), ("", ""), ("http://ha:8123", "")):
                settings = _settings(ha_agents=ha_agents, ha_url=url, ha_token=token)
                included = HOME_ASSISTANT_FRAGMENT in app_module.prompt_fragments_for(settings)
                assert included == app_module.home_assistant_agents_enabled(settings)


def _main(tmp_path: Path, provider: FakeProvider | None = None, **kwargs: Any) -> MainActor:
    return MainActor(llm_provider=provider, persistence_dir=str(tmp_path), **kwargs)


class TestMain:
    def test_built_without_home_assistant_its_system_prompt_never_mentions_it(
        self, tmp_path: Path
    ) -> None:
        main = _main(tmp_path, prompt_fragments=())
        main._rebuild_system_prompt()

        assert "Home Assistant" not in main.system_prompt
        assert "ha_actuator" not in main.system_prompt
        assert main.system_prompt.startswith("== MAIN-SPECIFIC OVERRIDE")
        assert "== CURRENTLY RUNNING AGENTS" in main.system_prompt

    def test_built_directly_it_speaks_of_everything(self, tmp_path: Path) -> None:
        """Library code and tests construct a MainActor without a say in the
        matter; they get the fully configured installation's prompts."""
        main = _main(tmp_path)
        main._rebuild_system_prompt()

        assert main._prompt_fragments == DEFAULT_FRAGMENTS
        assert "Home Assistant" in main.system_prompt

    async def test_a_home_assistant_intent_is_read_as_other_without_home_assistant(
        self, tmp_path: Path
    ) -> None:
        """The model is never offered HA or ACTUATE, and if it answers one anyway
        the router does not take it, so no turn reaches a handler that would
        reply "Home Assistant is not configured"."""
        provider = FakeProvider(intent="HA")
        main = _main(tmp_path, provider, prompt_fragments=())

        assert await main._classify_intent("list my devices") == "OTHER"
        system_sent, _ = provider.calls[-1]
        assert "ACTUATE" not in system_sent
        assert "Home Assistant" not in system_sent
        assert "PIPELINE or OTHER" in system_sent

    async def test_the_same_intent_is_taken_with_home_assistant(self, tmp_path: Path) -> None:
        provider = FakeProvider(intent="HA")
        main = _main(tmp_path, provider)

        assert await main._classify_intent("list my devices") == "HA"
        system_sent, _ = provider.calls[-1]
        assert "ACTUATE, HA, PIPELINE, or OTHER" in system_sent

    async def test_fact_extraction_is_asked_without_home_assistant_examples(
        self, tmp_path: Path
    ) -> None:
        provider = FakeProvider()
        main = _main(tmp_path, provider, prompt_fragments=())

        await main._extract_and_save_facts("my name is Sam", "Hello Sam")

        system_sent, _ = provider.calls[-1]
        assert system_sent.startswith("You extract durable facts")
        assert "home assistant" not in system_sent.lower()
        assert "entity" not in system_sent.lower().replace("identity", "")


def _planner(tmp_path: Path, **kwargs: Any) -> PlannerAgent:
    # The design prompt ends with the request, so this key is always present
    # and the model answers with an empty plan, which is parsed and returned.
    provider = FakeProvider(script={"USER REQUEST": "[]"})
    planner = PlannerAgent(
        llm_provider=provider, persistence_dir=str(tmp_path), auto_terminate=False, **kwargs
    )

    async def _bus() -> tuple[str, str]:
        return "", ""

    async def _urls(_task: str) -> str:
        return ""

    planner._gather_topic_bus_context = _bus  # pyright: ignore[reportAttributeAccessIssue]
    planner._gather_notification_urls = _urls  # pyright: ignore[reportAttributeAccessIssue]
    return planner


def _sent(planner: PlannerAgent) -> str:
    assert isinstance(planner.llm, FakeProvider)
    return planner.llm.calls[-1][1][-1]["content"]


class TestThePlanner:
    async def test_without_home_assistant_its_live_context_is_neither_gathered_nor_shown(
        self, tmp_path: Path
    ) -> None:
        planner = _planner(tmp_path, prompt_fragments=())

        async def _never() -> tuple[str, bool, str]:
            raise AssertionError("Home Assistant was asked for its entities")

        planner._gather_ha_entities = _never  # pyright: ignore[reportAttributeAccessIssue]

        assert (
            await planner._decompose_pipeline("when the sensor reads above 30 notify me", []) == []
        )
        prompt = _sent(planner)
        assert prompt.startswith("You are designing reactive automation pipelines")
        assert "HOME ASSISTANT ENTITIES" not in prompt
        assert "CAMERA STREAM URLS" not in prompt
        assert "CAMERA SNAPSHOT URLS" not in prompt
        assert "Home Assistant" not in prompt
        assert "ha_actuator" not in prompt
        assert "═══ NOTIFICATION URLS ═══" in prompt
        assert "═══ USER REQUEST ═══" in prompt

    async def test_with_home_assistant_its_live_context_is_shown(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path)

        async def _entities() -> tuple[str, bool, str]:
            return "  light.desk", True, "  light.desk"

        async def _cameras(_task: str, _entities: str) -> tuple[str, str]:
            return "streams", "snapshots"

        async def _feasible(*_args: Any, **_kwargs: Any) -> None:
            return None

        planner._gather_ha_entities = _entities  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_camera_context = _cameras  # pyright: ignore[reportAttributeAccessIssue]
        planner._check_ha_feasibility = _feasible  # pyright: ignore[reportAttributeAccessIssue]

        assert (
            await planner._decompose_pipeline("when the door opens turn on the desk lamp", []) == []
        )
        prompt = _sent(planner)
        assert "═══ HOME ASSISTANT ENTITIES ═══\n  light.desk" in prompt
        assert "═══ CAMERA STREAM URLS ═══\nstreams" in prompt
        assert "═══ CAMERA SNAPSHOT URLS ═══\nsnapshots" in prompt
        assert 'TYPE 1 — "ha_actuator"' in prompt

    def test_built_directly_it_plans_for_everything(self, tmp_path: Path) -> None:
        assert _planner(tmp_path)._prompt_fragments == DEFAULT_FRAGMENTS


class TestMainHandsItsFragmentsToThePlanner:
    async def test_the_planner_is_spawned_with_mains_fragments(self, tmp_path: Path) -> None:
        main = _main(tmp_path, FakeProvider(), prompt_fragments=())
        spawned: list[dict[str, Any]] = []

        async def _spawn(_cls: type, **kwargs: Any) -> None:
            spawned.append(kwargs)
            return  # no planner: _run_planner gives up and returns None

        main.spawn = _spawn  # pyright: ignore[reportAttributeAccessIssue]

        assert await main._run_planner("when X then Y", is_pipeline_intent=True) is None
        assert len(spawned) == 1
        assert spawned[0]["prompt_fragments"] == ()

    @pytest.mark.parametrize("fragments", [(), DEFAULT_FRAGMENTS])
    def test_what_main_was_built_with_is_what_it_hands_on(
        self, tmp_path: Path, fragments: tuple[Any, ...]
    ) -> None:
        main = _main(tmp_path, prompt_fragments=fragments)
        assert main._prompt_fragments == fragments
