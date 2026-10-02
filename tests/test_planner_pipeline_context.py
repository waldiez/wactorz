"""What the pipeline planner gathers before it designs a standing rule.

A pipeline is designed once and then runs unattended, so the prompt that
designs it has to be grounded in what exists: real Home Assistant entity ids,
resolvable camera URLs, the field names topics actually carry, and where a
notification may go. Each gatherer here degrades to a section that says what
is missing rather than failing the plan, because a plan built on "unknown" is
still more use than no plan.

The checks around the design are advisory in one direction only: a rule
conflict never blocks, and a feasibility checker that cannot answer lets the
request through.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wactorz import config
from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner import pipeline as pipeline_mod
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.pipeline import (
    StepOutcome,
    active_rule_lines,
    camera_candidates,
    camera_sections,
    describe_rule_conflicts,
    ha_entity_ids_in,
    skips_ha_feasibility,
)
from wactorz.core import topic_bus
from wactorz.core.actor import MessageType
from wactorz.core.integrations.home_assistant import ha_helper
from wactorz.core.topic_bus import TopicBus, TopicContract


class _Actor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"id-{name}"


class _Registry:
    def __init__(self, *names: str) -> None:
        self._actors = [_Actor(n) for n in names]

    def find_by_name(self, name: str) -> _Actor | None:
        return next((a for a in self._actors if a.name == name), None)

    def all_actors(self) -> list[_Actor]:
        return list(self._actors)


class _Main:
    def __init__(
        self,
        rules: dict[str, dict[str, Any]] | None = None,
        urls: dict[str, str] | None = None,
        broken: bool = False,
    ) -> None:
        self._rules = rules or {}
        self._urls = urls or {}
        self._broken = broken
        self.saved_rules: list[dict[str, Any]] = []
        self.spawn_registry: list[dict[str, Any]] = []

    def get_pipeline_rules(self) -> dict[str, dict[str, Any]]:
        if self._broken:
            raise RuntimeError("database locked")
        return self._rules

    def get_notification_urls(self) -> dict[str, str]:
        return dict(self._urls)

    def save_pipeline_rule(self, rule: dict[str, Any]) -> None:
        self.saved_rules.append(rule)

    def _save_to_spawn_registry(self, cfg: dict[str, Any]) -> None:
        self.spawn_registry.append(cfg)


def _planner(tmp_path: Path, script: dict[str, str] | None = None, **kwargs: Any) -> PlannerAgent:
    llm = FakeProvider(script=script or {})
    return PlannerAgent(
        llm_provider=llm, persistence_dir=str(tmp_path), auto_terminate=False, **kwargs
    )


def _calls(planner: PlannerAgent) -> list[tuple[str, list[dict[str, Any]]]]:
    assert isinstance(planner.llm, FakeProvider)
    return planner.llm.calls


def _prompt(planner: PlannerAgent) -> str:
    return _calls(planner)[-1][1][-1]["content"]


@pytest.fixture(name="planner")
def planner_fixture(tmp_path: Path) -> PlannerAgent:
    return _planner(tmp_path)


def _use_main(monkeypatch: pytest.MonkeyPatch, main: _Main | None) -> None:
    monkeypatch.setattr(pipeline_mod, "find_main_actor", lambda _registry: main)


@pytest.fixture(name="instant_sleep")
def instant_sleep_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settle delays exist for real agents starting up; nothing starts here."""
    real_sleep = asyncio.sleep

    async def _instant(_delay: float, *args: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr(pipeline_mod.asyncio, "sleep", _instant)


class TestRuleStorageOnThePlanner:
    def test_no_rules_is_an_empty_list(self, planner: PlannerAgent) -> None:
        assert planner._load_pipeline_rules() == []

    def test_saving_a_rule_with_the_same_id_replaces_it(self, planner: PlannerAgent) -> None:
        planner._save_pipeline_rule({"rule_id": "r1", "task": "old"})
        planner._save_pipeline_rule({"rule_id": "r2", "task": "other"})
        planner._save_pipeline_rule({"rule_id": "r1", "task": "new"})

        assert planner._load_pipeline_rules() == [
            {"rule_id": "r2", "task": "other"},
            {"rule_id": "r1", "task": "new"},
        ]


class TestRunPipelineAdvisories:
    @staticmethod
    def _stub(planner: PlannerAgent, conflict: str, note: str = "") -> list[Any]:
        executed: list[Any] = []

        async def _resolve(task: str) -> tuple[str, str]:
            return task + " (resolved)", note

        async def _decompose(task: str, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"name": "watcher"}]

        async def _conflicts(task: str, plan: list[dict[str, Any]]) -> str:
            return conflict

        async def _execute(plan: list[dict[str, Any]], task: str, resolution_note: str = "") -> str:
            executed.append((task, resolution_note))
            return "summary"

        planner._resolve_data_references = _resolve  # pyright: ignore[reportAttributeAccessIssue]
        planner._decompose_pipeline = _decompose  # pyright: ignore[reportAttributeAccessIssue]
        planner._check_rule_conflicts = _conflicts  # pyright: ignore[reportAttributeAccessIssue]
        planner._execute_pipeline_plan = _execute  # pyright: ignore[reportAttributeAccessIssue]
        return executed

    async def test_a_conflict_is_prepended_to_the_summary(self, planner: PlannerAgent) -> None:
        executed = self._stub(planner, conflict="duplicates rule r1", note="topic `x`")

        result = await planner._run_pipeline("when the door opens", [])

        assert result == "⚠️ Heads up — duplicates rule r1\n\nsummary"
        assert executed == [("when the door opens (resolved)", "topic `x`")]

    async def test_no_conflict_leaves_the_summary_alone(self, planner: PlannerAgent) -> None:
        self._stub(planner, conflict="")

        assert await planner._run_pipeline("when the door opens", []) == "summary"

    async def test_a_proposal_carries_the_conflict_as_a_warning(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, plan_only=True)
        self._stub(planner, conflict="contradicts r2")

        envelope = json.loads(await planner._run_pipeline("when the door opens", []))

        assert envelope["warnings"] == "contradicts r2"
        assert envelope["task"] == "when the door opens (resolved)"

    async def test_an_approved_plan_may_list_its_steps_as_agents(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, approved_plan={"agents": [{"name": "a"}]})
        executed = self._stub(planner, conflict="")

        await planner._run_pipeline("t", [])

        assert executed == [("t", "")]


