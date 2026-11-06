# SPDX-License-Identifier: Apache-2.0
"""A model's name carries a policy saying when its shadow becomes active, and main holds each trial to it.

The policy lives beside the pointers in the store's index. Main reads every
trial under way against its policy on a cadence; when it is met, main writes
a decision and promotes on its own or asks on the dashboard. The routes, the
CLI and chat set and clear it. ``docs/design/promotion-policy.md`` is the
decision these pin.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz import artifacts_cli
from wactorz.agents.main import shadow_serving as main_side
from wactorz.agents.main.commands import models as models_commands
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.agents.main.hosts import SwapHost
from wactorz.agents.main.shadow_serving import CLEAR, KEEP, OPERATOR, PROMOTE, ShadowServing
from wactorz.core.actor import ActorState
from wactorz.core.artifacts import ArtifactNotFoundError, ArtifactStore
from wactorz.core.model_swaps import move_id
from wactorz.core.paths import resolve_state_dir
from wactorz.core.persistence import WactorzDB
from wactorz.core.promotion import (
    METRICS_OF_KIND,
    POLICY,
    Policy,
    PolicyError,
    decision_id,
    decisions_of,
    declared,
    from_json,
    judge_trial,
    record_decision,
)
from wactorz.core.provenance import DECISION, LED_TO, Provenance
from wactorz.monitoring import model_metrics
from wactorz.web import api_models, runtime
from wactorz.web.app import build_app

if TYPE_CHECKING:
    from wactorz.agents.main.actor import MainActor

T0 = 1_760_000_000.0
DAY = 86400.0


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> ArtifactStore:
    """``imu`` in two versions of raw bytes: v1 active, v2 on trial as shadow since ``T0``."""
    store = ArtifactStore.in_state_dir(resolve_state_dir())
    for version in (1, 2):
        weights = tmp_path / f"imu-{version}.bin"
        weights.write_bytes(f"imu weights {version}".encode())
        store.add(weights, "imu", format_name="raw")
    store.move("active", "imu:1", by="test")
    store.move("shadow", "imu:2", by="test", reason="trying it")
    return store


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "db" / "wactorz.db") as database:
        monkeypatch.setattr(main_side, "get_db", lambda: database)
        monkeypatch.setattr(api_models, "get_db", lambda: database)
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


def judged(
    *, shadow_f1: float = 0.9, active_f1: float = 0.8, labelled: int = 200, p: float = 0.01
) -> dict[str, Any]:
    """A trial compared and judged, as `shadow_trial.compare` gives it with labels."""
    better = "shadow" if p < 0.05 and shadow_f1 > active_f1 else None
    return {
        "pairs": labelled + 20,
        "agreement": 0.9,
        "labels": {
            "kind": "detector",
            "labelled": labelled,
            "active": {"f1": active_f1, "precision": 0.8, "false_positive_rate": 0.1},
            "shadow": {"f1": shadow_f1, "precision": 0.9, "false_positive_rate": 0.05},
            "mcnemar": {"active_only": 1, "shadow_only": 9, "p": p, "better": better},
        },
    }


# ── The policy ────────────────────────────────────────────────────────────────


class TestThePolicy:
    def test_the_defaults_ask_the_operator_after_a_day_and_a_hundred_labels(self) -> None:
        policy = declared(by="cli", at=T0)

        assert policy == Policy(None, 0.0, 1.0, 100, True, False, by="cli", at=T0)
        assert policy.words() == (
            "the main figure of its kind by 0 over 1 day and 100 labelled, significant, asked"
        )

    def test_fields_given_sit_over_the_policy_set_before(self) -> None:
        before = declared({"margin": 0.05, "min_days": 3}, by="cli", at=T0)

        later = declared({"automatic": "yes", "metric": "mcc"}, over=before, by="chat", at=T0 + 1)

        assert later.terms() == {
            "metric": "mcc",
            "margin": 0.05,
            "min_days": 3.0,
            "min_labelled": 100,
            "significant": True,
            "automatic": True,
        }
        assert (later.by, later.at) == ("chat", T0 + 1)
        assert later.words() == (
            "mcc by 0.05 over 3 days and 100 labelled, significant, promoted on its own"
        )

    @pytest.mark.parametrize(
        ("given", "says"),
        [
            ({"metric": "auc"}, "not 'auc'"),
            ({"margin": "lots"}, "margin is a number"),
            ({"min_days": -1}, "cannot be below zero"),
            ({"min_labelled": 2.5}, "whole number"),
            ({"automatic": "maybe"}, "yes or no"),
            ({"cadence": 5}, "no 'cadence'"),
            ([1], "a mapping of its fields"),
        ],
    )
    def test_what_cannot_be_a_policy_is_refused_saying_why(self, given: Any, says: str) -> None:
        with pytest.raises(PolicyError, match=says):
            declared(given)

    def test_it_reads_back_what_it_wrote_and_knows_the_same_terms(self) -> None:
        policy = declared({"margin": 0.02, "significant": False}, by="cli", at=T0)

        again = from_json(policy.to_json())

        assert again == policy
        assert policy.same_terms(declared({"margin": 0.02, "significant": False}, by="x"))
        assert not policy.same_terms(declared({"margin": 0.03}))
        assert not policy.same_terms(None)


class TestKeptWithTheName:
    def test_set_read_and_cleared(self, store: ArtifactStore) -> None:
        policy = declared({"margin": 0.02}, by="cli", at=T0)

        assert store.set_policy("imu", policy) == policy
        assert store.policy("imu") == policy
        assert json.loads(store.index_path.read_text())["policies"] == {"imu": policy.to_json()}
        assert store.clear_policy("imu") == policy
        assert store.policy("imu") is None
        assert "policies" not in json.loads(store.index_path.read_text())
        assert store.clear_policy("imu") is None

    def test_the_same_terms_again_keep_who_set_them_first(self, store: ArtifactStore) -> None:
        first = store.set_policy("imu", declared({"margin": 0.02}, by="cli", at=T0))

        kept = store.set_policy("imu", declared({"margin": 0.02}, by="chat", at=T0 + 5))

        assert kept == first and store.policy("imu") == first

    def test_a_name_with_no_version_is_refused(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.set_policy("nothing", declared())

    def test_it_outlives_the_versions_changing_and_goes_with_the_name(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        store.set_policy("imu", declared({"automatic": True}, by="cli"))
        weights = tmp_path / "imu-3.bin"
        weights.write_bytes(b"imu weights 3")
        store.add(weights, "imu", format_name="raw")
        store.promote("imu", by="cli")

        assert store.policy("imu") is not None
        store.remove("imu", every_version=True)
        assert store.policy("imu") is None
        assert "policies" not in json.loads(store.index_path.read_text())

    def test_an_index_without_the_key_reads_as_no_policy(self, store: ArtifactStore) -> None:
        assert store.policy("imu") is None
        assert store.policy("never") is None


# ── A trial held against it ───────────────────────────────────────────────────


class TestTheVerdict:
    def test_a_trial_meeting_every_term_is_met_and_says_the_figures(self) -> None:
        policy = declared({"margin": 0.05})

        verdict = judge_trial(judged(), policy, since=T0, now=T0 + 2 * DAY)

        assert verdict.met is True
        assert (verdict.metric, verdict.shadow, verdict.active, verdict.gain) == (
            "f1",
            0.9,
            0.8,
            pytest.approx(0.1),
        )
        assert [t.name for t in verdict.terms] == [
            "judged",
            "margin",
            "min_days",
            "min_labelled",
            "significant",
        ]
        assert all(t.met for t in verdict.terms)
        assert (
            verdict.words()
            == "met: f1 0.9 vs 0.8 (+0.1, margin 0.05), 2 day(s), 200 labelled, McNemar p=0.01"
        )

    @pytest.mark.parametrize(
        ("summary", "policy", "now", "short"),
        [
            (judged(shadow_f1=0.82), {"margin": 0.05}, T0 + 2 * DAY, "margin"),
            (judged(), {}, T0 + DAY / 2, "min_days"),
            (judged(labelled=50), {}, T0 + 2 * DAY, "min_labelled"),
            (judged(p=0.3), {}, T0 + 2 * DAY, "significant"),
            ({"pairs": 5, "agreement": 1.0}, {}, T0 + 2 * DAY, "judged"),
        ],
    )
    def test_one_term_short_is_not_met_and_named(
        self, summary: dict[str, Any], policy: dict[str, Any], now: float, short: str
    ) -> None:
        verdict = judge_trial(summary, declared(policy), since=T0, now=now)

        assert verdict.met is False
        assert [t.name for t in verdict.terms if not t.met] == (
            [short] if short != "judged" else ["judged", "margin", "min_labelled", "significant"]
        )
        assert verdict.words().startswith("not met: ")

    def test_significance_may_be_waived_and_a_lower_figure_may_be_better(self) -> None:
        waived = declared({"significant": False, "metric": "false_positive_rate", "margin": 0.04})

        verdict = judge_trial(judged(p=0.4), waived, since=T0, now=T0 + 2 * DAY)

        assert verdict.met is True
        assert verdict.gain == pytest.approx(0.05)
        assert verdict.to_dict()["terms"][4] == {
            "name": "significant",
            "wanted": False,
            "actual": 0.4,
            "met": True,
        }


class TestEachKindsFigures:
    """A policy compares on the figures the trial's kind reports, its main one unless it names one."""

    @staticmethod
    def classified(shadow: float = 0.9, active: float = 0.7, p: float = 0.01) -> dict[str, Any]:
        return {
            "labels": {
                "kind": "classifier",
                "labelled": 200,
                "active": {"macro_f1": active, "weighted_f1": active, "accuracy": active},
                "shadow": {"macro_f1": shadow, "weighted_f1": shadow, "accuracy": shadow},
                "mcnemar": {"p": p, "better": "shadow" if p < 0.05 else None},
            }
        }

    @staticmethod
    def regressed(shadow: float = 4.0, active: float = 9.0, p: float = 0.001) -> dict[str, Any]:
        return {
            "labels": {
                "kind": "regressor",
                "labelled": 200,
                "active": {"mae": active, "rmse": active * 1.2},
                "shadow": {"mae": shadow, "rmse": shadow * 1.2},
                "closer": {"active": 3, "shadow": 40, "ties": 0, "p": p,
                           "better": "shadow" if p < 0.05 else None},
            }
        }  # fmt: skip

    def test_a_classifier_is_compared_on_its_macro_f1_unless_another_is_named(self) -> None:
        verdict = judge_trial(
            self.classified(), declared({"margin": 0.1}), since=T0, now=T0 + 2 * DAY
        )

        assert (verdict.met, verdict.metric, verdict.kind) == (True, "macro_f1", "classifier")
        assert verdict.gain == pytest.approx(0.2)
        accuracy = judge_trial(
            self.classified(), declared({"metric": "accuracy"}), since=T0, now=T0 + 2 * DAY
        )
        assert (accuracy.met, accuracy.metric) == (True, "accuracy")

    def test_a_regressor_is_better_with_less_error_and_its_sign_test_says_if_by_chance(
        self,
    ) -> None:
        verdict = judge_trial(self.regressed(), declared({"margin": 2}), since=T0, now=T0 + 2 * DAY)

        assert (verdict.met, verdict.metric, verdict.gain, verdict.test) == (
            True,
            "mae",
            5.0,
            "sign",
        )
        assert verdict.words() == (
            "met: mae 4 vs 9 (+5, margin 2), 2 day(s), 200 labelled, sign test p=0.001"
        )
        chance = judge_trial(self.regressed(p=0.4), declared(), since=T0, now=T0 + 2 * DAY)
        assert [t.name for t in chance.terms if not t.met] == ["significant"]

    def test_a_worse_regressor_falls_short_on_the_margin(self) -> None:
        verdict = judge_trial(
            self.regressed(shadow=10.0), declared({"metric": "rmse"}), since=T0, now=T0 + 2 * DAY
        )

        assert verdict.met is False
        assert verdict.gain == pytest.approx(-1.2)

    @pytest.mark.parametrize(("metric", "kind"), [("mae", "classifier"), ("f1", "regressor")])
    def test_a_figure_the_kind_does_not_report_is_never_met_and_says_why(
        self, metric: str, kind: str
    ) -> None:
        summary = self.classified() if kind == "classifier" else self.regressed()
        # Even should the sides hold a value under that name, it is not the kind's figure.
        summary["labels"]["active"][metric] = 0.1
        summary["labels"]["shadow"][metric] = 0.9

        verdict = judge_trial(summary, declared({"metric": metric}), since=T0, now=T0 + 2 * DAY)

        assert verdict.met is False and verdict.reported is False
        assert f"{metric} is not a figure a {kind} reports" in verdict.words()
        shown = verdict.to_dict()
        assert (shown["reported"], shown["kind"]) == (False, kind)
        assert shown["metrics"] == list(METRICS_OF_KIND[kind])

    def test_a_detector_is_compared_on_f1_when_the_policy_names_none(self) -> None:
        verdict = judge_trial(judged(), declared(), since=T0, now=T0 + 2 * DAY)

        assert (verdict.metric, verdict.kind, verdict.test) == ("f1", "detector", "mcnemar")

    def test_a_metric_of_any_kind_may_be_set(self) -> None:
        assert declared({"metric": "mae"}).metric == "mae"
        assert declared({"metric": "macro_f1"}).metric == "macro_f1"


