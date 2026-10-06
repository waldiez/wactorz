"""Tests for the call-site evaluation harness (wactorz/evalharness.py)."""

import json

from wactorz.evalharness import (
    _SEED_CASES,
    CATEGORIES,
    extract_json,
    load_cases,
    score_actuator,
    score_dynamic,
    score_ha,
    score_intent,
    score_planner,
    summarize,
)

# ── extract_json ─────────────────────────────────────────────────────────────


def test_extract_json_plain():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_fenced():
    assert extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]


def test_extract_json_with_prose():
    assert extract_json('Here is the plan:\n[{"a": 1}]\nDone.') == [{"a": 1}]


def test_extract_json_garbage():
    assert extract_json("no json here") is None


# ── intent / ha ──────────────────────────────────────────────────────────────


def test_score_intent_exact_and_noise():
    assert score_intent("PIPELINE", "PIPELINE")
    assert score_intent("pipeline\n", "PIPELINE")
    assert score_intent("ACTUATE.", "ACTUATE")
    assert not score_intent("I think PIPELINE", "PIPELINE")
    assert not score_intent("", "OTHER")


def test_score_ha():
    assert score_ha("create_automation", "create_automation")
    assert score_ha("Create_Automation\n", "create_automation")
    assert not score_ha("automation", "create_automation")


# ── actuator ─────────────────────────────────────────────────────────────────

_ACTION = {"domain": "light", "service": "turn_on", "entity_id": "light.lamp"}


def test_score_actuator_subset_match():
    out = json.dumps([{**_ACTION, "service_data": {"brightness_pct": 50}}])
    assert score_actuator(out, [_ACTION])


def test_score_actuator_multiple_actions_any_order():
    lock = {"domain": "lock", "service": "lock", "entity_id": "lock.door"}
    out = json.dumps([lock, _ACTION])
    assert score_actuator(out, [_ACTION, lock])


def test_score_actuator_wrong_entity_fails():
    out = json.dumps([{**_ACTION, "entity_id": "light.other"}])
    assert not score_actuator(out, [_ACTION])


def test_score_actuator_not_a_list_fails():
    assert not score_actuator(json.dumps(_ACTION), [_ACTION])
    assert not score_actuator("nonsense", [_ACTION])


def test_score_actuator_extra_action_fails():
    # The "wiz light" pattern: correct action + an unrequested bonus action.
    extra = {"domain": "light", "service": "turn_on", "entity_id": "light.wiz_rgbw"}
    out = json.dumps([_ACTION, extra])
    assert not score_actuator(out, [_ACTION])


def test_score_actuator_expected_empty_requires_refusal():
    assert score_actuator("[]", [])
    assert not score_actuator(json.dumps([_ACTION]), [])


# ── planner ──────────────────────────────────────────────────────────────────


def test_score_planner_valid_plan():
    plan = [
        {"name": "door-watch", "type": "dynamic", "description": "watches the door"},
        {"name": "hall-light", "type": "ha_actuator", "description": "turns on hall light"},
    ]
    assert score_planner(json.dumps(plan), ["dynamic", "ha_actuator"])


def test_score_planner_disallowed_type_fails():
    plan = [{"name": "x", "type": "magic", "description": "y"}]
    assert not score_planner(json.dumps(plan), ["dynamic"])


def test_score_planner_missing_fields_or_empty_fails():
    assert not score_planner(json.dumps([{"type": "dynamic"}]), ["dynamic"])
    assert not score_planner("[]", ["dynamic"])


# ── dynamic (codegen) ────────────────────────────────────────────────────────


def test_score_dynamic_valid_code():
    code = "async def setup(agent):\n    pass\n\nasync def process(agent):\n    pass\n"
    assert score_dynamic(code, ["setup", "process"])


def test_score_dynamic_fenced_code():
    code = "```python\nasync def handle_task(agent, payload):\n    return {}\n```"
    assert score_dynamic(code, ["handle_task"])


def test_score_dynamic_sync_def_fails():
    assert not score_dynamic("def setup(agent):\n    pass\n", ["setup"])


def test_score_dynamic_syntax_error_fails():
    assert not score_dynamic("async def setup(agent:\n    pass", ["setup"])


# ── case loading / summary ───────────────────────────────────────────────────


def test_seed_cases_cover_all_categories():
    assert {c["category"] for c in _SEED_CASES} == set(CATEGORIES)


