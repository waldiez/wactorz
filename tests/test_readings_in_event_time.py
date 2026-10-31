"""A reading is kept under when it happened, with when it arrived beside it.

The collector files each row under the payload's event time and keeps the
arrival and where the time came from in two columns a migration adds; a row
written before has the arrival in ``ts`` and nothing in them, which reads as
before. A stream window asked for event time orders and trims by what the
payloads say, with the window ending at the newest entry, for a replay.
``docs/design/event-time.md`` is the decision these pin.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.programs import program_namespace
from wactorz.core.persistence.db import WactorzDB
from wactorz.core.persistence.migrations import READING_TIME_COLUMNS, migrate_sql_7
from wactorz.core.topic_bus import StreamWindow

NS = program_namespace("timeseries_collector_agent.py")
T0 = 1_760_000_000.0


class _Agent:
    """What `_route_message` uses of the collector's ``agent``: its buffers."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "sensor_buffer": [],
            "detection_buffer": [],
            "ha_buffer": [],
        }


def _route(agent: _Agent, topic: str, payload: Any) -> None:
    NS["_route_message"](agent, topic, payload)


class TestTheCollector:
    def test_a_reading_is_filed_under_its_event_time(self) -> None:
        agent = _Agent()

        _route(agent, "sensors/a", {"t": 21.5, "ts": T0, "entity_id": "sensor.a"})

        (row,) = agent.state["sensor_buffer"]
        ts, topic, entity, field, value = row[:5]
        arrived, time_source = row[-2:]
        assert (ts, topic, entity, field, value) == (T0, "sensors/a", "sensor.a", "t", 21.5)
        assert (time_source, len(row)) == ("payload", 11)
        assert arrived > T0

    def test_a_reading_without_a_time_is_filed_at_its_arrival(self) -> None:
        agent = _Agent()

        _route(agent, "sensors/a", {"t": 1})

        (row,) = agent.state["sensor_buffer"]
        assert row[0] == row[-2]
        assert row[-1] == "arrival"

    def test_the_rows_fit_the_table(self, tmp_path: Path) -> None:
        agent = _Agent()
        _route(agent, "sensors/a", {"t": 1, "h": 2, "ts": T0})
        with WactorzDB(tmp_path / "wactorz.db") as db:
            db.write_sensor_batch(agent.state["sensor_buffer"])
            # A row of the older shape is still taken, with the two columns empty.
            db.write_sensor_batch([(T0 + 1, "sensors/b", "", "t", 3.0, "", "", "", "")])

            rows = db.conn.execute(
                "SELECT ts, field, arrived, time_source FROM sensor_readings ORDER BY ts, field"
            ).fetchall()

        assert [(r[0], r[1], r[3]) for r in rows] == [
            (T0, "h", "payload"),
            (T0, "t", "payload"),
            (T0 + 1, "t", ""),
        ]
        assert rows[0][2] is not None and rows[2][2] is None


class TestTheMigration:
    def test_an_older_table_gains_the_two_columns_once(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "old.db")
        conn.execute(
            "CREATE TABLE sensor_readings (ts REAL NOT NULL, topic TEXT NOT NULL, entity_id TEXT, "
            "field TEXT NOT NULL, value REAL, value_str TEXT, unit TEXT, agent TEXT, node TEXT)"
        )
        conn.execute(
            "INSERT INTO sensor_readings VALUES (?, 'sensors/a', '', 't', 1.0, '', '', '', '')",
            (T0,),
        )

        migrate_sql_7(conn)
        migrate_sql_7(conn)

        columns = [row[1] for row in conn.execute("PRAGMA table_info(sensor_readings)")]
        assert columns[-2:] == [name for name, _ in READING_TIME_COLUMNS]
        assert conn.execute("SELECT ts, arrived, time_source FROM sensor_readings").fetchone() == (
            T0,
            None,
            "",
        )
        conn.close()

    def test_a_new_database_has_them(self, tmp_path: Path) -> None:
        with WactorzDB(tmp_path / "new.db") as db:
            columns = [row[1] for row in db.conn.execute("PRAGMA table_info(sensor_readings)")]

        assert "arrived" in columns and "time_source" in columns


class TestTheWindowInEventTime:
    def test_entries_are_timed_by_the_payload_and_the_window_ends_at_the_newest(self) -> None:
        window = StreamWindow("sensors/a", seconds=60, event_time=True)
        for offset, value in ((0, 1), (50, 2), (100, 3)):
            window.push({"ts": T0 + offset, "value": value})

        # The window ends at T0 + 100, so it holds the last sixty seconds before that.
        assert window.values() == [2, 3]
        assert window.latest("value") == 3
        assert window.absent_for(10) is False
        assert window.event_count(seconds=30) == 1

    def test_a_payload_without_a_time_is_timed_by_its_arrival(self) -> None:
        window = StreamWindow("sensors/a", seconds=60, event_time=True)
        window.push({"value": 1})

        assert window.count() == 1
        assert window.latest()["_ts"] == pytest.approx(__import__("time").time(), abs=5)

    def test_the_wall_clock_stays_the_default(self) -> None:
        window = StreamWindow("sensors/a", seconds=60)
        window.push({"ts": T0, "value": 1})

        assert window.values() == [1]
        assert window.latest()["_ts"] != T0
