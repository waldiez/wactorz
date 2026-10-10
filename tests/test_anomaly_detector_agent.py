"""The anomaly detector: baselines from history, scoring live readings, and reporting.

The program is exec'd as it is at spawn and driven through a stand-in `agent`
whose history queries return what the test gives them. Its clock is set by the
test, the broker is a stand-in that delivers a set list of messages, and
`asyncio.sleep` is replaced in its namespace so the subscriber's retries do not
wait. Hours are local, as the detector's daily profiles are, so readings are
placed with `time.mktime`.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.programs import program_namespace
from wactorz.core.persistence.db import WactorzDB

NS = program_namespace("anomaly_detector_agent.py")
Baseline = NS["EntityBaseline"]

#: Ten in the morning, local time.
TEN = time.mktime((2026, 10, 7, 10, 0, 0, 0, 0, -1))


class _Actor:
    _mqtt_broker = "broker.test"
    _mqtt_port = 1883


class _Agent:
    """What the program uses of `agent`, recording what it publishes, persists and reports."""

    _actor = _Actor()

    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = {}
        self.stored: dict[str, Any] = dict(stored or {})
        self.published: list[tuple[str, Any]] = []
        self.alerts: list[tuple[str, str]] = []
        self.sent: list[tuple[str, Any]] = []
        self.logs: list[str] = []
        self.background: list[Coroutine[Any, Any, Any]] = []
        self.contract: dict[str, Any] = {}
        self.ts_rows: dict[str, list[dict[str, Any]]] = {}
        self.ha_rows: list[dict[str, Any]] = []
        self.ts_queries: list[dict[str, Any]] = []
        self.fail_query: Exception | None = None
        self.fail_ha: Exception | None = None
        self.fail_send: Exception | None = None

    def recall(self, key: str, default: Any = None) -> Any:
        return self.stored.get(key, default)

    def persist(self, key: str, value: Any) -> None:
        self.stored[key] = value

    async def log(self, text: str, level: str = "info") -> None:
        self.logs.append(f"{level}: {text}")

    async def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, payload))

    async def alert(self, message: str, severity: str = "warning") -> None:
        self.alerts.append((severity, message))

    async def send_to(self, target: str, payload: Any) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append((target, payload))

    def declare_contract(self, **contract: Any) -> None:
        self.contract = contract

    def run_in_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.background.append(coro)

    def query_ts(self, *, hours: float, entity_id: str, field: str | None, limit: int) -> Any:
        self.ts_queries.append({"hours": hours, "entity_id": entity_id, "field": field})
        if self.fail_query is not None:
            raise self.fail_query
        return self.ts_rows.get(entity_id, [])

    def query_ha_states(self, *, hours: float, limit: int) -> Any:
        if self.fail_ha is not None:
            raise self.fail_ha
        return self.ha_rows


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock(TEN)
    monkeypatch.setitem(NS, "time", clock)
    return clock


@pytest.fixture(name="agent")
async def agent_fixture(clock: _Clock) -> AsyncIterator[_Agent]:
    agent = _Agent()
    await NS["setup"](agent)
    yield agent
    for coro in agent.background:
        coro.close()


def _history(
    values: list[float], start: float = TEN - 86400, step: float = 600, field: str = "temperature"
) -> list[dict[str, Any]]:
    """Readings `step` seconds apart, from `start`."""
    return [{"ts": start + i * step, "value": v, "field": field} for i, v in enumerate(values)]


def _steady(n: int = 200, level: float = 20.0) -> list[float]:
    """Readings around `level`, a little noisy, rising and falling slowly."""
    return [level + ((i % 5) - 2) * 0.1 for i in range(n)]


def _ready_baseline(entity: str = "sensor.room", field: str = "temperature") -> Any:
    # A day of readings every ten minutes ends at ten o'clock, so every hour has six,
    # and ten o'clock itself, which is scored, has several more from the day before.
    rows = _history(_steady(288), start=TEN - 2 * 86400)
    return NS["_build_baseline_from_data"](entity, field, rows, 50)


# ── The baseline ───────────────────────────────────────────────────────────────


class TestBuildingABaseline:
    def test_too_few_samples_is_not_ready(self) -> None:
        baseline = NS["_build_baseline_from_data"]("sensor.a", "t", _history([1.0] * 10), 50)

        assert baseline.ready is False
        assert baseline.total_samples == 10

    def test_a_ready_baseline_knows_its_spread_profile_and_pace(self) -> None:
        rows = [*_history(_steady(288), start=TEN - 2 * 86400), {"ts": TEN, "value": None}]

        baseline = NS["_build_baseline_from_data"]("sensor.a", "t", rows, 50)

        assert baseline.ready is True
        assert baseline.total_samples == 288
        assert baseline.global_mean == pytest.approx(20.0, abs=0.01)
        assert (baseline.p1, baseline.p99) == (pytest.approx(19.8), pytest.approx(20.2))
        assert baseline.mean_interval == 600
        assert baseline.max_rate > 0
        assert sum(baseline.hourly_count) == 288
        assert baseline.is_binary is False

    def test_an_hour_with_no_readings_falls_back_to_the_whole(self) -> None:
        rows = _history(_steady(60), start=TEN, step=30)  # half an hour, all at ten

        baseline = NS["_build_baseline_from_data"]("sensor.a", "t", rows, 50)

        assert baseline.hourly_count[3] == 0
        assert baseline.hourly_mean[3] == baseline.global_mean
        assert baseline.hourly_std[3] == baseline.global_std

    def test_an_on_off_sensor_is_binary_and_counts_its_changes(self) -> None:
        values = [float(i % 2) for i in range(60)]  # flips every reading

        baseline = NS["_build_baseline_from_data"](
            "binary_sensor.door", "state", _history(values, step=360), 50
        )

        assert baseline.is_binary is True
        assert baseline.transition_freq == pytest.approx(59 / (59 * 360 / 3600))

    def test_it_round_trips_through_storage(self) -> None:
        baseline = _ready_baseline()

        again = Baseline.from_dict(baseline.to_dict())

        assert again.to_dict() == baseline.to_dict()


# ── Scoring ────────────────────────────────────────────────────────────────────


class TestScoring:
    def test_a_baseline_still_learning_flags_nothing(self) -> None:
        assert NS["_score_reading"](99.0, TEN, Baseline("sensor.a", "t"), 3.0, 4.0) == []

    def test_a_value_far_from_its_hour_is_statistical_and_out_of_range(self) -> None:
        anomalies = NS["_score_reading"](35.0, TEN, _ready_baseline(), 3.0, 4.0)

        types = {a["type"] for a in anomalies}
        assert types == {"statistical", "range"}
        assert all(0 < a["score"] <= 1.0 for a in anomalies)

    def test_an_hour_with_few_samples_is_not_judged_by_its_profile(self) -> None:
        baseline = _ready_baseline()
        baseline.hourly_count = [0] * 24

        types = {a["type"] for a in NS["_score_reading"](35.0, TEN, baseline, 3.0, 4.0)}

        assert types == {"range"}

    def test_below_the_range_too_and_a_flat_range_scores_nothing_extra(self) -> None:
        baseline = Baseline("sensor.a", "t")
        baseline.ready = True
        baseline.p1 = baseline.p99 = 5.0

        [anomaly] = NS["_score_reading"](4.0, TEN, baseline, 3.0, 4.0)

        assert anomaly["type"] == "range"
        assert anomaly["score"] == 0.0

    def test_a_change_faster_than_ever_seen_is_flagged(self) -> None:
        baseline = _ready_baseline()
        baseline.last_value, baseline.last_ts = 20.0, TEN - 1

        anomalies = NS["_score_reading"](20.1, TEN, baseline, 50.0, 4.0)

        assert [a["type"] for a in anomalies] == ["rate"]

    @pytest.mark.parametrize(
        ("interval", "last", "now", "absent"),
        [
            (0.0, TEN, TEN + 9999, False),  # no pace known
            (600.0, 0.0, TEN, False),  # never heard
            (600.0, TEN, TEN + 1000, False),  # late, not yet missing
            (600.0, TEN, TEN + 3600, True),
            (10.0, TEN, TEN + 3600, False),  # sub-minute pace is not watched
        ],
    )
    def test_silence_is_flagged_only_past_its_pace(
        self, interval: float, last: float, now: float, absent: bool
    ) -> None:
        baseline = Baseline("sensor.a", "t")
        baseline.mean_interval, baseline.last_ts = interval, last

        found = NS["_check_absence"](baseline, now, 3.0)

        assert (found is not None) == absent

    @pytest.mark.parametrize(
        ("score", "severity"), [(0.9, "critical"), (0.5, "warning"), (0.1, "info")]
    )
    def test_an_explanation_names_the_worst_and_the_rest(self, score: float, severity: str) -> None:
        anomalies = [
            {"type": "range", "score": score, "detail": "out of range"},
            {"type": "rate", "score": score / 2, "detail": "too fast"},
        ]

        text = NS["_explain_anomaly"]("sensor.living_room", "temperature", 1.0, anomalies, False)
        sinergym = NS["_explain_anomaly"]("sinergym.office", "reward", 1.0, anomalies[:1], True)

        assert text.startswith("⚠️ Living Room (temperature): out of range")
        assert "Also flagged by: rate" in text
        assert f"Severity: {severity}" in text
        assert sinergym.startswith("⚠️ [Sinergym] sinergym.office/reward")


# ── Live readings ──────────────────────────────────────────────────────────────


def _watching(agent: _Agent, key: str = "sensor.room:temperature") -> Any:
    baseline = _ready_baseline(*key.split(":"))
    agent.state["baselines"][key] = baseline
    agent.state["detection_active"] = True
    return baseline


async def _reading(agent: _Agent, clock: _Clock, at: float, topic: str, payload: Any) -> None:
    clock.now = at
    await NS["_process_live_reading"](agent, topic, payload)


class TestLiveReadings:
    async def test_a_jump_faster_than_ever_seen_is_reported_as_rate(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        _watching(agent)
        agent.state["stat_k"] = 1000.0  # judge the pace alone
        reading = {"entity_id": "sensor.room", "temperature": 20.0}
        await _reading(agent, clock, TEN, "sensors/room", reading)

        await _reading(agent, clock, TEN + 1, "sensors/room", {**reading, "temperature": 20.19})

        [record] = agent.state["anomaly_history"]
        assert record["types"] == ["rate"]

    async def test_while_learning_readings_are_remembered_not_judged(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        baseline = _watching(agent)
        agent.state["detection_active"] = False

        await _reading(
            agent, clock, TEN, "sensors/room", {"entity_id": "sensor.room", "temperature": 99}
        )

        assert agent.state["anomaly_history"] == []
        assert (baseline.last_value, baseline.last_ts) == (99.0, TEN)

    async def test_a_new_entity_gets_an_empty_baseline_to_fill(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        await _reading(
            agent,
            clock,
            TEN,
            "sensors/new",
            {"entity_id": "sensor.new", "humidity": 40, "_seq": 1, "node": "pi", "label": "x"},
        )

        assert set(agent.state["baselines"]) == {"sensor.new:humidity"}
        assert agent.state["baselines"]["sensor.new:humidity"].ready is False

    async def test_a_small_anomaly_below_the_sensitivity_is_kept_quiet(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        _watching(agent)
        agent.state["sensitivity"] = 0.99

        await _reading(
            agent, clock, TEN, "sensors/room", {"entity_id": "sensor.room", "temperature": 20.3}
        )

        assert agent.state["anomaly_history"] == []

    async def test_a_real_world_anomaly_is_published_and_alerted(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        _watching(agent)

        await _reading(
            agent, clock, TEN, "sensors/room", {"entity_id": "sensor.room", "temperature": 60}
        )

        [(topic, record)] = agent.published
        assert topic == "wactorz/anomalies/sensor.room"
        assert record["severity"] == "critical"
        assert agent.alerts and agent.alerts[0][0] == "critical"
        assert agent.stored["anomalies_detected"] == 1

    async def test_a_sinergym_anomaly_goes_to_its_topic_and_the_optimizer(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        _watching(agent, "sinergym.office:reward")
        payload = {"env_id": "office", "reward": 60.0, "obs": [1, "x"], "info": {"energy": 3}}

        await _reading(agent, clock, TEN, "sinergym/env/office/observation", payload)

        assert agent.published[0][0] == "sinergym/anomalies/office"
        assert agent.sent[0][0] == "sinergym-optimizer"
        assert agent.alerts == []
        assert {"sinergym.office:obs_0", "sinergym.office:info_energy"} <= set(
            agent.state["baselines"]
        )

    async def test_a_missing_optimizer_is_no_failure(self, agent: _Agent, clock: _Clock) -> None:
        _watching(agent, "sinergym.office:reward")
        agent.fail_send = RuntimeError("no such agent")

        await _reading(
            agent,
            clock,
            TEN,
            "sinergym/env/office/observation",
            {"env_id": "office", "reward": 60.0},
        )

        assert len(agent.state["anomaly_history"]) == 1

    @pytest.mark.parametrize(
        ("new_state", "value"),
        [
            ({"state": "21.5"}, 21.5),
            ({"state": "on"}, 1.0),
            ("closed", 0.0),
            ({"state": "unknown"}, None),
        ],
    )
    async def test_a_home_assistant_state_is_read_as_a_number(
        self, new_state: Any, value: float | None, agent: _Agent, clock: _Clock
    ) -> None:
        await _reading(
            agent,
            clock,
            TEN,
            "homeassistant/state_changes/x",
            {"entity_id": "binary_sensor.door", "new_state": new_state},
        )

        baseline = agent.state["baselines"].get("binary_sensor.door:state")
        assert (baseline.last_value if baseline else None) == value

    async def test_history_keeps_the_latest_two_hundred_and_stores_fifty(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        agent.state["anomaly_history"] = [{"n": i} for i in range(200)]
        anomaly = [{"type": "range", "score": 0.5, "detail": "x"}]

        await NS["_report_anomaly"](agent, "sensor.a", "t", 1.0, anomaly, "explained", False)

        assert len(agent.state["anomaly_history"]) == 200
        assert agent.state["anomaly_history"][0] == {"n": 1}
        assert len(agent.stored["anomaly_history"]) == 50

    async def test_an_info_anomaly_is_published_but_not_alerted(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        anomaly = [{"type": "range", "score": 0.2, "detail": "x"}]

        await NS["_report_anomaly"](agent, "sensor.a", "t", 1.0, anomaly, "explained", False)

        assert len(agent.published) == 1
        assert agent.alerts == []


# ── The periodic pass ──────────────────────────────────────────────────────────


class TestTheProcessPass:
    async def test_a_ready_baseline_ends_learning(self, agent: _Agent, clock: _Clock) -> None:
        agent.state["baselines"]["sensor.room:temperature"] = _ready_baseline()
        agent.state["last_baseline_build"] = TEN

        await NS["process"](agent)

        assert agent.state["detection_active"] is True

    async def test_learning_ends_after_its_period_even_without_baselines(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        agent.state["last_baseline_build"] = TEN
        clock.now = TEN + 169 * 3600
        agent.state["last_baseline_build"] = clock.now

        await NS["process"](agent)

        assert agent.state["detection_active"] is True

    async def test_a_silent_entity_is_reported(self, agent: _Agent, clock: _Clock) -> None:
        baseline = _watching(agent)
        baseline.last_ts = TEN
        agent.state["last_baseline_build"] = TEN
        clock.now = TEN + 3 * 3600

        await NS["process"](agent)

        [record] = agent.state["anomaly_history"]
        assert record["types"] == ["absence"]

    async def test_baselines_are_rebuilt_when_due_and_keep_the_last_reading(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        old = Baseline("sensor.room", "temperature")
        old.last_value, old.last_ts = 21.0, TEN - 5
        agent.state["baselines"]["sensor.room:temperature"] = old
        agent.state["monitored_entities"] = ["sensor.room:temperature", "sensor.hall"]
        agent.ts_rows = {
            "sensor.room": _history(_steady()),
            "sensor.hall": _history(_steady(), field="humidity") + _history([1.0], field="co2"),
        }

        await NS["process"](agent)

        baselines = agent.state["baselines"]
        assert baselines["sensor.room:temperature"].ready is True
        assert baselines["sensor.room:temperature"].last_value == 21.0
        assert baselines["sensor.hall:humidity"].ready is True
        assert baselines["sensor.hall:co2"].ready is False
        assert agent.ts_queries[0] == {
            "hours": 720,
            "entity_id": "sensor.room",
            "field": "temperature",
        }
        assert set(agent.stored["baselines"]) == set(baselines)
        assert agent.stored["last_baseline_build"] == TEN


class TestRebuilding:
    async def test_home_assistant_states_become_numbers_and_one_bad_one_spoils_nothing(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        start = TEN - 86400
        agent.ha_rows = (
            [
                {"entity_id": "binary_sensor.door", "ts": start + i * 600, "new_state": s}
                for i, s in enumerate(["on", "off"] * 30)
            ]
            + [
                {"entity_id": "sensor.temp", "ts": start + i * 600, "new_state": str(20 + i % 3)}
                for i in range(60)
            ]
            + [
                {"entity_id": "sensor.temp", "ts": start, "new_state": None},
                {"entity_id": "", "ts": start, "new_state": "on"},
            ]
        )

        await NS["_rebuild_baselines"](agent)

        baselines = agent.state["baselines"]
        assert baselines["binary_sensor.door:state"].is_binary is True
        assert baselines["sensor.temp:state"].ready is True
        assert not any("failed" in line for line in agent.logs)

    async def test_a_failed_query_is_logged_and_the_rest_go_on(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        agent.state["monitored_entities"] = ["sensor.a"]
        agent.fail_query = OSError("locked")
        agent.fail_ha = OSError("locked too")

        await NS["_rebuild_baselines"](agent)

        assert any("Baseline build failed for sensor.a" in line for line in agent.logs)
        assert any("HA state baseline build failed" in line for line in agent.logs)
        assert agent.stored["baselines"] == {}

    async def test_with_nothing_configured_it_finds_what_the_store_holds(
        self, agent: _Agent, clock: _Clock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = WactorzDB(tmp_path / "wactorz.db")
        try:
            db.write_sensor_batch([(TEN, "sensors/a", "sensor.a", "t", 1.0, "", "", "", "")])
            db.write_ha_state(TEN, "light.desk", "off", "on")
            monkeypatch.setitem(NS, "get_db", lambda: db)

            await NS["_rebuild_baselines"](agent)
        finally:
            db.close()

        assert agent.state["monitored_entities"] == ["light.desk", "sensor.a"]

    async def test_a_store_that_cannot_be_read_finds_nothing(
        self, agent: _Agent, clock: _Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Closed:
            @property
            def conn(self) -> Any:
                raise RuntimeError("closed")

        monkeypatch.setitem(NS, "get_db", _Closed)

        assert await NS["_discover_entities"](agent) == []


# ── The subscriber ─────────────────────────────────────────────────────────────


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Broker:
    """`mqtt_client` as the program calls it: refuses once, then delivers and stops."""

    def __init__(self, messages: list[_Message]) -> None:
        self._messages = messages
        self._refused = False
        self.subscribed: list[str] = []

    def __call__(self, host: str, port: int) -> "_Broker":
        assert (host, port) == ("broker.test", 1883)
        if not self._refused:
            self._refused = True
            raise OSError("refused")
        return self

    async def __aenter__(self) -> "_Broker":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, pattern: str) -> None:
        self.subscribed.append(pattern)

    @property
    def messages(self) -> AsyncIterator[_Message]:
        return self._deliver()

    async def _deliver(self) -> AsyncIterator[_Message]:
        for message in self._messages:
            yield message
        raise asyncio.CancelledError


class _FastAsyncio:
    CancelledError = asyncio.CancelledError

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await asyncio.sleep(0)


@pytest.fixture(name="sleeps")
def sleeps_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FastAsyncio]:
    fast = _FastAsyncio()
    monkeypatch.setitem(NS, "asyncio", fast)
    yield fast


class TestTheSubscriber:
    async def test_it_retries_subscribes_and_reads_what_it_can(
        self, agent: _Agent, clock: _Clock, sleeps: _FastAsyncio, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker(
            [
                _Message("sensors/a", b'{"entity_id": "sensor.a", "t": 1}'),
                _Message("sensors/a", b"not json"),
                _Message("sensors/a", b"[1, 2]"),
                _Message("sinergym/env/x/observation", b'{"info": 5}'),
            ]
        )
        monkeypatch.setitem(NS, "mqtt_client", broker)

        await NS["_mqtt_detector"](agent)

        assert sleeps.slept == [5]
        assert broker.subscribed == NS["MONITOR_TOPICS"]
        assert "sensor.a:t" in agent.state["baselines"]


# ── Commands ───────────────────────────────────────────────────────────────────


class TestCommands:
    async def test_setup_restores_what_was_kept(self, clock: _Clock) -> None:
        stored = {
            "baselines": {"sensor.a:t": _ready_baseline("sensor.a", "t").to_dict()},
            "sensitivity": 0.5,
            "anomalies_detected": 3,
        }
        agent = _Agent(stored)
        await NS["setup"](agent)
        for coro in agent.background:
            coro.close()

        assert agent.state["baselines"]["sensor.a:t"].ready is True
        assert agent.state["stat_k"] == pytest.approx(4.5)
        assert agent.state["anomalies_detected"] == 3
        assert agent.contract["publishes"] == ["wactorz/anomalies"]

    async def test_status_and_baselines(self, agent: _Agent, clock: _Clock) -> None:
        _watching(agent)
        agent.state["last_baseline_build"] = TEN - 7200

        status = await NS["handle_task"](agent, {"action": "status"})
        baselines = await NS["handle_task"](agent, {"action": "baselines"})

        assert status["baselines_ready"] == 1
        assert "Last baseline build: 2.0h ago" in status["result"]
        assert baselines["baselines"]["sensor.room:temperature"]["ready"] is True

    async def test_a_report_of_the_latest(self, agent: _Agent, clock: _Clock) -> None:
        empty = await NS["handle_task"](agent, {"action": "report"})
        anomaly = [{"type": "range", "score": 0.5, "detail": "x"}]
        for value in (1.0, 2.0, 3.0):
            await NS["_report_anomaly"](agent, "sensor.a", "t", value, anomaly, "x", False)

        two = await NS["handle_task"](agent, {"action": "report", "n": 2})

        assert empty["result"] == "No anomalies detected yet."
        assert [a["value"] for a in two["anomalies"]] == [2.0, 3.0]
        assert "[warning] sensor.a:t" in two["result"]

    @pytest.mark.parametrize("n", ["a few", 0, -3])
    async def test_a_report_count_that_is_not_one_is_the_default(
        self, n: Any, agent: _Agent, clock: _Clock
    ) -> None:
        anomaly = [{"type": "range", "score": 0.5, "detail": "x"}]
        for value in range(12):
            await NS["_report_anomaly"](agent, "sensor.a", "t", float(value), anomaly, "x", False)

        report = await NS["handle_task"](agent, {"action": "report", "n": n})

        assert len(report["anomalies"]) == 10

    async def test_train_and_reset(self, agent: _Agent, clock: _Clock) -> None:
        agent.state["monitored_entities"] = ["sensor.room:temperature"]
        agent.ts_rows = {"sensor.room": _history(_steady())}

        trained = await NS["handle_task"](agent, {"action": "train"})
        reset = await NS["handle_task"](agent, {"action": "reset"})

        assert trained["result"] == "Baselines rebuilt: 1 ready / 1 total"
        assert "cleared" in reset["result"]
        assert agent.state["baselines"] == {}
        assert agent.stored["baselines"] == {}

    async def test_configure_takes_effect_now_and_after_a_restart(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        result = await NS["handle_task"](
            agent,
            {
                "action": "configure",
                "sensitivity": "0.5",
                "learning_period_hours": 1,
                "rebuild_interval_hours": 2,
                "baseline_hours": 48,
                "entities": ["sensor.a"],
            },
        )

        assert result["result"] == "Configured"
        assert agent.state["sensitivity"] == 0.5
        assert agent.state["stat_k"] == pytest.approx(4.5)
        assert agent.state["learning_period_h"] == 1
        assert agent.state["rebuild_interval_h"] == 2
        assert agent.state["baseline_hours"] == 48
        assert agent.state["monitored_entities"] == ["sensor.a"]
        restarted = _Agent(agent.stored)
        await NS["setup"](restarted)
        for coro in restarted.background:
            coro.close()
        assert restarted.state["learning_period_h"] == 1
        assert restarted.state["monitored_entities"] == ["sensor.a"]

    async def test_configure_refuses_a_number_that_is_not_one(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        result = await NS["handle_task"](agent, {"action": "configure", "sensitivity": "high"})

        assert "must be a number" in result["result"]
        assert agent.state["sensitivity"] == 0.3

    async def test_entities_json_text_a_bare_string_and_help(
        self, agent: _Agent, clock: _Clock
    ) -> None:
        agent.state["monitored_entities"] = ["sensor.a"]

        listed = await NS["handle_task"](agent, {"text": '{"action": "entities"}'})
        as_string = await NS["handle_task"](agent, "status")
        helped = await NS["handle_task"](agent, {"text": "what can you do?"})

        assert listed["entities"] == ["sensor.a"]
        assert "detection_active" in as_string
        assert "status" in helped["commands"]

    @pytest.mark.parametrize(
        ("age", "said"),
        [(30, "30s ago"), (600, "10min ago"), (7200, "2.0h ago"), (3 * 86400, "3.0d ago")],
    )
    def test_ages_read_in_the_unit_that_fits(self, age: float, said: str, clock: _Clock) -> None:
        assert NS["_format_age"](TEN - age) == said
        assert NS["_format_age"](0) == "never"