class TestExecutePipelinePlan:
    async def test_outcomes_are_gathered_and_the_rule_saved(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main()
        _use_main(monkeypatch, main)
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        planner._auto_terminate = True
        outcomes = {
            "new": StepOutcome(wired="**new** — watches", spawned="new", rule_agent="new"),
            "old": StepOutcome(wired="**old** (already active)", rule_agent="old"),
            "bad": StepOutcome(wired="**bad** — spawn failed: x"),
        }
        bootstrapped: list[Any] = []

        async def _step(step: dict[str, Any], task: str) -> StepOutcome:
            return outcomes[step["name"]]

        async def _bootstrap(task: str, plan: list[dict[str, Any]] | None = None) -> None:
            bootstrapped.append(task)

        planner._spawn_pipeline_step = _step  # pyright: ignore[reportAttributeAccessIssue]
        planner._bootstrap_ha_entity_states = _bootstrap  # pyright: ignore[reportAttributeAccessIssue]

        summary = await planner._execute_pipeline_plan(
            [{"name": "new"}, {"name": "old"}, {"name": "bad"}], "when x", resolution_note="n"
        )
        await asyncio.sleep(0)

        assert "1. **new** — watches" in summary
        assert "3. **bad** — spawn failed: x" in summary
        assert "Spawned: new" in summary
        assert main.saved_rules[0]["agents"] == ["new", "old"]
        assert bootstrapped == ["when x"]
        assert planner._auto_terminate is False, "a planner that spawned a rule must stay"

    async def test_nothing_spawned_saves_no_rule_and_bootstraps_nothing(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main()
        _use_main(monkeypatch, main)
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]

        async def _step(step: dict[str, Any], task: str) -> StepOutcome:
            return StepOutcome()

        planner._spawn_pipeline_step = _step  # pyright: ignore[reportAttributeAccessIssue]

        summary = await planner._execute_pipeline_plan([{"name": "x"}], "when x")

        assert "no agents could be spawned" in summary
        assert main.saved_rules == []

    async def test_a_spawned_step_lists_what_it_listens_to(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch, instant_sleep: None
    ) -> None:
        _use_main(monkeypatch, None)
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]

        async def _spawn(cfg: dict[str, Any]) -> _Actor:
            return _Actor(cfg["name"])

        planner._spawn_agent = _spawn  # pyright: ignore[reportAttributeAccessIssue]
        step = {
            "name": "door",
            "description": "watches the door",
            "spawn_config": {"type": "dynamic", "mqtt_topics": ["a/b", "c/d"]},
        }

        outcome = await planner._spawn_pipeline_step(step, "t")

        assert outcome.wired == "**door** — watches the door\n  listens: a/b, c/d"

    def test_restore_registration_needs_a_registry_and_main(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner._register_for_restore({"type": "dynamic"}, "a", "t")
        planner._persist_pipeline_rule("t", ["a"])

        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, None)
        planner._register_for_restore({"type": "dynamic"}, "a", "t")

        main = _Main()
        _use_main(monkeypatch, main)
        planner._register_for_restore({"type": "dynamic"}, "a", "x" * 300)

        assert main.spawn_registry == [
            {"type": "dynamic", "name": "a", "_rule": True, "_rule_task": "x" * 200}
        ]


