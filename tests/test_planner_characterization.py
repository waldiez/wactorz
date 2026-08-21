"""Characterization of PlannerAgent's pure helpers.

These tests record what the code does today, which is not always what it should
do. Where current behaviour is wrong, the test says so in its name and docstring
and asserts the wrong answer anyway: the point is that a restructure cannot
change any of it by accident. Correcting a recorded defect means editing the
assertion deliberately, which is the moment to decide it.
"""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.cache import CACHE_TTL_S, PLAN_CACHE_KEY
from wactorz.agents.planner.detection import is_pipeline_request
from wactorz.agents.planner.parsing import extract_json_array, extract_json_object, task_hash
from wactorz.agents.planner.validation import rewrite_aiomqtt_to_subscribe

PIPELINE_REQUESTS = [
    "when the door opens, send me a discord message",
    "monitor the temperature",
    "every 5 min publish a random number",
    "spawn an agent to log the mean",
    "remind me to call mum",
    "whenever motion is detected turn on the light",
    "pipeline: anything at all",
    "if the humidity drops then notify me",
    "check the sensor every 10 seconds",
    "at 5pm turn off the lamp",
]

ONE_SHOT_REQUESTS = [
    "what is the capital of France?",
    "summarize this document",
    "how many agents are running?",
    "what time is it?",
    "tell me a joke",
    "check the disk usage",
    "pipelines are great",
]

# Recorded, not endorsed: each of these is a one-shot question that the
# classifier calls a pipeline, and a pipeline verdict spawns a persistent agent.
MISREAD_AS_PIPELINE = [
    ("look at 5 files and tell me the total", "the clock-time pattern matches 'at 5'"),
    ("what is 2 at 3?", "the clock-time pattern matches 'at 3'"),
    ("my monitor is broken, what should I buy?", "the bare word 'monitor'"),
    ("I want to watch a movie tonight", "the bare word 'watch'"),
    ("listen to what I am saying", "'listen to' reads as an MQTT subscription"),
    ("log the results of the last run", "'log the' reads as continuous logging"),
]


@pytest.mark.parametrize("task", PIPELINE_REQUESTS)
def test_pipeline_requests_are_detected(task: str) -> None:
    assert is_pipeline_request(task) is True


@pytest.mark.parametrize("task", ONE_SHOT_REQUESTS)
def test_one_shot_requests_are_not_pipelines(task: str) -> None:
    assert is_pipeline_request(task) is False


@pytest.mark.parametrize(("task", "why"), MISREAD_AS_PIPELINE)
def test_one_shot_questions_currently_misread_as_pipelines(task: str, why: str) -> None:
    """Known false positives, asserted as they behave today.

    Each spawns a persistent agent to answer a question that wanted one reply.
    Fixing any of them means changing this assertion on purpose.
    """
    assert is_pipeline_request(task) is True, why


def test_the_pipeline_prefix_wins_over_everything() -> None:
    """An explicit prefix is checked before the patterns and short-circuits them."""
    assert is_pipeline_request("pipeline: do nothing") is True
    assert is_pipeline_request("PIPELINE: do nothing") is True


def test_detection_is_case_insensitive() -> None:
    assert is_pipeline_request("MONITOR THE TEMPERATURE") is True


