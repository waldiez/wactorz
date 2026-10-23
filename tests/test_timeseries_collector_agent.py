"""The time-series collector: what it buffers from each topic, and what reaches SQLite.

The program is exec'd as it is at spawn and driven through a stand-in `agent`,
against a real database in the test's own directory, so the rows it builds are
checked against the tables they have to fit. The broker is a stand-in that
delivers a set list of messages, and `asyncio.sleep` is replaced in the
program's namespace so its background loops run without waiting.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.programs import program_namespace
from tests.waiting import until
from wactorz.core.persistence.db import WactorzDB

NS = program_namespace("timeseries_collector_agent.py")


class _Actor:
    _mqtt_broker = "broker.test"
    _mqtt_port = 1883


class _Agent:
    """What the program uses of `agent`, recording what it publishes, persists and logs."""

    actor_id = "ts-1"
    _actor = _Actor()

    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = {}
        self.stored: dict[str, Any] = dict(stored or {})
        self.published: list[tuple[str, Any]] = []
        self.logs: list[str] = []
        self.background: list[Coroutine[Any, Any, Any]] = []
        self.contract: dict[str, Any] = {}

    def recall(self, key: str, default: Any = None) -> Any:
        return self.stored.get(key, default)

    def persist(self, key: str, value: Any) -> None:
        self.stored[key] = value

    async def log(self, text: str, level: str = "info") -> None:
        self.logs.append(f"{level}: {text}")

    async def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, payload))

    def declare_contract(self, **contract: Any) -> None:
        self.contract = contract

    def run_in_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.background.append(coro)


class _FastAsyncio:
    """`asyncio` as the program sees it, with a sleep that only yields and records."""

    CancelledError = asyncio.CancelledError

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await asyncio.sleep(0)


class _FailingDb:
    """The real database, failing the writes the test names."""

    def __init__(self, db: WactorzDB, fail: str, times: int = 1, after: int = 0) -> None:
        self._db = db
        self._fail = fail
        self._times = times
        self._after = after
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._db, name)
        if name != self._fail:
            return method

        def failing(*args: Any) -> Any:
            self.calls += 1
            if self._times > 0 and self.calls > self._after:
                self._times -= 1
                raise OSError("disk full")
            return method(*args)

        return failing


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    db = WactorzDB(tmp_path / "state" / "wactorz.db")
    monkeypatch.setitem(NS, "get_db", lambda: db)
    yield db
    db.close()


@pytest.fixture(name="sleeps")
def sleeps_fixture(monkeypatch: pytest.MonkeyPatch) -> _FastAsyncio:
    fast = _FastAsyncio()
    monkeypatch.setitem(NS, "asyncio", fast)
    return fast


@pytest.fixture(name="agent")
async def agent_fixture() -> AsyncIterator[_Agent]:
    agent = _Agent()
    await NS["setup"](agent)
    yield agent
    for coro in agent.background:
        coro.close()


def _route(agent: _Agent, topic: str, payload: Any) -> None:
    NS["_route_message"](agent, topic, payload)


def _rows(db: WactorzDB, table: str) -> list[dict[str, Any]]:
    cursor = db.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")  # a literal table name
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


# ── Starting ───────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_defaults_contract_and_workers(self, agent: _Agent) -> None:
        assert agent.state["topics"] == NS["DEFAULT_TOPICS"]
        assert agent.state["retention_days"] == 90
        assert agent.state["prune_interval_s"] == 6 * 3600
        assert agent.contract == {
            "publishes": ["agents/ts-1/storage"],
            "subscribes": NS["DEFAULT_TOPICS"],
        }
        assert len(agent.background) == 3

    async def test_what_was_configured_before_is_kept(self) -> None:
        agent = _Agent({"topics": ["custom/#"], "retention_days": 2, "prune_interval_hours": 1})
        await NS["setup"](agent)
        for coro in agent.background:
            coro.close()

        assert agent.state["topics"] == ["custom/#"]
        assert agent.state["retention_days"] == 2.0
        assert agent.state["prune_interval_s"] == 3600.0


# ── What each kind of topic becomes ────────────────────────────────────────────


class TestRouting:
    async def test_a_sensor_message_becomes_a_row_per_field(
        self, agent: _Agent, db: WactorzDB
    ) -> None:
        _route(
            agent,
            "sensors/kitchen",
            {
                "entity_id": "sensor.kitchen",
                "agent": "bme280",
                "node": "pi",
                "ts": 1.0,
                "_seq": 7,
                "temperature": 21.5,
                "door": "open",
                "note": "",
            },
        )
        NS["_flush"](agent)

        rows = {r["field"]: r for r in _rows(db, "sensor_readings")}
        assert set(rows) == {"temperature", "door"}
        assert rows["temperature"]["value"] == 21.5
        assert rows["door"]["value_str"] == "open"
        assert rows["door"]["value"] is None
        assert (rows["temperature"]["agent"], rows["temperature"]["node"]) == ("bme280", "pi")

    async def test_anything_but_an_object_is_ignored(self, agent: _Agent) -> None:
        _route(agent, "sensors/x", [1, 2, 3])

        assert agent.state["sensor_buffer"] == []

    async def test_detections_one_or_many(self, agent: _Agent, db: WactorzDB) -> None:
        _route(
            agent,
            "custom/detections/cam1",
            {
                "node": "pi",
                "detections": [
                    {"class": "person", "confidence": 0.9, "bbox": [1, 2], "frame_id": 4, "x": 1},
                    {"class": "cat"},
                ],
            },
        )
        _route(agent, "custom/detection", {"class": "dog", "confidence": 0.5, "agent": "yolo"})
        NS["_flush"](agent)

        rows = _rows(db, "detections")
        assert [r["class_name"] for r in rows] == ["person", "cat", "dog"]
        assert rows[0]["agent"] == "cam1"
        assert json.loads(rows[0]["metadata"]) == {"x": 1}
        assert rows[0]["frame_id"] == 4
        assert rows[2]["agent"] == "yolo"

    async def test_a_sinergym_observation_is_spread_into_fields(
        self, agent: _Agent, db: WactorzDB
    ) -> None:
        _route(
            agent,
            "sinergym/env/office/observation",
            {
                "env_id": "office",
                "episode": 2,
                "step": 40,
                "reward": -1.5,
                "mode": "train",
                "obs": [20.0, "skip", 3],
                "action": [1],
                "info": {"energy": 12.5, "label": "x"},
            },
        )
        NS["_flush"](agent)

        rows = {r["field"]: r for r in _rows(db, "sensor_readings")}
        assert set(rows) == {
            "reward",
            "obs_0",
            "obs_2",
            "action_0",
            "step",
            "episode",
            "info_energy",
        }
        assert rows["reward"]["entity_id"] == "sinergym.office"
        assert rows["step"]["value"] == 40.0
        assert rows["obs_0"]["agent"] == "sinergym-train"

    async def test_only_an_episode_end_is_kept(self, agent: _Agent, db: WactorzDB) -> None:
        _route(agent, "sinergym/env/office/episode", {"event": "episode_start"})
        _route(
            agent,
            "sinergym/env/office/episode",
            {"event": "episode_end", "total_reward": -40, "steps": 96, "duration_s": "slow"},
        )
        NS["_flush"](agent)

        fields = {r["field"] for r in _rows(db, "sensor_readings")}
        assert fields == {"ep_total_reward", "ep_steps"}

    @pytest.mark.parametrize(
        ("payload", "old", "new", "attributes"),
        [
            (
                {
                    "entity_id": "light.desk",
                    "old_state": {"state": "off"},
                    "new_state": {"state": "on", "attributes": {"brightness": 200}},
                    "context": {"id": "ctx-1"},
                },
                "off",
                "on",
                {"brightness": 200},
            ),
            ({"entity_id": "light.desk", "old_state": "off", "new_state": "on"}, "off", "on", {}),
        ],
        ids=["nested", "flat"],
    )
    async def test_a_home_assistant_state_change(
        self,
        payload: dict[str, Any],
        old: str,
        new: str,
        attributes: dict[str, Any],
        agent: _Agent,
        db: WactorzDB,
    ) -> None:
        _route(agent, "homeassistant/state_changes/light", payload)
        NS["_flush"](agent)

        [row] = _rows(db, "ha_state_changes")
        assert (row["old_state"], row["new_state"], row["domain"]) == (old, new, "light")
        assert json.loads(row["attributes"]) == attributes


# ── Writing ────────────────────────────────────────────────────────────────────


class TestFlushing:
    async def test_buffers_are_written_and_counted(self, agent: _Agent, db: WactorzDB) -> None:
        _route(agent, "sensors/a", {"t": 1})
        _route(agent, "custom/detection", {"class": "cat"})
        _route(agent, "homeassistant/state_changes/x", {"entity_id": "switch.x"})

        NS["_flush"](agent)

        assert db.stats()["sensor_readings"] == 1
        assert db.stats()["detections"] == 1
        assert db.stats()["ha_state_changes"] == 1
        assert agent.state["total_written"] == 3
        assert agent.state["sensor_buffer"] == []

    async def test_without_a_database_the_rows_wait(
        self, agent: _Agent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(NS, "get_db", lambda: None)
        _route(agent, "sensors/a", {"t": 1})

        NS["_flush"](agent)

        assert len(agent.state["sensor_buffer"]) == 1

    @pytest.mark.parametrize(
        ("method", "after", "topic", "payload", "table"),
        [
            ("write_sensor_batch", 0, "sensors/a", {"t": 1, "h": 2}, "sensor_readings"),
            (
                "write_detection",
                1,
                "custom/detections/c",
                {"detections": [{"class": "a"}] * 3},
                "detections",
            ),
            (
                "write_ha_state",
                1,
                "homeassistant/state_changes/x",
                {"entity_id": "switch.x"},
                "ha_state_changes",
            ),
        ],
    )
    async def test_a_failed_write_is_kept_and_written_once_on_the_next(
        self,
        method: str,
        after: int,
        topic: str,
        payload: dict[str, Any],
        table: str,
        agent: _Agent,
        db: WactorzDB,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failure partway through a buffer leaves none of it written, so the retry adds no copy."""
        _route(agent, topic, payload)
        _route(agent, topic, payload)
        expected = sum(
            len(agent.state[buffer])
            for buffer in ("sensor_buffer", "detection_buffer", "ha_buffer")
        )
        failing = _FailingDb(db, method, after=after)
        monkeypatch.setitem(NS, "get_db", lambda: failing)

        with caplog.at_level(logging.WARNING, logger="timeseries-collector"):
            NS["_flush"](agent)
        assert "kept for the next flush" in caplog.text
        NS["_flush"](agent)

        assert db.stats()[table] == expected
        assert agent.state["total_written"] == expected