class TestTheDecision:
    def test_a_decision_is_about_the_trial_and_its_verdict_and_written_once(
        self, graph: Provenance
    ) -> None:
        verdict = judge_trial(judged(), declared(), since=T0, now=T0 + 2 * DAY)

        node = record_decision(
            graph,
            name="imu",
            shadow_version=2,
            active_version=1,
            since=T0,
            verdict=verdict,
            at=T0 + 2 * DAY,
        )
        again = record_decision(
            graph,
            name="imu",
            shadow_version=2,
            active_version=1,
            since=T0,
            verdict=verdict,
            at=T0 + 3 * DAY,
        )

        assert node.id == again.id == decision_id("imu", 2, T0, True, verdict.policy.at)
        assert (node.kind, node.actor, node.source, node.ts, node.ts_end) == (
            DECISION,
            POLICY,
            "models/imu",
            T0,
            T0 + 2 * DAY,
        )
        (decision,) = decisions_of(graph, "imu")
        assert (decision["met"], decision["shadow_version"], decision["words"]) == (
            True,
            2,
            verdict.words(),
        )


# ── Main holds each trial to it ───────────────────────────────────────────────


class _Registry:
    def all_actors(self) -> list[Any]:
        return []

    def find_by_name(self, name: str) -> Any:
        return None