class TestExtractJsonArray:
    """The extractor both decompose paths share, so they parse identically."""

    def test_a_bare_array_is_returned_unchanged(self) -> None:
        assert extract_json_array('[{"a": 1}]') == '[{"a": 1}]'

    def test_a_fenced_array_is_unwrapped(self) -> None:
        assert extract_json_array('```json\n[{"a": 1}]\n```') == '[{"a": 1}]'

    def test_prose_on_either_side_is_sliced_away(self) -> None:
        response = 'Sure! Here you go:\n[{"a": 1}]\nHope that helps.'
        assert json.loads(extract_json_array(response)) == [{"a": 1}]

    def test_nested_arrays_keep_their_outermost_brackets(self) -> None:
        assert extract_json_array("[[1],[2]]") == "[[1],[2]]"

    @pytest.mark.parametrize("response", ["", "```", "```json"])
    def test_empty_and_fence_only_responses_yield_nothing(self, response: str) -> None:
        assert not extract_json_array(response)

    def test_none_is_tolerated(self) -> None:
        """Callers pass an LLM response that can be absent."""
        assert not extract_json_array(None)  # pyright: ignore[reportArgumentType]

    def test_text_with_no_array_is_returned_verbatim_despite_the_docstring(self) -> None:
        """The docstring promises '' here; the code returns the prose instead.

        Harmless today only because all three call sites wrap the json.loads in
        try/except, so the decode error is caught and handled.
        """
        assert extract_json_array("no array here at all") == "no array here at all"

    def test_reversed_delimiters_are_left_alone(self) -> None:
        """The slice needs the closing bracket after the opening one."""
        assert extract_json_array("] backwards [") == "] backwards ["


class TestExtractJsonObject:
    def test_a_bare_object_is_returned_unchanged(self) -> None:
        assert extract_json_object('{"a":1}') == '{"a":1}'

    def test_prose_on_either_side_is_sliced_away(self) -> None:
        assert extract_json_object('prose {"a":1} prose') == '{"a":1}'

    def test_a_fenced_object_is_unwrapped(self) -> None:
        assert extract_json_object('```json\n{"a":1}\n```') == '{"a":1}'

    def test_text_with_no_object_is_returned_verbatim(self) -> None:
        assert extract_json_object("no object") == "no object"


class TestTaskHash:
    def test_it_is_stable_for_the_same_task(self) -> None:
        assert task_hash("do the thing") == task_hash("do the thing")

    @pytest.mark.parametrize(
        "variant", ["Do The Thing", "DO THE THING", "  do   the   thing  ", "do\tthe\nthing"]
    )
    def test_case_and_whitespace_are_normalized_away(self, variant: str) -> None:
        """Cache hits must survive retyping the same request differently."""
        assert task_hash(variant) == task_hash("do the thing")

    def test_different_tasks_hash_differently(self) -> None:
        assert task_hash("do the thing") != task_hash("do the other thing")

    def test_it_is_twelve_characters(self) -> None:
        """The width is a cache-key contract; widening it invalidates every key."""
        assert len(task_hash("anything")) == 12


class TestRewriteAiomqttToSubscribe:
    def test_code_it_cannot_parse_yields_nothing(self) -> None:
        """An empty return tells the caller to keep the original code."""
        assert not rewrite_aiomqtt_to_subscribe("print('hi')", "sensors/x")

    def test_a_recognised_subscription_loop_is_rewritten(self) -> None:
        code = (
            "async with aiomqtt.Client('localhost') as client:\n"
            "    async for msg in client.messages:\n"
            "        data = json.loads(msg.payload)\n"
            "        print(data)\n"
        )
        rewritten = rewrite_aiomqtt_to_subscribe(code, "sensors/x")

        assert rewritten
        assert "aiomqtt" not in rewritten
        assert "sensors/x" in rewritten


@pytest.fixture(name="planner")
def planner_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlannerAgent:
    """A planner with a fake provider and an isolated state dir.

    ``accumulate_global_cost`` is stubbed because it writes to process-wide cost
    state that would otherwise carry between tests.
    """
    monkeypatch.setattr("wactorz.agents.planner.agent.accumulate_global_cost", lambda _d: None)
    return PlannerAgent(
        llm_provider=FakeProvider(), task="do a thing", persistence_dir=str(tmp_path)
    )