def test_load_cases_filters_categories():
    cases = load_cases(None, ["intent"])
    assert cases and all(c["category"] == "intent" for c in cases)


def test_load_cases_from_file_skips_malformed(tmp_path):
    path = tmp_path / "bench.jsonl"
    good = {"id": "x", "category": "intent", "prompt": "p", "expected": "OTHER"}
    path.write_text(json.dumps(good) + "\nnot json\n" + json.dumps({"id": "y"}) + "\n")
    cases = load_cases(str(path), list(CATEGORIES))
    assert cases == [good]


def test_summarize_accuracy_latency_cost():
    records = [
        {
            "model": "m",
            "category": "intent",
            "ok": True,
            "passed": True,
            "latency_s": 1.0,
            "cost_usd": 0.01,
        },
        {
            "model": "m",
            "category": "intent",
            "ok": True,
            "passed": False,
            "latency_s": 3.0,
            "cost_usd": 0.01,
        },
        {
            "model": "m",
            "category": "intent",
            "ok": False,
            "passed": False,
            "latency_s": 0.5,
            "cost_usd": 0.0,
        },
    ]
    (row,) = summarize(records)
    assert row["n"] == 3
    assert row["errors"] == 1
    assert row["accuracy"] == round(1 / 3, 4)
    assert row["mean_latency_s"] == 2.0
    assert row["total_cost_usd"] == 0.02


# ── Profiles ─────────────────────────────────────────────────────────────────


def test_system_prompts_follow_the_profile():
    from wactorz.evalharness import _system_prompts

    with_ha = _system_prompts("ha")
    without = _system_prompts("minimal")

    assert "ACTUATE, HA, PIPELINE, or OTHER" in with_ha["intent"]
    assert "PIPELINE or OTHER" in without["intent"]
    assert "Home Assistant" not in without["intent"]
    assert 'TYPE 1 — "ha_actuator"' in with_ha["planner"]
    assert "ha_actuator" not in without["planner"]
    assert "═══ OUTPUT FORMAT ═══" in with_ha["planner"]
    # The Home Assistant call sites are the same text on both; the runner skips them.
    assert with_ha["ha"] == without["ha"]
    assert with_ha["actuator"] == without["actuator"]


def test_the_default_profile_is_the_home_assistant_one():
    from wactorz.evalharness import _system_prompts

    assert _system_prompts() == _system_prompts("ha")


def test_an_unknown_profile_is_refused():
    import pytest

    from wactorz.evalharness import _system_prompts

    with pytest.raises(ValueError, match="unknown profile"):
        _system_prompts("desktop")


def test_a_case_with_a_minimal_expectation_uses_it_on_that_profile():
    by_id = {c["id"]: c for c in load_cases(None, ["intent"], "minimal")}
    assert by_id["intent-001"]["expected"] == "OTHER"
    assert by_id["intent-003"]["expected"] == "OTHER"
    # The pipeline case needs no override: a rule is a pipeline on either profile.
    assert by_id["intent-002"]["expected"] == "PIPELINE"

    with_ha = {c["id"]: c for c in load_cases(None, ["intent"])}
    assert with_ha["intent-001"]["expected"] == "ACTUATE"
    assert with_ha["intent-003"]["expected"] == "HA"


def test_a_case_without_a_minimal_expectation_is_returned_as_it_is(tmp_path):
    path = tmp_path / "bench.jsonl"
    case = {"id": "x", "category": "intent", "prompt": "p", "expected": "OTHER"}
    path.write_text(json.dumps(case) + "\n")
    assert load_cases(str(path), ["intent"], "minimal") == [case]


def test_every_seed_planner_case_can_pass_without_home_assistant():
    """A minimal-profile plan has no ha_actuator; the allowed types must say so."""
    for case in load_cases(None, ["planner"], "minimal"):
        assert "ha_actuator" not in case["expected"], case["id"]
        assert set(case["expected"]) <= {"dynamic", "scheduled"}, case["id"]


def test_score_planner_reads_the_type_from_the_spawn_config_too():
    """The production output format nests the type in spawn_config."""
    plan = json.dumps(
        [
            {
                "name": "door-watch",
                "description": "watches the door",
                "spawn_config": {"type": "dynamic", "code": "pass"},
            }
        ]
    )
    assert score_planner(plan, ["dynamic"])
    assert not score_planner(plan, ["scheduled"])