class _Host:
    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._registry = _Registry()
        self.published: list[tuple[str, Any]] = []

    def _get_spawn_registry(self) -> Any:
        return None

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        pass

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        pass

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))

    def feed(self) -> list[str]:
        return [p["message"] for t, p in self.published if t == "agents/main-id/logs"]


class _Operator:
    """Stands in for `Actor.ask_operator`: answers what it is told to, and keeps what it was asked."""

    def __init__(self, answer: str = KEEP) -> None:
        self.answer = answer
        self.asked: list[tuple[str, list[str], str]] = []

    async def __call__(self, text: str, *, options: list[str], context: str = "", **_: Any) -> str:
        self.asked.append((text, list(options), context))
        return self.answer


@pytest.fixture(name="host")
def host_fixture() -> _Host:
    return _Host()


@pytest.fixture(name="operator")
def operator_fixture() -> _Operator:
    return _Operator()


@pytest.fixture(name="trials")
def trials_fixture(host: _Host, operator: _Operator) -> ShadowServing:
    # The fake declares what the service reads; it stands in for MainActor.
    return ShadowServing(cast("SwapHost", host), ask=operator)


@pytest.fixture(name="compared")
def compared_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What main's comparison of the trial finds; a test changes it in place."""
    found = judged()

    async def compare(self: Any, name: str, since: float, until: float, **_: Any) -> dict[str, Any]:
        return dict(found)

    monkeypatch.setattr(ShadowServing, "_compare", compare)
    return found


