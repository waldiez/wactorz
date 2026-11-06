# SPDX-License-Identifier: Apache-2.0
"""Drift: three tests over two windows, an episode per shift, and the drift agent that raises them.

PSI, KS and ADWIN are checked on numbers whose answer is known; the check on
rows; the observation in the graph; and the agent end to end, over Parquet
the loader reads, with the store, the graph, the feed, the topic and the
gauges. ``docs/design/drift.md`` is the decision these pin.
"""

import random
import statistics
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import generate_latest

from wactorz.agents import drift_agent
from wactorz.agents.drift_agent import DriftAgent, DriftError, drift_topic
from wactorz.core.actor import Message, MessageType
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.paths import resolve_state_dir
from wactorz.core.persistence import WactorzDB
from wactorz.core.provenance import OBSERVATION, Provenance
from wactorz.data import data_root
from wactorz.data.parquet import Writer
from wactorz.drift import (
    ADWIN,
    KS,
    PSI,
    Adwin,
    Reading,
    Thresholds,
    Window,
    adwin_changes,
    alarmed,
    alarms_of,
    check_features,
    check_scores,
    ks,
    observation_id,
    psi,
    raised_episodes,
    record_alarm,
)
from wactorz.drift.detectors import kolmogorov_p
from wactorz.monitoring import model_metrics
from wactorz.monitoring.prometheus import PrometheusMonitor
from wactorz.web import api_models, runtime
from wactorz.web.app import build_app

T0 = 1_760_000_000.0
DAY = 86400.0
SOURCE = "sensors/imu"
SCORES = "anomalies/imu"


def _normal(seed: int, n: int, mean: float = 0.0, spread: float = 1.0) -> list[float]:
    r = random.Random(seed)
    return [r.gauss(mean, spread) for _ in range(n)]


# ── The detectors ─────────────────────────────────────────────────────────────


class TestPSI:
    def test_the_same_distribution_is_near_zero_and_a_shifted_one_is_not(self) -> None:
        reference = _normal(1, 2000)

        assert psi(reference, _normal(2, 2000)) < 0.05
        assert psi(reference, _normal(3, 2000, mean=1.0)) > 0.5

    def test_an_empty_bin_gives_a_finite_figure(self) -> None:
        index = psi([1.0, 2.0, 3.0, 4.0, 5.0] * 20, [100.0] * 50)

        assert index > 1.0 and index < float("inf")

    def test_nothing_to_compare_is_zero(self) -> None:
        assert psi([], [1.0, 2.0]) == 0.0
        assert psi([1.0, 2.0], []) == 0.0
        assert psi([3.0] * 50, [3.0] * 50) == 0.0

    def test_text_and_booleans_are_not_numbers(self) -> None:
        assert psi(["a", True, None, 1.0], [1.0]) == 0.0


class TestKS:
    def test_the_asymptotic_p_is_the_kolmogorov_series(self) -> None:
        # The textbook critical value: λ = 1.36 is p ≈ 0.05.
        assert kolmogorov_p(1.36) == pytest.approx(0.0495, abs=0.001)
        assert kolmogorov_p(0.0) == 1.0
        assert kolmogorov_p(3.0) < 1e-6

    def test_the_same_distribution_is_not_told_apart_and_a_shifted_one_is(self) -> None:
        reference = _normal(1, 2000)

        same = ks(reference, _normal(2, 2000))
        shifted = ks(reference, _normal(3, 2000, mean=1.0))

        assert same.statistic < 0.06 and same.p > 0.05
        assert shifted.statistic > 0.3 and shifted.p < 1e-6
        assert (shifted.reference, shifted.current) == (2000, 2000)

    def test_an_empty_side_is_no_distance(self) -> None:
        assert (ks([], [1.0]).statistic, ks([], [1.0]).p) == (0.0, 1.0)
        assert (ks([1.0], []).statistic, ks([1.0], []).p) == (0.0, 1.0)


