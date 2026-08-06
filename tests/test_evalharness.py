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


def test_score_planner_grounded_requires_entity_reference():
    spec = {
        "types": ["ha_actuator"],
        "must_contain": ["light.philips_hue_lct015"],
        "must_not_contain": ["light.wiz_rgbw_tunable_351b6e"],
    }
    good = [
        {
            "name": "door-light",
            "type": "ha_actuator",
            "description": "office light on door open",
            "actions": [
                {"domain": "light", "service": "turn_on", "entity_id": "light.philips_hue_lct015"}
            ],
        }
    ]
    assert score_planner(json.dumps(good), spec)

    # right shape, wrong device — must fail
    wrong = json.loads(json.dumps(good).replace("philips_hue_lct015", "wiz_rgbw_tunable_351b6e"))
    assert not score_planner(json.dumps(wrong), spec)

    # right shape, no entity resolved at all — must fail
    vague = [{"name": "door-light", "type": "ha_actuator", "description": "turn on the light"}]
    assert not score_planner(json.dumps(vague), spec)


def test_score_planner_list_form_still_shape_only():
    plan = [{"name": "x", "type": "dynamic", "description": "y"}]
    assert score_planner(json.dumps(plan), ["dynamic"])


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


# ── dynamic: strict (dict) form ──────────────────────────────────────────────

_STRICT = {
    "functions": ["setup", "process"],
    "must_contain": ["custom/home/heartbeat", "agent.subscribe"],
}

_GOOD = (
    "async def setup(agent):\n"
    "    async def on_beat(payload):\n"
    "        agent.state['last'] = payload\n"
    "    agent.subscribe('custom/home/heartbeat', on_beat)\n"
    "\n"
    "async def process(agent):\n"
    "    import time\n"
    "    if time.time() - agent.state.get('last', 0) > 60:\n"
    "        await agent.alert('silent')\n"
)


def test_score_dynamic_strict_accepts_real_agent():
    assert score_dynamic(_GOOD, _STRICT)


def test_score_dynamic_strict_rejects_stub():
    stub = "async def setup(agent):\n    pass\n\nasync def process(agent):\n    pass\n"
    assert score_dynamic(stub, ["setup", "process"])  # lenient list form passes
    assert not score_dynamic(stub, _STRICT)  # strict form does not


def test_score_dynamic_strict_allows_unrequired_stubs():
    # A model that emits the whole lifecycle with `pass` in unused slots is fine
    # as long as the REQUIRED entry points do real work.
    with_stubs = _GOOD + "\nasync def handle_task(agent, payload):\n    pass\n"
    assert score_dynamic(with_stubs, _STRICT)


def test_score_dynamic_strict_rejects_missing_topic():
    wrong = _GOOD.replace("custom/home/heartbeat", "custom/home/pulse")
    assert not score_dynamic(wrong, _STRICT)


def test_score_dynamic_strict_rejects_awaited_subscribe():
    bad = _GOOD.replace("    agent.subscribe(", "    await agent.subscribe(")
    assert not score_dynamic(bad, _STRICT)


def test_score_dynamic_strict_rejects_module_level_import():
    bad = "import time\n" + _GOOD
    assert not score_dynamic(bad, _STRICT)


def test_score_dynamic_strict_rejects_own_llm_client():
    bad = _GOOD.replace("    import time\n", "    import openai\n    import time\n")
    assert not score_dynamic(bad, _STRICT)


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