async def _settled(trials: ShadowServing) -> None:
    for task in list(trials._questions):
        await task


def days_in(store: ArtifactStore, days: float) -> float:
    """``days`` after the shadow was set: the ``now`` a check runs at."""
    shadow = store.pointer("imu", "shadow")
    assert shadow is not None
    return shadow.moved_at + days * DAY


class TestMainHoldsATrialToItsPolicy:
    async def test_an_automatic_policy_met_promotes_as_the_policy_and_records_the_decision(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        graph: Provenance,
        compared: dict[str, Any],
        operator: _Operator,
    ) -> None:
        store.set_policy("imu", declared({"margin": 0.05, "automatic": True}, by="cli"))

        verdicts = await trials.check_policies(now=days_in(store, 2))

        assert verdicts["imu"].met is True
        active = store.pointer("imu", "active")
        assert active is not None and (active.version, active.by) == (2, POLICY)
        assert active.reason == verdicts["imu"].words()
        assert store.pointer("imu", "shadow") is None
        assert operator.asked == []
        assert host.feed() == [
            f"imu:2's trial meets its policy: {verdicts['imu'].words()}",
            f"imu:2 is now active, by policy: {verdicts['imu'].words()}",
        ]
        (decision,) = decisions_of(graph, "imu")
        assert decision["met"] is True and decision["active_version"] == 1
        move = graph.get(move_id("imu", "active", 2, active.moved_at))
        assert move is not None
        assert (decision["id"], move.id, LED_TO) in {
            (e.src, e.dst, e.relation) for e in graph.edges(move.id)
        }

    async def test_a_policy_that_asks_puts_the_promotion_to_the_operator_once(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        graph: Provenance,
        compared: dict[str, Any],
        operator: _Operator,
    ) -> None:
        store.set_policy("imu", declared({"margin": 0.05}, by="cli"))
        operator.answer = PROMOTE

        now = days_in(store, 2)
        await trials.check_policies(now=now)
        await _settled(trials)
        await trials.check_policies(now=now + DAY)
        await _settled(trials)

        ((text, options, context),) = operator.asked
        assert text.startswith("Promote imu:2? Its trial meets the policy: met: f1 0.9 vs 0.8")
        assert options == [PROMOTE, CLEAR, KEEP]
        assert (
            context
            == "The policy asks for the main figure of its kind by 0.05 over 1 day and 100 labelled, "
            "significant, asked."
        )
        active = store.pointer("imu", "active")
        assert active is not None and (active.version, active.by) == (2, OPERATOR)
        assert active.reason is not None and active.reason.startswith("asked by the policy: met:")
        assert graph.get(move_id("imu", "active", 2, active.moved_at)) is not None

    @pytest.mark.parametrize(("answer", "shadow_left"), [(CLEAR, False), (KEEP, True)])
    async def test_the_operator_may_clear_the_shadow_or_keep_trying(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        compared: dict[str, Any],
        operator: _Operator,
        graph: Provenance,
        answer: str,
        shadow_left: bool,
    ) -> None:
        store.set_policy("imu", declared(by="cli"))
        operator.answer = answer

        await trials.check_policies(now=days_in(store, 2))
        await _settled(trials)

        assert (store.pointer("imu", "shadow") is not None) is shadow_left
        active = store.pointer("imu", "active")
        assert active is not None and active.version == 1

    async def test_an_answer_after_the_trial_ended_changes_nothing(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        compared: dict[str, Any],
        operator: _Operator,
        graph: Provenance,
    ) -> None:
        store.set_policy("imu", declared(by="cli"))
        operator.answer = PROMOTE

        async def late(text: str, *, options: list[str], context: str = "", **_: Any) -> str:
            store.clear("shadow", "imu", by="dashboard")
            return PROMOTE

        trials._ask = late
        await trials.check_policies(now=days_in(store, 2))
        await _settled(trials)

        active = store.pointer("imu", "active")
        assert active is not None and active.version == 1
        assert host.feed()[-1] == "the answer on imu:2 came after its trial ended"

    async def test_a_trial_short_of_the_policy_is_left_running_and_nobody_is_asked(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        compared: dict[str, Any],
        operator: _Operator,
        graph: Provenance,
    ) -> None:
        store.set_policy("imu", declared({"min_days": 7}, by="cli"))

        verdicts = await trials.check_policies(now=days_in(store, 2))

        assert verdicts["imu"].met is False
        assert operator.asked == [] and host.feed() == []
        assert decisions_of(graph, "imu") == []
        assert store.pointer("imu", "shadow") is not None

    async def test_a_verdict_that_changes_is_said_and_recorded_once_each_way(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        compared: dict[str, Any],
        operator: _Operator,
        graph: Provenance,
    ) -> None:
        store.set_policy("imu", declared({"margin": 0.05}, by="cli"))

        await trials.check_policies(now=days_in(store, 2))
        await _settled(trials)
        compared["labels"]["shadow"]["f1"] = 0.81
        await trials.check_policies(now=days_in(store, 3))
        await trials.check_policies(now=days_in(store, 4))

        assert [m.split(":")[0] for m in host.feed()] == ["imu", "imu"]
        assert host.feed()[1].startswith("imu:2's trial no longer meets its policy: not met")
        assert [d["met"] for d in decisions_of(graph, "imu")] == [False, True]
        assert len(operator.asked) == 1

    async def test_a_decision_from_an_earlier_run_is_neither_asked_nor_promoted_again(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        compared: dict[str, Any],
        operator: _Operator,
        graph: Provenance,
    ) -> None:
        policy = store.set_policy("imu", declared({"automatic": True}, by="cli"))
        shadow = store.pointer("imu", "shadow")
        assert shadow is not None
        record_decision(
            graph,
            name="imu",
            shadow_version=2,
            active_version=1,
            since=shadow.moved_at,
            verdict=judge_trial(judged(), policy, since=shadow.moved_at, now=days_in(store, 1)),
            at=days_in(store, 1),
        )

        await trials.check_policies(now=days_in(store, 2))

        assert store.pointer("imu", "shadow") is not None
        assert host.feed() == [] and operator.asked == []

    async def test_a_policy_set_anew_is_decided_afresh(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        host: _Host,
        compared: dict[str, Any],
        operator: _Operator,
        graph: Provenance,
    ) -> None:
        """Asked under one policy and left unanswered, then promoted when the policy says so."""
        store.set_policy("imu", declared({"margin": 0.05}, by="cli", at=T0))
        now = days_in(store, 2)
        await trials.check_policies(now=now)
        await _settled(trials)
        assert len(operator.asked) == 1 and store.pointer("imu", "shadow") is not None

        store.set_policy("imu", declared({"margin": 0.05, "automatic": True}, by="cli", at=T0 + 1))
        await trials.check_policies(now=now + 60)

        active = store.pointer("imu", "active")
        assert active is not None and (active.version, active.by) == (2, POLICY)
        assert [d["policy"]["automatic"] for d in decisions_of(graph, "imu")] == [True, False]

    async def test_every_trial_sets_its_gauges_with_or_without_a_policy(
        self,
        store: ArtifactStore,
        trials: ShadowServing,
        compared: dict[str, Any],
    ) -> None:
        def gauge(series: Any, **labels: str) -> float | None:
            for sample in series.collect()[0].samples:
                if all(sample.labels.get(k) == v for k, v in labels.items()):
                    return sample.value
            return None

        assert await trials.check_policies(now=days_in(store, 2)) == {}
        assert gauge(model_metrics.AGREEMENT, model="imu") == 0.9
        assert gauge(model_metrics.SCORE, model="imu", side="shadow", metric="f1") == 0.9

        store.set_policy("imu", declared({"metric": "precision"}, by="cli"))
        await trials.check_policies(now=days_in(store, 2))
        assert gauge(model_metrics.SCORE, model="imu", side="active", metric="precision") == 0.8

        await trials.follow([("imu", "shadow", None, store.pointer("imu", "shadow"))])
        assert gauge(model_metrics.AGREEMENT, model="imu") is None
        assert gauge(model_metrics.SCORE, model="imu", side="shadow", metric="f1") is None

    async def test_without_a_policy_or_a_shadow_nothing_is_checked(
        self, store: ArtifactStore, trials: ShadowServing, compared: dict[str, Any]
    ) -> None:
        now = days_in(store, 2)
        assert await trials.check_policies(now=now) == {}
        store.set_policy("imu", declared(by="cli"))
        store.clear("shadow", "imu", by="test")
        assert await trials.check_policies(now=now) == {}