class TestADWIN:
    def test_a_change_of_mean_is_found_near_where_it_happened(self) -> None:
        before = _normal(4, 1000, mean=0.0, spread=0.5)
        after = _normal(5, 1000, mean=2.0, spread=0.5)

        changes = adwin_changes(before + after)

        assert changes, "no change found"
        latest = changes[-1]
        assert 900 <= latest.index <= 1100
        assert latest.before is not None and latest.before == pytest.approx(0.0, abs=0.2)
        assert latest.after is not None and latest.after == pytest.approx(2.0, abs=0.5)
        assert latest.kept >= 5

    def test_a_stationary_sequence_is_left_alone(self) -> None:
        assert adwin_changes(_normal(6, 5000)) == []

    def test_the_window_keeps_an_exact_mean_and_variance_through_its_buckets(self) -> None:
        values = _normal(7, 700, mean=1.0) + _normal(8, 700, mean=4.0)
        detector = Adwin()
        for value in values:
            detector.add(value)

        kept = values[-detector.width :]
        assert detector.mean == pytest.approx(statistics.mean(kept))
        assert detector.variance == pytest.approx(statistics.pvariance(kept))
        assert detector.changes >= 1

    def test_what_is_not_a_number_is_skipped_and_positions_are_of_the_sequence_given(
        self,
    ) -> None:
        values: list[Any] = ["x", None, *_normal(9, 600, mean=0.0, spread=0.1)]
        values += [*_normal(10, 600, mean=3.0, spread=0.1), True]

        changes = adwin_changes(values)

        assert changes and 550 <= changes[-1].index <= 650
        assert Adwin().mean is None


# ── The check ─────────────────────────────────────────────────────────────────


def _rows(ts: list[float], **columns: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "ts": moment,
            "arrived": moment,
            "time_source": "payload",
            **{k: v[i] for k, v in columns.items()},
        }
        for i, moment in enumerate(ts)
    ]


class TestTheCheck:
    def test_every_numeric_column_is_tested_and_a_shifted_one_alarms(self) -> None:
        ts = [T0 + i for i in range(500)]
        reference = _rows(ts, ax=_normal(1, 500), ay=_normal(2, 500), note=["ok"] * 500)
        current = _rows(ts, ax=_normal(3, 500), ay=_normal(4, 500, mean=2.0), note=["ok"] * 500)

        readings = check_features(reference, current, source=SOURCE)

        assert [(r.test, r.column) for r in readings] == [
            (PSI, "ax"),
            (KS, "ax"),
            (PSI, "ay"),
            (KS, "ay"),
        ]
        assert {(r.test, r.column) for r in alarmed(readings)} == {(PSI, "ay"), (KS, "ay")}
        by_key = {r.key: r for r in readings}
        p = by_key[(KS, "ay")].p
        assert p is not None and p < 0.001
        assert by_key[(PSI, "ax")].rows == {"reference": 500, "current": 500}
        assert by_key[(PSI, "ay")].words().startswith("psi on ay ")

    def test_the_columns_named_are_the_ones_tested(self) -> None:
        ts = [T0 + i for i in range(200)]
        reference = _rows(ts, ax=_normal(1, 200), ay=_normal(2, 200))
        current = _rows(ts, ax=_normal(3, 200), ay=_normal(4, 200))

        readings = check_features(reference, current, source=SOURCE, columns=["ay"])

        assert {r.column for r in readings} == {"ay"}

    def test_too_few_rows_on_either_side_is_no_reading(self) -> None:
        ts = [T0 + i for i in range(50)]
        rows = _rows(ts, ax=_normal(1, 50))

        assert check_features(rows, rows, source=SOURCE) == []
        assert check_features(rows, rows, source=SOURCE, thresholds=Thresholds(min_rows=10))

    def test_the_scores_are_held_against_the_reference_and_adwin_says_when_they_changed(
        self,
    ) -> None:
        reference = _rows([T0 + i for i in range(600)], score=_normal(1, 600, 1.0, 0.2))
        now = [T0 + DAY + i * 10 for i in range(600)]
        current = _rows(now, score=_normal(2, 300, 1.0, 0.2) + _normal(3, 300, 4.0, 0.2))

        readings = check_scores(reference, current, source=SCORES)

        assert [r.test for r in readings] == [PSI, KS, ADWIN]
        change = readings[-1]
        assert change.alarmed and change.at is not None
        assert now[250] <= change.at <= now[350]
        assert change.detail["after"] == pytest.approx(4.0, abs=0.8)
        assert change.words().endswith(f"from {change.at:.0f}")

    def test_no_change_in_the_scores_is_a_reading_that_did_not_alarm(self) -> None:
        reference = _rows([T0 + i for i in range(600)], score=_normal(1, 600))
        current = _rows([T0 + DAY + i for i in range(600)], score=_normal(2, 600))

        readings = check_scores(reference, current, source=SCORES)

        assert alarmed(readings) == []
        assert readings[-1].test == ADWIN and readings[-1].at is None