class TestRuleConflicts:
    RULES: ClassVar[dict[str, dict[str, Any]]] = {
        "r1": {"rule_id": "r1", "task": "when the door opens turn on the hall light"}
    }
    VERDICT = json.dumps(
        {
            "conflict": True,
            "items": [{"rule_id": "r1", "kind": "duplicate", "reason": "same trigger and action"}],
        }
    )

    async def test_without_an_llm_there_is_no_advisory(self, tmp_path: Path) -> None:
        planner = PlannerAgent(llm_provider=None, persistence_dir=str(tmp_path))

        assert await planner._check_rule_conflicts("t", []) == ""

    async def test_without_active_rules_the_model_is_not_asked(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, _Main())

        assert await planner._check_rule_conflicts("t", []) == ""
        assert _calls(planner) == []

    async def test_a_duplicate_is_described_with_the_rule_it_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner = _planner(tmp_path, script={"Respond with ONLY a JSON object": self.VERDICT})
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, _Main(rules=self.RULES))

        advisory = await planner._check_rule_conflicts("when the door opens light the hall", [])

        assert advisory.startswith("This pipeline may overlap with existing rules:")
        assert 'Duplicate of rule [r1] ("when the door opens turn on the hall light")' in advisory
        assert "[r1] when the door opens" in _prompt(planner)
        assert planner.total_cost_usd > 0

    async def test_an_unreadable_verdict_is_no_advisory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner = _planner(tmp_path, script={"Respond with ONLY": "I think it is fine"})
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, _Main(rules=self.RULES))

        assert await planner._check_rule_conflicts("t", []) == ""

    def test_active_rules_degrade_to_none(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert planner._active_rules() == []

        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, None)
        assert planner._active_rules() == []

        _use_main(monkeypatch, _Main(broken=True))
        assert planner._active_rules() == []


class TestRuleConflictRendering:
    def test_rules_without_a_task_are_left_out(self) -> None:
        lines, by_id = active_rule_lines(
            [{"rule_id": "a", "task": "line one\nline two"}, {"rule_id": "b", "task": "  "}, {}]
        )

        assert lines == ["- [a] line one line two"]
        assert list(by_id) == ["a"]

    def test_at_most_thirty_rules_are_offered(self) -> None:
        rules = [{"rule_id": str(i), "task": f"rule {i}"} for i in range(40)]

        lines, _ = active_rule_lines(rules)

        assert len(lines) == 30

    @pytest.mark.parametrize(
        "data", [None, [], {"conflict": False}, {"conflict": True, "items": ["x", 3]}]
    )
    def test_no_clear_conflict_renders_nothing(self, data: object) -> None:
        assert describe_rule_conflicts(data, {}) == ""

    def test_a_contradiction_for_an_unknown_rule_still_reads(self) -> None:
        text = describe_rule_conflicts(
            {"conflict": True, "items": [{"rule_id": "zz", "kind": "contradiction"}]}, {}
        )

        assert text.endswith("• May contradict rule [zz]")

    def test_at_most_five_items_are_listed(self) -> None:
        items = [{"rule_id": str(i), "kind": "duplicate"} for i in range(8)]

        text = describe_rule_conflicts({"conflict": True, "items": items}, {})

        assert text.count("•") == 5


