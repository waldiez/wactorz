"""Tests for ``wactorz.agents.main.planning.PlanningMixin``.

Covers the pure / cheaply-testable surface: pipeline-rule and pending-plan
persistence, the approval/rejection/correction predicates, dry-run gating, the
planning heuristic, collision warnings, and proposal formatting.

The heavy orchestration methods (``run_pipeline``, ``_run_planner``,
``_propose_or_execute_pipeline``, ``_execute_pending_plan``) spawn real agents /
the planner and are integration-level — not unit-tested here.

Run with ``pytest``. Async methods are driven through ``asyncio.run``.
"""

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from wactorz.agents.main.planning import (
    BYPASS_MARKERS,
    PlanningMixin,
    _parse_plan_envelope,
    _strip_dryrun_bypass,
    starts_with_bypass,
)


def run(coro):
    return asyncio.run(coro)


class FakeActor:
    def __init__(self, name):
        self.name = name


class FakeRegistry:
    def __init__(self, names):
        self._actors = [FakeActor(n) for n in names]

    def all_actors(self):
        return list(self._actors)


class PlanningHost(PlanningMixin):
    def __init__(self, registry=None, facts=None):
        self.name = "main"
        self._store = {}
        self._registry = registry
        self._facts = facts or {}
        self._persistence_dir = Path(tempfile.mkdtemp()) / "main"
        self._result_futures = {}

    def recall(self, key, default=None):
        return self._store.get(key, default)

    def persist(self, key, value):
        self._store[key] = value

    def get_user_facts(self):
        return self._facts


def host(**kw):
    return PlanningHost(**kw)


# ── Pipeline-rule + pending-plan persistence ─────────────────────────────────


def test_pipeline_rule_roundtrip():
    h = host()
    h.save_pipeline_rule({"rule_id": "r1", "agents": ["a", "b"]})
    rules = h.get_pipeline_rules()
    assert "r1" in rules and rules["r1"]["agents"] == ["a", "b"]


def test_pending_plan_crud():
    h = host()
    now = time.time()
    h.save_pending_plan({"plan_id": "p1", "task": "x", "status": "pending", "created_at": now})
    assert "p1" in h.get_pending_plans()
    h.update_plan_status("p1", "approved")
    assert h.get_pending_plans()["p1"]["status"] == "approved"


def test_most_recent_pending_picks_latest():
    h = host()
    now = time.time()
    h.save_pending_plan(
        {"plan_id": "old", "task": "a", "status": "pending", "created_at": now - 50}
    )
    h.save_pending_plan({"plan_id": "new", "task": "b", "status": "pending", "created_at": now})
    assert h._most_recent_pending_plan()["plan_id"] == "new"


def test_most_recent_ignores_non_pending():
    h = host()
    h.save_pending_plan(
        {"plan_id": "p", "task": "a", "status": "approved", "created_at": time.time()}
    )
    assert h._most_recent_pending_plan() is None


def test_pending_plan_expires_after_ttl():
    h = host()
    old = time.time() - (PlanningMixin.PLAN_TTL_S + 10)
    h.save_pending_plan({"plan_id": "stale", "task": "a", "status": "pending", "created_at": old})
    # Reading expires it in place.
    assert h.get_pending_plans()["stale"]["status"] == "expired"
    assert h._most_recent_pending_plan() is None


# ── Approval / rejection / correction predicates ─────────────────────────────


@pytest.mark.parametrize("text", ["yes", "ok", "approve", "do it", "go ahead"])
def test_looks_like_approval_true(text):
    assert PlanningHost._looks_like_approval(text) is True


@pytest.mark.parametrize("text", ["no", "cancel", "something totally unrelated here"])
def test_looks_like_approval_false(text):
    assert PlanningHost._looks_like_approval(text) is False


@pytest.mark.parametrize("text", ["no", "cancel", "nope", "abort"])
def test_looks_like_rejection_true(text):
    assert PlanningHost._looks_like_rejection(text) is True


def test_looks_like_rejection_false():
    assert PlanningHost._looks_like_rejection("yes") is False


def test_looks_like_correction_true():
    assert PlanningHost._looks_like_correction("actually use celsius instead") is True


def test_looks_like_correction_false():
    assert PlanningHost._looks_like_correction("yes") is False


# ── Dry-run gating ───────────────────────────────────────────────────────────


def test_dryrun_bypass_marker():
    assert host()._dryrun_enabled("pipeline! build the thing") is False


def test_dryrun_default_on():
    assert host()._dryrun_enabled("build the thing") is True


def test_dryrun_off_by_policy():
    assert host(facts={"policy_dryrun": "off"})._dryrun_enabled("build the thing") is False


# ── Planning heuristic (exercises _PLANNING_KEYWORDS) ────────────────────────


def test_needs_planning_explicit():
    assert run(host()._needs_planning("coordinate: weather then news")) is True


def test_needs_planning_at_planner():
    assert run(host()._needs_planning("@planner please help")) is True