# ── The graph ─────────────────────────────────────────────────────────────────


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "db" / "wactorz.db") as database:
        monkeypatch.setattr(drift_agent, "get_db", lambda: database)
        monkeypatch.setattr(api_models, "get_db", lambda: database)
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


def _reading(test: str = PSI, column: str = "ax", statistic: float = 0.4) -> Reading:
    return Reading(test, column, SOURCE, statistic=statistic, threshold=0.2, alarmed=True)


class TestTheObservation:
    def test_an_episode_is_written_once_and_read_back_newest_first(self, graph: Provenance) -> None:
        window = Window(SOURCE, T0, T0 + DAY)
        reference = Window(SOURCE, T0 - 7 * DAY, T0)
        first = record_alarm(
            graph,
            model="imu",
            version=3,
            reading=_reading(),
            window=window,
            reference=reference,
            by="agent:drift",
            at=T0 + DAY,
        )
        again = record_alarm(
            graph,
            model="imu",
            version=3,
            reading=_reading(statistic=0.9),
            window=window,
            reference=reference,
            by="agent:drift",
            at=T0 + DAY,
        )
        later = record_alarm(
            graph,
            model="imu",
            version=3,
            reading=_reading(KS),
            window=window,
            reference=reference,
            by="agent:drift",
            at=T0 + 2 * DAY,
        )

        assert first.id == again.id == observation_id("imu", 3, PSI, "ax", T0 + DAY)
        assert first.kind == OBSERVATION and (first.ts, first.ts_end) == (T0, T0 + DAY)
        assert first.payload["statistic"] == 0.4
        assert [a["id"] for a in alarms_of(graph, "imu")] == [later.id, first.id]
        assert alarms_of(graph, "imu")[0]["reference"] == reference.to_dict()
        assert alarms_of(graph, "other") == []

    def test_the_episodes_still_raised_are_those_of_the_version_over_the_window_ahead(
        self, graph: Provenance
    ) -> None:
        reference = Window(SOURCE, T0 - 7 * DAY, T0)
        old = record_alarm(
            graph,
            model="imu",
            version=2,
            reading=_reading(),
            window=Window(SOURCE, T0, T0 + DAY),
            reference=reference,
            by="agent:drift",
            at=T0 + DAY,
        )
        recent = record_alarm(
            graph,
            model="imu",
            version=3,
            reading=_reading(KS, "ay"),
            window=Window(SOURCE, T0 + DAY, T0 + 2 * DAY),
            reference=reference,
            by="agent:drift",
            at=T0 + 2 * DAY,
        )
        graph.add_node(
            OBSERVATION,
            ts=T0 + DAY,
            ts_end=T0 + 2 * DAY,
            source="models/imu",
            payload={"what": "something else", "version": 3},
        )

        assert raised_episodes(graph, "imu", version=3, since=T0 + 1.5 * DAY) == {
            (KS, "ay"): recent.id
        }
        assert raised_episodes(graph, "imu", version=2, since=T0 + 1.5 * DAY) == {}
        assert raised_episodes(graph, "imu", version=2, since=T0 + 0.5 * DAY) == {
            (PSI, "ax"): old.id
        }