class TestAccrueUsage:
    def test_usage_accumulates_across_calls(self, planner: PlannerAgent) -> None:
        planner._accrue_usage({"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25})
        planner._accrue_usage({"input_tokens": 3, "output_tokens": 1, "cost_usd": 0.10})

        assert planner.total_input_tokens == 13
        assert planner.total_output_tokens == 6
        assert planner.total_cost_usd == pytest.approx(0.35)

    def test_missing_keys_count_as_zero(self, planner: PlannerAgent) -> None:
        """Providers do not all report the same fields."""
        planner._accrue_usage({})

        assert planner.total_input_tokens == 0
        assert planner.total_cost_usd == 0.0

    def test_only_the_increase_is_reported_globally(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The global total must not be charged the running sum every time."""
        deltas: list[float] = []
        monkeypatch.setattr("wactorz.agents.planner.agent.accumulate_global_cost", deltas.append)

        planner._accrue_usage({"cost_usd": 0.20})
        planner._accrue_usage({"cost_usd": 0.05})

        assert deltas == pytest.approx([0.20, 0.05])

    def test_a_zero_cost_call_reports_nothing(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deltas: list[float] = []
        monkeypatch.setattr("wactorz.agents.planner.agent.accumulate_global_cost", deltas.append)

        planner._accrue_usage({"input_tokens": 5, "output_tokens": 5})

        assert not deltas


class TestPipelineRules:
    def test_a_saved_rule_is_read_back(self, planner: PlannerAgent) -> None:
        planner._save_pipeline_rule({"rule_id": "r1", "task": "watch the door"})

        assert planner._load_pipeline_rules() == [{"rule_id": "r1", "task": "watch the door"}]

    def test_no_rules_yet_reads_as_empty(self, planner: PlannerAgent) -> None:
        assert not planner._load_pipeline_rules()

    def test_saving_the_same_rule_id_replaces_rather_than_appends(
        self, planner: PlannerAgent
    ) -> None:
        planner._save_pipeline_rule({"rule_id": "r1", "task": "old"})
        planner._save_pipeline_rule({"rule_id": "r1", "task": "new"})
        rules = planner._load_pipeline_rules()

        assert len(rules) == 1
        assert rules[0]["task"] == "new"

    def test_distinct_rule_ids_accumulate(self, planner: PlannerAgent) -> None:
        planner._save_pipeline_rule({"rule_id": "r1", "task": "one"})
        planner._save_pipeline_rule({"rule_id": "r2", "task": "two"})

        assert {r["rule_id"] for r in planner._load_pipeline_rules()} == {"r1", "r2"}


def _dynamic_step(code: str, name: str = "worker") -> dict[str, Any]:
    return {"name": name, "spawn_config": {"type": "dynamic", "code": code}}


class TestValidatePipelineCode:
    def test_await_on_sync_agent_methods_is_stripped(self, planner: PlannerAgent) -> None:
        """Awaiting a sync method raises at runtime, so the validator rewrites it."""
        step = _dynamic_step("await agent.subscribe('t')\nawait agent.persist('k', 1)\n")
        planner._validate_pipeline_code([step])

        assert step["spawn_config"]["code"] == "agent.subscribe('t')\nagent.persist('k', 1)\n"

    def test_await_on_genuinely_async_calls_is_left_alone(self, planner: PlannerAgent) -> None:
        code = "await agent.publish('t', {})\n"
        step = _dynamic_step(code)
        planner._validate_pipeline_code([step])

        assert step["spawn_config"]["code"] == code

    def test_non_dynamic_steps_are_untouched(self, planner: PlannerAgent) -> None:
        step: dict[str, Any] = {
            "name": "w",
            "spawn_config": {"type": "scheduled", "code": "await agent.subscribe('t')"},
        }
        planner._validate_pipeline_code([step])

        assert step["spawn_config"]["code"] == "await agent.subscribe('t')"

    def test_a_step_with_no_code_is_skipped(self, planner: PlannerAgent) -> None:
        step: dict[str, Any] = {"name": "w", "spawn_config": {"type": "dynamic", "code": ""}}

        assert planner._validate_pipeline_code([step]) == [step]

    def test_an_empty_plan_is_returned_as_is(self, planner: PlannerAgent) -> None:
        assert not planner._validate_pipeline_code([])

    def test_raw_aiomqtt_is_rewritten_to_agent_subscribe(self, planner: PlannerAgent) -> None:
        code = (
            "async with aiomqtt.Client('localhost') as client:\n"
            "    await client.subscribe('sensors/temp')\n"
            "    async for msg in client.messages:\n"
            "        data = json.loads(msg.payload)\n"
            "        print(data)\n"
        )
        step = _dynamic_step(code)
        planner._validate_pipeline_code([step])

        assert "aiomqtt" not in step["spawn_config"]["code"]
        assert "sensors/temp" in step["spawn_config"]["code"]

    def test_unrewritable_aiomqtt_code_is_left_for_the_operator(
        self, planner: PlannerAgent
    ) -> None:
        """A detected problem the rewriter cannot fix must not be silently mangled."""
        code = "client = aiomqtt.Client('localhost')\n"
        step = _dynamic_step(code)
        planner._validate_pipeline_code([step])

        assert step["spawn_config"]["code"] == code


class TestPlanCache:
    def test_a_saved_plan_is_read_back(self, planner: PlannerAgent) -> None:
        plan = [{"agent": "main", "task": "x"}]
        planner._save_plan_cache("k1", "do a thing", plan)

        assert planner._load_cached_plan("k1", []) == plan

    def test_an_unknown_key_is_a_miss(self, planner: PlannerAgent) -> None:
        assert planner._load_cached_plan("nope", []) is None

    def test_an_entry_past_its_ttl_is_a_miss(self, planner: PlannerAgent) -> None:
        planner.persist(
            PLAN_CACHE_KEY,
            {
                "k1": {
                    "task": "t",
                    "plan": [{"agent": "main"}],
                    "timestamp": time.time() - CACHE_TTL_S - 1,
                }
            },
        )

        assert planner._load_cached_plan("k1", []) is None

    def test_a_plan_naming_a_dead_agent_is_a_miss(self, planner: PlannerAgent) -> None:
        """Reusing a plan whose worker is gone would delegate into nothing."""
        planner._save_plan_cache("k1", "t", [{"agent": "ghost"}])

        assert planner._load_cached_plan("k1", []) is None

    def test_a_step_that_spawns_its_own_agent_stays_valid(self, planner: PlannerAgent) -> None:
        """A spawn_config means the agent is created on use, so absence is fine."""
        plan = [{"agent": "ghost", "spawn_config": {"type": "dynamic", "code": "pass"}}]
        planner._save_plan_cache("k1", "t", plan)

        assert planner._load_cached_plan("k1", []) == plan

    def test_main_and_the_planner_itself_always_count_as_alive(self, planner: PlannerAgent) -> None:
        plan = [{"agent": "main"}, {"agent": planner.name}]
        planner._save_plan_cache("k1", "t", plan)

        assert planner._load_cached_plan("k1", []) == plan

    def test_a_live_worker_keeps_the_plan_valid(self, planner: PlannerAgent) -> None:
        plan = [{"agent": "weather"}]
        planner._save_plan_cache("k1", "t", plan)

        assert planner._load_cached_plan("k1", [{"name": "weather"}]) == plan

    def test_an_empty_cached_plan_is_a_miss(self, planner: PlannerAgent) -> None:
        planner._save_plan_cache("k1", "t", [])

        assert planner._load_cached_plan("k1", []) is None

    def test_saving_evicts_entries_that_are_already_stale(self, planner: PlannerAgent) -> None:
        planner.persist(
            PLAN_CACHE_KEY,
            {"old": {"task": "t", "plan": [{"agent": "main"}], "timestamp": 0.0}},
        )
        planner._save_plan_cache("new", "t", [{"agent": "main"}])
        stored = planner.recall(PLAN_CACHE_KEY)

        assert set(stored) == {"new"}

    def test_the_stored_task_string_is_truncated(self, planner: PlannerAgent) -> None:
        """Cache entries hold a label, not a whole prompt."""
        planner._save_plan_cache("k1", "x" * 500, [{"agent": "main"}])
        stored = planner.recall(PLAN_CACHE_KEY)

        assert len(stored["k1"]["task"]) == 200