class TestDecomposePipeline:
    PLAN = json.dumps(
        [
            {
                "name": "door-watcher",
                "description": "watches",
                "spawn_config": {"type": "dynamic", "code": "pass", "mqtt_topics": ["a"]},
            }
        ]
    )

    @staticmethod
    def _grounded(
        planner: PlannerAgent,
        *,
        ha_text: str = "",
        samples: str = "",
        verdict: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        checked: list[str] = []

        async def _ha() -> tuple[str, bool, str]:
            return ha_text, bool(ha_text), ha_text or "(none)"

        async def _cameras(task: str, text: str) -> tuple[str, str]:
            return "CAMERA-SECTION", "SNAPSHOT-SECTION"

        async def _bus() -> tuple[str, str]:
            return "BUS-SECTION", samples

        async def _urls(task: str) -> str:
            return "URL-SECTION"

        async def _feasible(
            task: str, section: str, topics: str = ""
        ) -> list[dict[str, Any]] | None:
            checked.append(task)
            return verdict

        planner._gather_ha_entities = _ha  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_camera_context = _cameras  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_topic_bus_context = _bus  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_notification_urls = _urls  # pyright: ignore[reportAttributeAccessIssue]
        planner._check_ha_feasibility = _feasible  # pyright: ignore[reportAttributeAccessIssue]
        return checked

    async def test_without_an_llm_there_is_no_plan(self, tmp_path: Path) -> None:
        planner = PlannerAgent(llm_provider=None, persistence_dir=str(tmp_path))

        assert await planner._decompose_pipeline("t", []) == []

    async def test_the_prompt_carries_every_section_and_the_plan_comes_back(
        self, tmp_path: Path
    ) -> None:
        planner = _planner(tmp_path, script={"when the door opens": self.PLAN})
        checked = self._grounded(planner, samples="SAMPLES-SECTION")

        plan = await planner._decompose_pipeline("when the door opens", [])

        assert [step["name"] for step in plan] == ["door-watcher"]
        prompt = _prompt(planner)
        for section in (
            "BUS-SECTION",
            "SAMPLES-SECTION",
            "URL-SECTION",
            "CAMERA-SECTION",
            "SNAPSHOT-SECTION",
        ):
            assert section in prompt
        assert checked == [], "no HA entities means nothing to check feasibility against"

    async def test_without_samples_the_sample_instructions_are_left_out(
        self, tmp_path: Path
    ) -> None:
        planner = _planner(tmp_path, script={"when the door opens": self.PLAN})
        self._grounded(planner)

        await planner._decompose_pipeline("when the door opens", [])

        assert "LIVE TOPIC SAMPLES" not in _prompt(planner)

    async def test_an_infeasible_request_returns_the_refusal_without_designing(
        self, tmp_path: Path
    ) -> None:
        planner = _planner(tmp_path)
        refusal = [{"_feasibility_error": "no lamp"}]
        checked = self._grounded(planner, ha_text="  light.hall", verdict=refusal)

        assert await planner._decompose_pipeline("turn on the lamp", []) == refusal
        assert checked == ["turn on the lamp"]
        assert _calls(planner) == []

    async def test_a_feasible_request_goes_on_to_design(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"turn on the lamp": self.PLAN})
        self._grounded(planner, ha_text="  light.hall", verdict=None)

        assert len(await planner._decompose_pipeline("turn on the lamp", [])) == 1

    async def test_a_camera_request_skips_the_feasibility_check(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"camera": self.PLAN})
        checked = self._grounded(planner, ha_text="  camera.porch", verdict=[{"x": 1}])

        await planner._decompose_pipeline("post to discord when the camera sees a cat", [])

        assert checked == []

    async def test_an_answer_that_is_not_a_plan_is_no_plan(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"door": '{"not": "a list"}'})
        self._grounded(planner)

        assert await planner._decompose_pipeline("when the door opens", []) == []

    async def test_an_unparseable_answer_is_no_plan(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"door": "Sorry, I cannot."})
        self._grounded(planner)

        assert await planner._decompose_pipeline("when the door opens", []) == []