# ── The agent ─────────────────────────────────────────────────────────────────


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> ArtifactStore:
    """``imu`` in two versions: v1 by hand, v2 made by the train step on a week ending at ``T0``, active."""
    store = ArtifactStore.in_state_dir(resolve_state_dir())
    for version in (1, 2):
        weights = tmp_path / f"imu-{version}.bin"
        weights.write_bytes(f"imu weights {version}".encode())
        metadata = (
            {"window": {"source": SOURCE, "since": T0 - 7 * DAY, "until": T0}}
            if version == 2
            else None
        )
        store.add(weights, "imu", format_name="raw", metadata=metadata)
    store.move("active", "imu:2", by="test")
    return store


def _record(source: str, ts: list[float], **columns: list[Any]) -> None:
    writer = Writer(data_root(resolve_state_dir()), source)
    for row in _rows(ts, **columns):
        writer.append(row)
    writer.close()


@pytest.fixture(name="recorded")
def recorded_fixture() -> None:
    """A week of readings the version learned from, and a day after ``T0`` that looks the same."""
    week = [T0 - 7 * DAY + i * 600 for i in range(7 * 144)]
    _record(SOURCE, week, ax=_normal(1, len(week)), ay=_normal(2, len(week)))
    _record(SCORES, week, score=_normal(3, len(week), 1.0, 0.2))
    day = [T0 + i * 300 for i in range(288)]
    _record(SOURCE, day, ax=_normal(4, 288), ay=_normal(5, 288))
    _record(SCORES, day, score=_normal(6, 288, 1.0, 0.2))


def _record_a_shifted_day(since: float, *, ay_mean: float = 3.0, score_mean: float = 1.0) -> None:
    day = [since + i * 300 for i in range(288)]
    _record(SOURCE, day, ax=_normal(7, 288), ay=_normal(8, 288, mean=ay_mean))
    _record(SCORES, day, score=_normal(9, 288, score_mean, 0.2))


@pytest.fixture(name="feed")
def feed_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        if topic.endswith("/logs"):
            seen.append(payload)
        else:
            seen.append({"topic": topic, **payload})

    monkeypatch.setattr(DriftAgent, "_mqtt_publish", record)
    return seen


def _lines(feed: list[dict[str, Any]]) -> list[str]:
    return [p["message"] for p in feed if "message" in p]


def _alarms(feed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in feed if p.get("topic") == drift_topic("imu")]


@pytest.fixture(name="sent")
def sent_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, Any]]:
    seen: list[tuple[str, Any, Any]] = []

    async def record(self: Any, target: str, kind: Any, payload: Any = None) -> bool:
        seen.append((target, kind, payload))
        return True

    monkeypatch.setattr(DriftAgent, "send", record)
    return seen


@pytest.fixture(name="agent")
async def agent_fixture(
    tmp_path: Path,
    store: ArtifactStore,
    feed: list[dict[str, Any]],
    sent: list[tuple[str, Any, Any]],
) -> AsyncIterator[DriftAgent]:
    agent = DriftAgent(
        name="drift",
        persistence_dir=str(tmp_path / "agents"),
        model="imu",
        scores=SCORES,
        min_rows=50,
    )
    yield agent
    model_metrics.forget_drift("imu")


def _gauge(metric: Any, **labels: str) -> float | None:
    for sample in metric.collect()[0].samples:
        if all(sample.labels.get(k) == v for k, v in labels.items()):
            return sample.value
    return None