# ── The background workers ─────────────────────────────────────────────────────


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Broker:
    """`mqtt_client` as the program calls it: refuses a few times, then delivers and stops."""

    def __init__(self, messages: list[_Message], refusals: list[Exception] | None = None) -> None:
        self._messages = messages
        self._refusals = list(refusals or [])
        self.subscribed: list[str] = []

    def __call__(self, host: str, port: int) -> "_Broker":
        assert (host, port) == ("broker.test", 1883)
        if self._refusals:
            raise self._refusals.pop(0)
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
        # Ends the subscriber the way a stop does.
        raise asyncio.CancelledError


class TestTheWorkers:
    async def test_the_subscriber_counts_and_routes_what_it_hears(
        self, agent: _Agent, sleeps: _FastAsyncio, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker(
            [
                _Message("sensors/a", b'{"t": 1}'),
                _Message("sensors/a", b"not json"),
                _Message("sensors/a", b"\xff\xfe"),
                _Message("custom/detection", b'{"detections": [5]}'),
            ],
            refusals=[OSError("refused"), OSError("refused"), OSError("reset")],
        )
        monkeypatch.setitem(NS, "mqtt_client", broker)

        await NS["_mqtt_subscriber"](agent)

        assert broker.subscribed == NS["DEFAULT_TOPICS"]
        assert agent.state["total_received"] == 2
        assert len(agent.state["sensor_buffer"]) == 1
        errors = [line for line in agent.logs if "MQTT error" in line]
        assert len(errors) == 2  # the same error twice is said once
        assert sleeps.slept == [5, 5, 5]

    async def test_the_flush_loop_writes_and_flushes_once_more_on_stop(
        self, agent: _Agent, db: WactorzDB, sleeps: _FastAsyncio
    ) -> None:
        loop = asyncio.ensure_future(NS["_flush_loop"](agent))
        _route(agent, "sensors/a", {"t": 1})
        await until(lambda: db.stats()["sensor_readings"] == 1, "a periodic flush")

        _route(agent, "sensors/a", {"t": 2})
        loop.cancel()
        await loop

        assert db.stats()["sensor_readings"] == 2

    async def test_the_flush_loop_survives_a_failure(
        self, agent: _Agent, sleeps: _FastAsyncio, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(_agent: Any) -> None:
            raise RuntimeError("no disk")

        monkeypatch.setitem(NS, "_flush", broken)
        loop = asyncio.ensure_future(NS["_flush_loop"](agent))
        await until(lambda: len(sleeps.slept) >= 3, "the loop going on after a failure")
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

        assert any("no disk" in line for line in agent.logs)

    async def test_the_prune_loop_survives_a_failure_and_follows_the_interval(
        self, agent: _Agent, db: WactorzDB, sleeps: _FastAsyncio, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failing = _FailingDb(db, "prune_old_data")
        monkeypatch.setitem(NS, "get_db", lambda: failing)
        loop = asyncio.ensure_future(NS["_prune_loop"](agent))
        await until(lambda: failing.calls >= 2, "a prune after the failed one")

        await NS["handle_task"](agent, {"action": "configure", "prune_interval_hours": 2})
        await until(lambda: 7200 in sleeps.slept, "the loop waiting the new interval")
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

        assert sleeps.slept[0] == 6 * 3600


# ── Storage ────────────────────────────────────────────────────────────────────


class TestStorage:
    @pytest.mark.parametrize(
        ("size", "said"),
        [
            (512, "512 B"),
            (2048, "2.0 KB"),
            (5 * 1024**2, "5.0 MB"),
            (3 * 1024**3, "3.0 GB"),
            (2 * 1024**4, "2.0 TB"),
        ],
    )
    def test_sizes_read_in_the_unit_that_fits(self, size: int, said: str) -> None:
        assert NS["_human_bytes"](size) == said

    async def test_the_report_measures_the_database_and_is_published(
        self, agent: _Agent, db: WactorzDB
    ) -> None:
        _route(agent, "sensors/a", {"t": 1})

        await NS["process"](agent)

        [(topic, report)] = agent.published
        assert topic == "agents/ts-1/storage"
        assert report["db_bytes"] > 0
        assert report["table_rows"]["sensor_readings"] == 1
        assert report["total_written"] == 1

    async def test_a_database_without_a_path_measures_nothing(
        self, agent: _Agent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Unplaced:
            def stats(self) -> dict[str, int]:
                return {}

        monkeypatch.setitem(NS, "get_db", _Unplaced)

        assert NS["_storage_report"](agent)["db_bytes"] == 0

    async def test_without_a_database_nothing_is_published(
        self, agent: _Agent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(NS, "get_db", lambda: None)

        result = await NS["handle_task"](agent, {"action": "storage"})
        await NS["process"](agent)

        assert result == {"result": "persistence not initialised"}
        assert agent.published == []

    async def test_a_failed_report_is_logged_not_raised(
        self, agent: _Agent, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(NS, "get_db", lambda: _FailingDb(db, "stats"))

        await NS["process"](agent)

        assert any("storage report failed" in line for line in agent.logs)


# ── Commands ───────────────────────────────────────────────────────────────────


def _old_reading(db: WactorzDB, days_ago: float) -> None:
    then = time.time() - days_ago * 86400
    db.write_sensor_batch([(then, "sensors/a", "sensor.a", "t", 1.0, "", "", "", "")])


class TestCommands:
    async def test_stats(self, agent: _Agent, db: WactorzDB) -> None:
        _route(agent, "sensors/a", {"t": 1})

        result = await NS["handle_task"](agent, {"action": "stats"})

        assert result["buffer_sizes"] == {"sensor": 1, "detection": 0, "ha": 0}
        assert "Retention: 90.0 days" in result["result"]

    async def test_prune_and_flush(self, agent: _Agent, db: WactorzDB) -> None:
        _old_reading(db, 120)
        _route(agent, "sensors/a", {"t": 1})

        pruned = await NS["handle_task"](agent, {"action": "prune"})
        flushed = await NS["handle_task"](agent, {"action": "flush"})

        assert pruned["pruned_rows"] == 1
        assert flushed["total_written"] == 1
        assert db.stats()["sensor_readings"] == 1

    @pytest.mark.parametrize("table", ["sensors", "ha_states", "detections", "actuations"])
    async def test_every_table_can_be_queried(
        self, table: str, agent: _Agent, db: WactorzDB
    ) -> None:
        _route(agent, "sensors/a", {"entity_id": "sensor.a", "t": 1})
        _route(agent, "homeassistant/state_changes/x", {"entity_id": "sensor.a", "new_state": "1"})
        _route(agent, "custom/detection", {"class": "cat"})

        result = await NS["handle_task"](
            agent, {"action": "query", "table": table, "hours": 1, "limit": 99999}
        )

        expected = {"sensors": 1, "ha_states": 1, "detections": 1, "actuations": 0}[table]
        assert result["count"] == expected
        assert result["table"] == table

    async def test_an_unknown_table_or_no_database(
        self, agent: _Agent, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unknown = await NS["handle_task"](agent, {"action": "query", "table": "weather"})
        monkeypatch.setitem(NS, "get_db", lambda: None)
        nothing = await NS["handle_task"](agent, {"action": "query"})

        assert "Unknown table 'weather'" in unknown["result"]
        assert nothing["rows"] == []

    async def test_configure_sets_persists_and_applies_the_window(
        self, agent: _Agent, db: WactorzDB
    ) -> None:
        _old_reading(db, 3)

        result = await NS["handle_task"](
            agent, {"action": "configure", "retention_days": 1, "prune_interval_hours": 0.5}
        )

        assert result["pruned_rows"] == 1
        assert agent.stored["retention_days"] == 1.0
        assert agent.stored["prune_interval_hours"] == 0.5
        assert agent.state["prune_interval_s"] == 1800

    @pytest.mark.parametrize(
        ("fields", "said"),
        [
            ({}, "Nothing to configure"),
            ({"retention_days": 0}, "retention_days must be > 0"),
            ({"prune_interval_hours": -1}, "prune_interval_hours must be > 0"),
            ({"retention_days": "a week"}, "retention_days must be a number"),
            ({"prune_interval_hours": "often"}, "prune_interval_hours must be a number"),
        ],
    )
    async def test_configure_refuses_what_it_cannot_use(
        self, fields: dict[str, Any], said: str, agent: _Agent, db: WactorzDB
    ) -> None:
        result = await NS["handle_task"](agent, {"action": "configure", **fields})

        assert said in result["result"]
        assert agent.state["retention_days"] == 90

    async def test_a_query_that_is_not_a_number_is_refused(
        self, agent: _Agent, db: WactorzDB
    ) -> None:
        result = await NS["handle_task"](agent, {"action": "query", "hours": "lately"})

        assert "must be a number" in result["result"]

    async def test_storage_on_demand(self, agent: _Agent, db: WactorzDB) -> None:
        result = await NS["handle_task"](agent, {"action": "storage"})

        assert "Database size:" in result["result"]
        assert agent.published[0][0] == "agents/ts-1/storage"

    async def test_a_command_sent_as_json_text_or_a_bare_string(
        self, agent: _Agent, db: WactorzDB
    ) -> None:
        as_json = await NS["handle_task"](agent, {"text": '{"action": "flush"}'})
        as_string = await NS["handle_task"](agent, "stats")

        assert as_json["result"] == "Buffers flushed to SQLite."
        assert "total_received" in as_string


class TestPlainWords:
    @pytest.mark.parametrize(
        ("text", "days"),
        [
            ("keep only 2 days of data", 2.0),
            ("retain 12 hours", 0.5),
            ("store 2 weeks", 14.0),
            ("hold 3 months please", 90.0),
        ],
    )
    async def test_a_retention_in_words(
        self, text: str, days: float, agent: _Agent, db: WactorzDB
    ) -> None:
        await NS["handle_task"](agent, {"text": text})

        assert agent.state["retention_days"] == days

    async def test_storage_in_words(self, agent: _Agent, db: WactorzDB) -> None:
        result = await NS["handle_task"](agent, {"text": "how much disk are you using?"})

        assert "Database size:" in result["result"]

    @pytest.mark.parametrize(
        ("text", "hours"),
        [
            ("give me the last 6 hours of data", 6.0),
            ("send the history for 2 days", 48.0),
            ("get me the readings", 24.0),
        ],
    )
    async def test_a_query_in_words(
        self, text: str, hours: float, agent: _Agent, db: WactorzDB
    ) -> None:
        result = await NS["handle_task"](agent, {"text": text})

        assert result["hours"] == hours
        assert result["table"] == "ha_states"

    async def test_anything_else_lists_what_it_can_do(self, agent: _Agent, db: WactorzDB) -> None:
        result = await NS["handle_task"](agent, {"text": "tell me a joke"})

        assert result["commands"] == ["stats", "prune", "flush", "query", "configure", "storage"]