class TestNotificationUrls:
    @pytest.fixture(autouse=True)
    def _no_env_webhook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, discord_webhook_url=""))
        monkeypatch.setattr(pipeline_mod, "CONFIG", config.CONFIG)

    async def test_with_nothing_stored_the_model_is_told_to_use_a_placeholder(
        self, planner: PlannerAgent
    ) -> None:
        section = await planner._gather_notification_urls("notify me")

        assert "none stored" in section
        assert "WEBHOOK_URL_REQUIRED" in section

    async def test_urls_stored_on_main_are_offered(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, _Main(urls={"slack": "https://hooks.slack.com/services/x"}))

        section = await planner._gather_notification_urls("notify me")

        assert "  slack: https://hooks.slack.com/services/x" in section

    @pytest.mark.parametrize(
        ("url", "service"),
        [
            ("https://discord.com/api/webhooks/1/abc", "discord"),
            ("https://hooks.slack.com/services/T/B/C", "slack"),
            ("https://api.telegram.org/bot123/sendMessage", "telegram"),
        ],
    )
    async def test_a_url_named_in_the_task_is_offered_without_trailing_punctuation(
        self, planner: PlannerAgent, url: str, service: str
    ) -> None:
        section = await planner._gather_notification_urls(f"when x, post to {url}.")

        assert f"  {service}: {url}\n" in section + "\n"

    async def test_an_environment_webhook_is_named_but_its_value_is_not_shown(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "https://discord.com/api/webhooks/9/secret"
        monkeypatch.setattr(
            pipeline_mod, "CONFIG", replace(config.CONFIG, discord_webhook_url=secret)
        )

        section = await planner._gather_notification_urls("notify me on discord")

        assert 'os.environ["DISCORD_WEBHOOK_URL"]' in section
        assert secret not in section

    async def test_a_url_in_the_task_wins_over_the_environment(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pipeline_mod,
            "CONFIG",
            replace(config.CONFIG, discord_webhook_url="https://discord.com/api/webhooks/9/env"),
        )

        section = await planner._gather_notification_urls(
            "post to https://discord.com/api/webhooks/1/task"
        )

        assert "DISCORD_WEBHOOK_URL" not in section
        assert "webhooks/1/task" in section


class TestTopicBusContext:
    @pytest.fixture(name="bus")
    def bus_fixture(self, monkeypatch: pytest.MonkeyPatch) -> TopicBus:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        return bus

    async def test_without_contracts_the_section_explains_how_to_get_them(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_bus, "_topic_bus", None)

        section, samples = await planner._gather_topic_bus_context()

        assert section.startswith("No topic contracts registered yet.")
        assert samples == ""

    async def test_observed_samples_are_offered_with_their_field_names(
        self, planner: PlannerAgent, bus: TopicBus
    ) -> None:
        contract = TopicContract(name="thermo", publishes=["sensors/temp"])
        contract.update_observed("sensors/temp", {"temp": 21.5})
        bus.register_contract(contract)

        section, samples = await planner._gather_topic_bus_context()

        assert "thermo" in section
        assert "Topic: sensors/temp  (published by thermo)" in samples
        assert "{'temp': 'float'}" in samples

    async def test_without_observed_samples_live_topics_are_sampled(
        self, planner: PlannerAgent, bus: TopicBus
    ) -> None:
        bus.register_contract(TopicContract(name="thermo", publishes=["sensors/temp"]))

        async def _sample(_bus: Any) -> list[str]:
            return ["  Topic: sensors/temp (live)"]

        planner._sample_live_topics = _sample  # pyright: ignore[reportAttributeAccessIssue]

        _, samples = await planner._gather_topic_bus_context()

        assert samples.endswith("  Topic: sensors/temp (live)")

    async def test_nothing_sampled_leaves_the_samples_empty(
        self, planner: PlannerAgent, bus: TopicBus
    ) -> None:
        bus.register_contract(TopicContract(name="thermo", publishes=["sensors/temp"]))

        async def _sample(_bus: Any) -> list[str]:
            return []

        planner._sample_live_topics = _sample  # pyright: ignore[reportAttributeAccessIssue]

        assert (await planner._gather_topic_bus_context())[1] == ""

    async def test_a_broken_bus_is_reported_in_the_section(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> TopicBus:
            raise RuntimeError("not initialised")

        monkeypatch.setattr(topic_bus, "get_topic_bus", _broken)

        section, _ = await planner._gather_topic_bus_context()

        assert section == "TopicBus unavailable: not initialised"


class TestHomeAssistantEntities:
    ENTITIES: ClassVar[dict[str, Any]] = {
        "entities": [
            {"entity_id": "light.hall", "name": "Hall", "platform": "hue"},
            {"entity_id": "sensor.t", "name": "sensor.t"},
            {"name": "no id"},
        ]
    }

    @staticmethod
    def _delegate(planner: PlannerAgent, result: Any) -> list[str]:
        asked: list[str] = []

        async def _delegate(agent: str, task: str, timeout: float = 60.0) -> Any:
            asked.append(task)
            if isinstance(result, Exception):
                raise result
            return result

        planner._delegate = _delegate  # pyright: ignore[reportAttributeAccessIssue]
        return asked

    @staticmethod
    def _direct(planner: PlannerAgent, text: str) -> list[int]:
        used: list[int] = []

        async def _fetch() -> tuple[str, bool]:
            used.append(1)
            return text, bool(text)

        planner._fetch_ha_entities_directly = _fetch  # pyright: ignore[reportAttributeAccessIssue]
        return used

    async def test_the_agents_list_is_formatted_one_entity_per_line(
        self, planner: PlannerAgent
    ) -> None:
        planner._registry = _Registry("home-assistant-agent")  # pyright: ignore[reportAttributeAccessIssue]
        asked = self._delegate(planner, self.ENTITIES)
        direct = self._direct(planner, "unused")

        text, available, section = await planner._gather_ha_entities()

        assert asked == ["list_entities"]
        assert text == "  light.hall  name=Hall  platform=hue\n  sensor.t"
        assert available is True
        assert section == text
        assert direct == []

    @pytest.mark.parametrize(
        "result", [None, {"error": "HA down"}, {"entities": []}, RuntimeError("boom")]
    )
    async def test_an_agent_that_cannot_answer_falls_back_to_a_direct_fetch(
        self, planner: PlannerAgent, result: Any
    ) -> None:
        planner._registry = _Registry("home-assistant-agent")  # pyright: ignore[reportAttributeAccessIssue]
        self._delegate(planner, result)
        direct = self._direct(planner, "  light.kitchen")

        text, available, _ = await planner._gather_ha_entities()

        assert direct == [1]
        assert (text, available) == ("  light.kitchen", True)

    async def test_nothing_reachable_says_so_in_the_section(self, planner: PlannerAgent) -> None:
        self._direct(planner, "")

        text, available, section = await planner._gather_ha_entities()

        assert (text, available) == ("", False)
        assert "HA not reachable" in section


class TestDirectHomeAssistantFetch:
    DEVICES: ClassVar[list[dict[str, Any]]] = [
        {
            "area": "Hall",
            "entities": [
                {"entity_id": "light.hall", "friendly_name": "Hall lamp", "state": "on"},
                {"entity_id": "", "name": "ghost"},
            ],
        },
        {"entities": [{"entity_id": "sensor.t", "name": "Temp"}]},
    ]

    @staticmethod
    def _configured(monkeypatch: pytest.MonkeyPatch, url: str, token: str) -> None:
        monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, ha_url=url, ha_token=token))

    async def test_without_configuration_nothing_is_fetched(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configured(monkeypatch, "", "")

        assert await planner._fetch_ha_entities_directly() == ("", False)

    async def test_devices_are_flattened_with_area_and_state(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configured(monkeypatch, "http://ha:8123/", " token ")
        seen: list[tuple[str, str]] = []

        async def _fetch(url: str, token: str, include_states: bool = False) -> Any:
            seen.append((url, token))
            return self.DEVICES

        monkeypatch.setattr(ha_helper, "fetch_devices_entities_with_location", _fetch)

        text, available = await planner._fetch_ha_entities_directly()

        assert seen == [("http://ha:8123", "token")]
        assert text == "  light.hall  name=Hall lamp  area=Hall  state=on\n  sensor.t  name=Temp"
        assert available is True

    async def test_a_failed_fetch_is_unavailable(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configured(monkeypatch, "http://ha:8123", "token")

        async def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("connection refused")

        monkeypatch.setattr(ha_helper, "fetch_devices_entities_with_location", _fail)

        assert await planner._fetch_ha_entities_directly() == ("", False)


class TestCameraUrls:
    @staticmethod
    def _ha_agent(planner: PlannerAgent, answers: dict[tuple[str, str], Any]) -> list[Any]:
        asked: list[Any] = []

        async def _delegate(agent: str, payload: dict[str, Any], timeout: float = 60.0) -> Any:
            key = (payload["operation"], payload["camera_entity_id"])
            asked.append(key)
            return answers.get(key)

        planner._delegate_with_payload = _delegate  # pyright: ignore[reportAttributeAccessIssue]
        return asked

    async def test_stream_and_snapshot_urls_are_resolved_for_named_cameras(
        self, planner: PlannerAgent
    ) -> None:
        self._ha_agent(
            planner,
            {
                ("get_camera_stream_url", "camera.porch"): {
                    "data": {"streams": {"mjpeg_proxy": "http://ha/mjpeg", "hls": "http://ha/hls"}}
                },
                ("get_camera_snapshot_url", "camera.porch"): {
                    "data": {"snapshot_url": "http://ha/snap"}
                },
            },
        )
        entities = "  camera.porch  name=Porch\n  camera.garage\n  light.hall"

        streams, snapshots = await planner._gather_camera_context("watch the porch", entities)

        assert "camera.porch: http://ha/mjpeg" in streams
        assert "camera.porch: http://ha/snap" in snapshots
        assert "camera.garage" not in streams

    async def test_a_camera_that_cannot_be_resolved_is_skipped(self, planner: PlannerAgent) -> None:
        asked = self._ha_agent(
            planner,
            {
                ("get_camera_stream_url", "camera.porch"): {"error": "unsupported"},
                ("get_camera_stream_url", "camera.garage"): {"data": {"streams": {}}},
                ("get_camera_snapshot_url", "camera.garage"): {"error": "nope"},
            },
        )

        streams, snapshots = await planner._gather_camera_context(
            "use the camera", "  camera.porch\n  camera.garage"
        )

        assert "none resolved" in streams
        assert "none resolved" in snapshots
        assert ("get_camera_snapshot_url", "camera.porch") not in asked

    async def test_no_cameras_means_nothing_is_asked(self, planner: PlannerAgent) -> None:
        asked = self._ha_agent(planner, {})

        await planner._gather_camera_context("use the camera", "  light.hall")

        assert asked == []

    async def test_a_failing_agent_leaves_the_urls_unresolved(self, planner: PlannerAgent) -> None:
        async def _broken(agent: str, payload: dict[str, Any], timeout: float = 60.0) -> Any:
            raise RuntimeError("HA agent crashed")

        planner._delegate_with_payload = _broken  # pyright: ignore[reportAttributeAccessIssue]

        assert await planner._fetch_camera_urls("porch", "  camera.porch") == ({}, {})


class TestCameraHelpers:
    def test_sections_list_each_resolved_url(self) -> None:
        streams, snapshots = camera_sections({"camera.a": "rtsp://a"}, {"camera.a": "http://s"})

        assert streams.endswith("  camera.a: rtsp://a")
        assert "  camera.a: http://s" in snapshots
        assert "os.environ['HA_TOKEN']" in snapshots

    def test_candidates_match_words_from_the_task(self) -> None:
        cams = ["camera.front_door", "camera.garage"]

        assert camera_candidates(cams, "when someone is at the front door") == ["camera.front_door"]

    def test_a_generic_camera_request_offers_the_first_five(self) -> None:
        cams = [f"camera.c{i}" for i in range(8)]

        assert camera_candidates(cams, "watch my webcam") == cams[:5]

    def test_a_request_that_is_not_about_cameras_offers_none(self) -> None:
        assert camera_candidates(["camera.c1"], "turn on the heating") == []


class TestHaFeasibilityHelpers:
    @pytest.mark.parametrize(
        ("task", "skips"),
        [
            ("post to discord when the camera sees a cat", True),
            ("when the camera sees a cat turn on the light", False),
            ("when it is dark turn on the light", False),
            ("log a warning message when the door opens", False),
        ],
    )
    def test_only_clearly_non_ha_requests_skip(self, task: str, skips: bool) -> None:
        assert skips_ha_feasibility(task) is skips

    def test_entity_ids_are_found_in_code_actions_topics_and_the_task(self) -> None:
        plan = [
            {
                "spawn_config": {
                    "code": "if state['entity_id'] == 'light.hall': agent.state.value = 1",
                    "actions": [{"entity_id": "Switch.Fan"}, {}],
                    "mqtt_topics": [
                        "homeassistant/state_changes/binary_sensor/binary_sensor.door",
                        "custom/other",
                    ],
                }
            },
            {},
        ]

        ids = ha_entity_ids_in(plan, "also watch sensor.temp and light.hall")

        assert ids == ["light.hall", "switch.fan", "binary_sensor.door", "sensor.temp"]

    def test_without_a_plan_only_the_task_is_read(self) -> None:
        assert ha_entity_ids_in(None, "turn on fan.bedroom") == ["fan.bedroom"]


class TestCheckHaFeasibility:
    async def test_without_an_llm_planning_continues(self, tmp_path: Path) -> None:
        planner = PlannerAgent(llm_provider=None, persistence_dir=str(tmp_path))

        assert await planner._check_ha_feasibility("t", "") is None

    async def test_a_fenced_refusal_becomes_a_one_step_error_plan(self, tmp_path: Path) -> None:
        answer = '```json\n{"feasible": false, "reason": "no lamp exists"}\n```'
        planner = _planner(tmp_path, script={"turn on the lamp": answer})

        verdict = await planner._check_ha_feasibility("turn on the lamp", "  light.hall")

        assert verdict == [{"_feasibility_error": "no lamp exists"}]

    async def test_a_refusal_without_a_reason_gets_a_generic_one(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"lamp": '{"feasible": false}'})

        verdict = await planner._check_ha_feasibility("turn on the lamp", "")

        assert verdict == [
            {"_feasibility_error": "Cannot fulfill request with available HA entities."}
        ]

    async def test_the_checker_is_shown_what_running_agents_publish(self, tmp_path: Path) -> None:
        # A Flic button is no Home Assistant entity; shown only entities, the
        # checker refused "when the Desk button is double-clicked" as missing.
        planner = _planner(tmp_path, script={"Desk": '{"feasible": true}'})
        topics = "  [flic]\n    about     : Buttons: 'Desk' is custom/flic/bh16-f58317."

        await planner._check_ha_feasibility(
            "when the Desk button is double-clicked, toggle the lamp", "  light.hall", topics
        )

        prompt = _prompt(planner)
        assert "LIVE MQTT DATA FLOWS" in prompt
        assert "'Desk' is custom/flic/bh16-f58317" in prompt

    async def test_the_topics_gathered_for_design_reach_the_checker(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path)
        seen: list[str] = []

        async def _ha() -> tuple[str, bool, str]:
            return "  light.hall", True, "  light.hall"

        async def _cameras(task: str, ha: str) -> tuple[str, str]:
            return "", ""

        async def _bus() -> tuple[str, str]:
            return "BUS-SECTION", ""

        async def _urls(task: str) -> str:
            return ""

        async def _feasible(
            task: str, section: str, topics: str = ""
        ) -> list[dict[str, Any]] | None:
            seen.append(topics)
            return [{"_feasibility_error": "stop here"}]

        planner._gather_ha_entities = _ha  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_camera_context = _cameras  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_topic_bus_context = _bus  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_notification_urls = _urls  # pyright: ignore[reportAttributeAccessIssue]
        planner._check_ha_feasibility = _feasible  # pyright: ignore[reportAttributeAccessIssue]

        await planner._decompose_pipeline("toggle the lamp when the Desk button is pressed", [])

        assert seen == ["BUS-SECTION"]

    @pytest.mark.parametrize("answer", ['{"feasible": true}', "not json at all"])
    async def test_a_yes_or_an_unreadable_answer_lets_planning_continue(
        self, tmp_path: Path, answer: str
    ) -> None:
        planner = _planner(tmp_path, script={"lamp": answer})

        assert await planner._check_ha_feasibility("turn on the lamp", "") is None


class TestBootstrapEntityStates:
    PLAN: ClassVar[list[dict[str, Any]]] = [
        {"spawn_config": {"actions": [{"entity_id": "light.hall"}]}}
    ]

    async def test_a_plan_without_entities_is_not_bootstrapped(self, planner: PlannerAgent) -> None:
        planner._registry = _Registry("home-assistant-agent")  # pyright: ignore[reportAttributeAccessIssue]
        sent: list[Any] = []
        planner.send = lambda *a, **k: sent.append(a)  # pyright: ignore[reportAttributeAccessIssue]

        await planner._bootstrap_ha_entity_states("say hello", [])

        assert sent == []

    async def test_without_a_registry_or_ha_agent_nothing_is_sent(
        self, planner: PlannerAgent
    ) -> None:
        sent: list[Any] = []
        planner.send = lambda *a, **k: sent.append(a)  # pyright: ignore[reportAttributeAccessIssue]

        await planner._bootstrap_ha_entity_states("t", self.PLAN)
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        await planner._bootstrap_ha_entity_states("t", self.PLAN)

        assert sent == []

    async def test_the_ha_agent_is_asked_for_the_current_states(
        self, planner: PlannerAgent, instant_sleep: None
    ) -> None:
        planner._registry = _Registry("home-assistant-agent")  # pyright: ignore[reportAttributeAccessIssue]
        sent: list[tuple[str, MessageType, dict[str, Any]]] = []

        async def _send(target: str, msg_type: MessageType, payload: dict[str, Any]) -> bool:
            sent.append((target, msg_type, payload))
            planner._result_futures[payload["_task_id"]].set_result({"result": "published"})
            return True

        planner.send = _send  # pyright: ignore[reportAttributeAccessIssue]

        await planner._bootstrap_ha_entity_states("when x", self.PLAN)

        ((target, msg_type, payload),) = sent
        assert target == "id-home-assistant-agent"
        assert msg_type == MessageType.TASK
        assert payload["text"] == "get_entities_state light.hall"
        assert payload["_reply_to"] == planner.actor_id
        assert planner._result_futures == {}

    async def test_a_send_that_fails_is_logged_and_cleaned_up(
        self, planner: PlannerAgent, instant_sleep: None
    ) -> None:
        planner._registry = _Registry("home-assistant-agent")  # pyright: ignore[reportAttributeAccessIssue]
        logged: list[str] = []

        async def _log(message: str) -> None:
            logged.append(message)

        async def _send(*_args: Any) -> bool:
            raise RuntimeError("mailbox full")

        planner._log = _log  # pyright: ignore[reportAttributeAccessIssue]
        planner.send = _send  # pyright: ignore[reportAttributeAccessIssue]

        await planner._bootstrap_ha_entity_states("when x", self.PLAN)

        assert logged[-1] == "Bootstrap — error: mailbox full"
        assert planner._result_futures == {}