class TestTheAgentChecks:
    async def test_the_source_and_the_reference_come_from_the_version_served(
        self, agent: DriftAgent, recorded: None, db: WactorzDB, feed: list[dict[str, Any]]
    ) -> None:
        readings = await agent.check(now=T0 + DAY)

        assert agent.source == "" and agent.status()["version"] == 2
        assert {(r.test, r.column, r.source) for r in readings} == {
            (PSI, "ax", SOURCE),
            (KS, "ax", SOURCE),
            (PSI, "ay", SOURCE),
            (KS, "ay", SOURCE),
            (PSI, "score", SCORES),
            (KS, "score", SCORES),
            (ADWIN, "score", SCORES),
        }
        assert alarmed(readings) == []
        assert readings[0].rows == {"reference": 7 * 144, "current": 288}
        assert _alarms(feed) == [] and _lines(feed) == []
        assert _gauge(model_metrics.DRIFT, model="imu", test=PSI, column="ay") is not None
        assert _gauge(model_metrics.DRIFT_ALARM, model="imu", test=PSI, column="ay") == 0.0
        assert alarms_of(Provenance(db), "imu") == []

    async def test_a_shift_is_one_episode_recorded_said_published_and_gauged(
        self, agent: DriftAgent, recorded: None, graph: Provenance, feed: list[dict[str, Any]]
    ) -> None:
        _record_a_shifted_day(T0 + DAY, score_mean=4.0)

        readings = await agent.check(now=T0 + 2 * DAY)
        await agent.check(now=T0 + 2 * DAY + 600)

        keys = {r.key for r in alarmed(readings)}
        assert {(PSI, "ay"), (KS, "ay"), (PSI, "score"), (KS, "score")} <= keys
        alarms = alarms_of(graph, "imu")
        assert {(a["test"], a["column"]) for a in alarms} == keys
        assert all(a["version"] == 2 and a["by"] == "agent:drift" for a in alarms)
        assert alarms[0]["reference"] == {"source": SOURCE, "since": T0 - 7 * DAY, "until": T0}
        published = _alarms(feed)
        assert {(p["test"], p["column"]) for p in published} == keys
        assert {p["observation"] for p in published} == {a["id"] for a in alarms}
        assert all(p["what"] == "drift" and p["version"] == 2 and "ts" in p for p in published)
        said = [line for line in _lines(feed) if "drifted" in line]
        assert len(said) == len(keys) and said[0].startswith("imu:2 drifted: ")
        assert _gauge(model_metrics.DRIFT_ALARM, model="imu", test=PSI, column="ay") == 1.0
        assert agent.status()["raised"]
        # The second check found the same episodes: nothing new.
        assert len(_alarms(feed)) == len(keys) and len(alarms_of(graph, "imu")) == len(keys)

    async def test_an_episode_settles_when_the_statistic_comes_back_and_may_be_raised_again(
        self, agent: DriftAgent, recorded: None, graph: Provenance, feed: list[dict[str, Any]]
    ) -> None:
        _record_a_shifted_day(T0 + DAY)
        await agent.check(now=T0 + 2 * DAY)
        raised = len(alarms_of(graph, "imu"))
        day = [T0 + 2 * DAY + i * 300 for i in range(288)]
        _record(SOURCE, day, ax=_normal(10, 288), ay=_normal(11, 288))
        _record(SCORES, day, score=_normal(12, 288, 1.0, 0.2))

        await agent.check(now=T0 + 3 * DAY)

        assert agent.status()["raised"] == []
        assert "imu: psi on ay settled" in _lines(feed)
        _record_a_shifted_day(T0 + 3 * DAY)
        await agent.check(now=T0 + 4 * DAY)
        assert len(alarms_of(graph, "imu")) == 2 * raised

    async def test_a_new_version_served_starts_the_episodes_afresh(
        self,
        agent: DriftAgent,
        recorded: None,
        store: ArtifactStore,
        graph: Provenance,
        feed: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        _record_a_shifted_day(T0 + DAY)
        await agent.check(now=T0 + 2 * DAY)
        raised = len(alarms_of(graph, "imu"))
        weights = tmp_path / "imu-3.bin"
        weights.write_bytes(b"imu weights 3")
        store.add(
            weights,
            "imu",
            format_name="raw",
            metadata={"window": {"source": SOURCE, "since": T0 + DAY, "until": T0 + 2 * DAY}},
        )
        store.move("active", "imu:3", by="test")

        readings = await agent.check(now=T0 + 2 * DAY + 600)

        assert "imu:3 is served now; drift is measured from it afresh" in _lines(feed)
        assert alarmed(readings) == [] and agent.status()["version"] == 3
        assert len(alarms_of(graph, "imu")) == raised

    async def test_after_a_restart_the_episodes_recorded_are_not_raised_again(
        self,
        agent: DriftAgent,
        recorded: None,
        graph: Provenance,
        feed: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        _record_a_shifted_day(T0 + DAY)
        await agent.check(now=T0 + 2 * DAY)
        raised = len(alarms_of(graph, "imu"))
        again = DriftAgent(
            name="drift",
            persistence_dir=str(tmp_path / "again"),
            model="imu",
            scores=SCORES,
            min_rows=50,
        )

        await again.check(now=T0 + 2 * DAY + 600)

        assert len(alarms_of(graph, "imu")) == raised
        assert len(_alarms(feed)) == raised
        assert {(r["test"], r["column"]) for r in again.status()["raised"]} == {
            (a["test"], a["column"]) for a in alarms_of(graph, "imu")
        }

    async def test_without_a_window_the_version_names_the_day_before_is_the_reference(
        self, agent: DriftAgent, store: ArtifactStore, recorded: None
    ) -> None:
        store.move("active", "imu:1", by="test")
        agent.source = SOURCE
        _record_a_shifted_day(T0 + DAY)

        readings = await agent.check(now=T0 + 2 * DAY)

        assert readings[0].rows == {"reference": 288, "current": 288}
        assert {(PSI, "ay"), (KS, "ay")} <= {r.key for r in alarmed(readings)}

    async def test_a_reference_given_at_spawn_is_the_one_used(
        self, tmp_path: Path, store: ArtifactStore, recorded: None, feed: list[dict[str, Any]]
    ) -> None:
        agent = DriftAgent(
            name="drift",
            persistence_dir=str(tmp_path / "a"),
            model="imu",
            source=SOURCE,
            reference={"since": T0 - 2 * DAY, "until": T0},
            columns="ax",
            min_rows=50,
        )

        readings = await agent.check(now=T0 + DAY)

        assert [r.column for r in readings] == ["ax", "ax"]
        assert readings[0].rows["reference"] == 2 * 144

    async def test_what_keeps_a_check_from_running_says_why(
        self, tmp_path: Path, store: ArtifactStore, recorded: None, feed: list[dict[str, Any]]
    ) -> None:
        nameless = DriftAgent(name="drift", persistence_dir=str(tmp_path / "n"))
        with pytest.raises(DriftError, match="needs the model"):
            await nameless.check(now=T0)
        store.move("active", "imu:1", by="test")
        sourceless = DriftAgent(name="drift", persistence_dir=str(tmp_path / "s"), model="imu")
        with pytest.raises(DriftError, match="set 'source'"):
            await sourceless.check(now=T0)
        unknown = DriftAgent(name="drift", persistence_dir=str(tmp_path / "u"), model="nobody")
        with pytest.raises(DriftError, match="nothing to drift from"):
            await unknown.check(now=T0)
        on_node = DriftAgent(name="drift", persistence_dir=str(tmp_path / "o"), model="imu")
        on_node._runner = object()  # pyright: ignore[reportAttributeAccessIssue]  # what a node's runner sets
        with pytest.raises(DriftError, match="checked on main"):
            await on_node.check(now=T0)


class TestTheAgentsTasks:
    async def test_check_runs_one_now_and_status_says_what_it_found(
        self,
        agent: DriftAgent,
        recorded: None,
        db: WactorzDB,
        sent: list[tuple[str, Any, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A task checks at the clock's now, which is the day after the readings.
        monkeypatch.setattr(drift_agent, "time", types.SimpleNamespace(time=lambda: T0 + DAY))
        await agent.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="main",
                payload={"action": "check", "_task_id": "t1"},
            )
        )
        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload={"_task_id": "t2"})
        )

        checked, status = [p for _, kind, p in sent if kind == MessageType.RESULT]
        assert checked["_task_id"] == "t1" and checked["model"] == "imu"
        assert {r["test"] for r in checked["readings"]} == {PSI, KS, ADWIN}
        assert status["_task_id"] == "t2" and status["checks"] == 1
        assert status["thresholds"]["min_rows"] == 50 and status["readings"]

    async def test_a_check_that_cannot_run_answers_with_the_reason(
        self,
        tmp_path: Path,
        store: ArtifactStore,
        sent: list[tuple[str, Any, Any]],
        feed: list[dict[str, Any]],
    ) -> None:
        agent = DriftAgent(name="drift", persistence_dir=str(tmp_path / "n"))

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload={"action": "check"})
        )

        ((_, _, reply),) = sent
        assert "needs the model" in reply["error"]

    async def test_starting_says_what_is_watched_and_stopping_drops_the_gauges(
        self,
        agent: DriftAgent,
        recorded: None,
        feed: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def manifest(self: Any, **_kwargs: Any) -> None:
            pass

        monkeypatch.setattr(DriftAgent, "publish_manifest", manifest)
        await agent.on_start()
        assert _lines(feed)[0].startswith("watching imu on the source its version names")
        await agent.check(now=T0 + DAY)
        assert _gauge(model_metrics.DRIFT, model="imu", test=PSI, column="ax") is not None

        await agent.on_stop()

        assert _gauge(model_metrics.DRIFT, model="imu", test=PSI, column="ax") is None


# ── The route and the registry ────────────────────────────────────────────────


@pytest.fixture(name="client")
async def client_fixture(
    db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(runtime, "db", db)
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheRoute:
    async def test_the_alarms_are_listed_newest_first(
        self, client: TestClient, graph: Provenance
    ) -> None:
        window = Window(SOURCE, T0, T0 + DAY)
        reference = Window(SOURCE, T0 - 7 * DAY, T0)
        first = record_alarm(
            graph,
            model="imu",
            version=2,
            reading=_reading(),
            window=window,
            reference=reference,
            by="agent:drift",
            at=T0 + DAY,
        )
        second = record_alarm(
            graph,
            model="imu",
            version=2,
            reading=_reading(KS),
            window=window,
            reference=reference,
            by="agent:drift",
            at=T0 + DAY + 1,
        )

        response = await client.get("/api/models/imu/drift")

        assert response.status == 200
        body = await response.json()
        assert body["name"] == "imu"
        assert [a["id"] for a in body["alarms"]] == [second.id, first.id]
        assert body["alarms"][1]["statistic"] == 0.4

    async def test_without_persistence_it_is_a_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "db", None)
        monkeypatch.setattr(api_models, "get_db", lambda: None)

        response = await client.get("/api/models/imu/drift")

        assert response.status == 503


class TestTheGauges:
    def test_the_model_metrics_are_served(self) -> None:
        monitor = PrometheusMonitor(lambda: None)
        model_metrics.record_train("imu", 12.5, ok=True)
        model_metrics.record_trial(
            "imu",
            {
                "agreement": 0.9,
                "labels": {
                    "kind": "classifier",
                    "active": {"macro_f1": 0.7},
                    "shadow": {"macro_f1": 0.8},
                },
            },
        )

        text = generate_latest(monitor._registry).decode()

        assert 'wactorz_model_train_seconds{model="imu"} 12.5' in text
        assert 'wactorz_model_train_runs_total{model="imu",outcome="ok"}' in text
        assert 'wactorz_model_shadow_agreement{model="imu"} 0.9' in text
        assert 'wactorz_model_score{metric="macro_f1",model="imu",side="shadow"} 0.8' in text
        assert (
            "wactorz_model_drift " in text
            or "wactorz_model_drift{" in text
            or "# HELP wactorz_model_drift " in text
        )
        model_metrics.forget_trial("imu")
        assert "wactorz_model_shadow_agreement{" not in generate_latest(monitor._registry).decode()

    def test_a_trial_not_judged_sets_the_agreement_alone(self) -> None:
        model_metrics.record_trial("bare", {"agreement": 0.5, "pairs": 10})

        assert _gauge(model_metrics.AGREEMENT, model="bare") == 0.5
        assert _gauge(model_metrics.SCORE, model="bare") is None
        model_metrics.forget_trial("bare")