# ── The routes ────────────────────────────────────────────────────────────────


@pytest.fixture(name="client")
async def client_fixture(
    db: WactorzDB, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(runtime, "db", db)
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheRoutes:
    async def test_a_policy_is_set_over_the_defaults_then_over_itself_and_shown(
        self, client: TestClient, store: ArtifactStore
    ) -> None:
        first = await client.put("/api/models/imu/policy", json={"margin": 0.02})
        second = await client.put("/api/models/imu/policy", json={"automatic": True})
        detail = await (await client.get("/api/models/imu")).json()
        listed = await (await client.get("/api/models")).json()

        assert first.status == 200
        body = await second.json()
        assert (body["policy"]["margin"], body["policy"]["automatic"], body["policy"]["by"]) == (
            0.02,
            True,
            "dashboard",
        )
        assert detail["policy"]["margin"] == 0.02
        assert listed["models"][0]["policy"]["automatic"] is True

    async def test_what_cannot_be_a_policy_is_a_400_and_an_unknown_model_a_404(
        self, client: TestClient
    ) -> None:
        bad = await client.put("/api/models/imu/policy", json={"metric": "auc"})
        missing = await client.put("/api/models/none/policy", json={})

        assert bad.status == 400 and "not 'auc'" in (await bad.json())["error"]
        assert missing.status == 404

    async def test_a_policy_is_cleared_with_what_it_was(
        self, client: TestClient, store: ArtifactStore
    ) -> None:
        store.set_policy("imu", declared({"margin": 0.1}, by="cli"))

        cleared = await client.delete("/api/models/imu/policy")
        again = await client.delete("/api/models/imu/policy")

        assert (await cleared.json())["was"]["margin"] == 0.1
        assert (await again.json()) == {"policy": None, "was": None}
        assert (await (await client.get("/api/models/imu")).json())["policy"] is None

    async def test_the_trial_says_where_it_stands_against_the_policy(
        self, client: TestClient, store: ArtifactStore
    ) -> None:
        without = await (await client.get("/api/models/imu/trial")).json()
        store.set_policy("imu", declared({"min_days": 0, "min_labelled": 0}, by="cli"))
        with_policy = await (await client.get("/api/models/imu/trial")).json()

        assert without["policy"] is None
        verdict = with_policy["policy"]
        assert verdict["met"] is False
        assert [t["name"] for t in verdict["terms"] if not t["met"]] == [
            "judged",
            "margin",
            "significant",
        ]
        assert verdict["policy"]["min_days"] == 0
        assert verdict["words"].startswith("not met: f1 not measured")


# ── From chat and from the command line ───────────────────────────────────────


class _Actor:
    """The chat verb reaches nothing of main's; this stands in for MainActor."""


def chat() -> CommandContext:
    return CommandContext(actor=cast("MainActor", _Actor()))


class TestTheChatCommand:
    async def test_fields_are_set_over_what_was_set(self, store: ArtifactStore) -> None:
        first = await models_commands.policy_model(chat(), "imu margin=0.02 min_days=2")
        second = await models_commands.policy_model(chat(), "imu automatic=yes")

        assert first == (
            "imu is promoted when its shadow shows the main figure of its kind by 0.02 over 2 days "
            "and 100 labelled, "
            "significant, asked."
        )
        assert second.endswith("significant, promoted on its own.")
        policy = store.policy("imu")
        assert policy is not None and (policy.margin, policy.by) == (0.02, "chat")

    async def test_none_unsets_it(self, store: ArtifactStore) -> None:
        store.set_policy("imu", declared({"margin": 0.3}, by="cli"))

        reply = await models_commands.policy_model(chat(), "imu none")

        assert reply.startswith("imu has no policy now. It was: the main figure of its kind by 0.3")
        assert store.policy("imu") is None

    @pytest.mark.parametrize(
        ("argument", "says"),
        [
            ("imu", "Usage: /artifacts policy"),
            ("", "Usage"),
            ("imu cadence=5", "no 'cadence'"),
            ("imu margin", "no 'margin'"),
            ("imu margin=lots", "could not convert"),
            ("nothing margin=1", "no artifact 'nothing'"),
        ],
    )
    async def test_what_cannot_be_done_says_why(
        self, store: ArtifactStore, argument: str, says: str
    ) -> None:
        assert says in await models_commands.policy_model(chat(), argument)


class TestTheCommandLine:
    @pytest.fixture(autouse=True)
    def _capture(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.artifacts_cli")
        self.caplog = caplog

    def test_policy_sets_shows_and_clears(self, store: ArtifactStore) -> None:
        state = resolve_state_dir()

        assert (
            artifacts_cli.main(
                ["policy", "imu", "--margin", "0.02", "--automatic", "--state-dir", state]
            )
            == 0
        )
        assert artifacts_cli.main(["policy", "imu", "--min-days", "3", "--state-dir", state]) == 0

        policy = store.policy("imu")
        assert policy is not None
        assert (policy.margin, policy.automatic, policy.min_days, policy.by) == (
            0.02,
            True,
            3.0,
            "cli",
        )
        assert "promoted on its own" in self.caplog.text
        assert artifacts_cli.main(["policy", "imu", "--clear", "--state-dir", state]) == 0
        assert store.policy("imu") is None and "has no policy now" in self.caplog.text

    def test_show_includes_the_policy(
        self, store: ArtifactStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store.set_policy("imu", declared({"margin": 0.1}, by="cli"))

        assert artifacts_cli.main(["show", "imu:2", "--state-dir", resolve_state_dir()]) == 0

        shown = json.loads(capsys.readouterr().out)
        assert shown["policy"]["margin"] == 0.1

    def test_a_policy_that_cannot_be_is_an_error(self, store: ArtifactStore) -> None:
        code = artifacts_cli.main(
            ["policy", "imu", "--margin", "-1", "--state-dir", resolve_state_dir()]
        )

        assert code == 1 and "cannot be below zero" in self.caplog.text