def test_needs_planning_keyword_threshold():
    # "coordinate" + "and then" + "summarize both" → 3 keyword hits
    assert run(host()._needs_planning("coordinate and then summarize both")) is True


def test_needs_planning_plain_text():
    assert run(host()._needs_planning("what's the weather today")) is False


def test_needs_planning_two_agent_mentions():
    h = host(registry=FakeRegistry(["weather-bot", "news-bot"]))
    assert run(h._needs_planning("use weather-bot and news-bot together")) is True


# ── Collision warning (exercises _SPAWN_INTENT_WORDS) ────────────────────────


def test_collision_none_when_no_pending():
    assert host()._warn_if_pending_plan_collision("create a sensor") is None


def test_collision_warns_on_spawn_intent():
    h = host()
    h.save_pending_plan(
        {"plan_id": "p1", "task": "old task", "status": "pending", "created_at": time.time()}
    )
    msg = h._warn_if_pending_plan_collision("create a new cpu sensor")
    assert msg is not None and "pending plan" in msg.lower()


def test_collision_bypassed_by_marker():
    h = host()
    h.save_pending_plan(
        {"plan_id": "p1", "task": "old", "status": "pending", "created_at": time.time()}
    )
    assert h._warn_if_pending_plan_collision("pipeline! create a sensor") is None


# ── Proposal formatting ──────────────────────────────────────────────────────


def test_format_plan_proposal():
    plan = {
        "plan_id": "p1",
        "task": "monitor cpu and alert",
        "envelope": {
            "plan": [
                {
                    "name": "cpu-mon",
                    "description": "watch cpu",
                    "spawn_config": {"type": "dynamic"},
                },
            ]
        },
    }
    out = host()._format_plan_proposal(plan)
    assert "p1" in out
    assert "monitor cpu and alert" in out
    assert "cpu-mon" in out
    assert "1 agent" in out


class TestParsePlanEnvelope:
    """Telling a planner's dry-run proposal from an ordinary prose answer."""

    def test_a_proposal_comes_back_whole(self) -> None:
        envelope = _parse_plan_envelope('{"_plan_proposal": true, "steps": [1, 2]}')
        assert envelope == {"_plan_proposal": True, "steps": [1, 2]}

    def test_leading_whitespace_does_not_hide_a_proposal(self) -> None:
        assert _parse_plan_envelope('  \n {"_plan_proposal": true}') is not None

    @pytest.mark.parametrize(
        "result",
        [
            "",
            "Sorry, I can't do that.",
            '{"steps": []}',
            "[1, 2, 3]",
            '{"broken": ',
        ],
        ids=["empty", "prose", "no-marker", "json-array", "malformed"],
    )
    def test_everything_else_is_an_ordinary_answer(self, result: str) -> None:
        assert _parse_plan_envelope(result) is None

    def test_the_marker_must_be_the_boolean_not_a_lookalike(self) -> None:
        """`is True` is deliberate — a string "true" is not a proposal.

        A model that quotes the value produces prose, not an unapproved plan
        running, so the strict check is the safe direction.
        """
        assert _parse_plan_envelope('{"_plan_proposal": "true"}') is None
        assert _parse_plan_envelope('{"_plan_proposal": 1}') is None


class TestBypassMarkers:
    """The dry-run bypass, and stripping it off the task text."""

    def test_the_marker_set_is_the_one_three_call_sites_share(self) -> None:
        """Pinned as a set so a marker cannot be added to a call site instead.

        The three call sites — the dry-run gate, the task-text stripper, and the
        restricted channel's refusal — must agree on the whole family. They once
        did not: the guard knew one spelling and the planner knew three, so two
        markers walked past a check written to catch exactly them.
        """
        assert set(BYPASS_MARKERS) == {"pipeline!", "coordinate!", "@planner!"}

    @pytest.mark.parametrize("marker", BYPASS_MARKERS)
    def test_every_marker_is_recognised_whatever_its_case_or_spacing(self, marker: str) -> None:
        assert starts_with_bypass(marker)
        assert starts_with_bypass(f"   {marker.upper()} do the thing")

    def test_ordinary_text_is_not_a_bypass(self) -> None:
        assert not starts_with_bypass("please build a pipeline!")
        assert not starts_with_bypass("")

    @pytest.mark.parametrize("marker", BYPASS_MARKERS)
    def test_stripping_removes_the_marker_and_its_punctuation(self, marker: str) -> None:
        assert _strip_dryrun_bypass(f"{marker} : make coffee") == "make coffee"
        assert _strip_dryrun_bypass(f"  {marker.title()}, make coffee") == "make coffee"

    def test_text_without_a_marker_survives_unchanged(self) -> None:
        assert _strip_dryrun_bypass("make coffee") == "make coffee"

    def test_empty_text_is_returned_rather_than_indexed(self) -> None:
        assert not _strip_dryrun_bypass("")

    def test_a_later_occurrence_is_left_in_place(self) -> None:
        """Only the opening marker is a bypass; a repeat is part of the task."""
        assert _strip_dryrun_bypass("pipeline! build a pipeline! thing") == (
            "build a pipeline! thing"
        )
