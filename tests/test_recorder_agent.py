"""The recorder keeps the readings of streams as Parquet, one source per device or topic.

A library agent the package brings, spawned from the catalogue on main or on a
node with the topics it records. Each reading is a row of its source in event
time; the source is what the payload names, else the topic without its
wildcards. ``docs/design/data-storage.md`` is the decision these pin.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from wactorz import plugins
from wactorz.agents.recorder_agent import RecorderAgent, source_from_topic, topics_of
from wactorz.core.actor import Message, MessageType
from wactorz.core.paths import resolve_state_dir
from wactorz.data import data_root, sources
from wactorz.data.parquet import frame

T0 = 1_760_000_000.0


@pytest.fixture(name="published")
def published_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        seen.append((topic, payload))

    monkeypatch.setattr(RecorderAgent, "_mqtt_publish", record)
    return seen


@pytest.fixture(name="recorder")
async def recorder_fixture(
    tmp_path: Path, published: list[tuple[str, Any]]
) -> AsyncIterator[RecorderAgent]:
    agent = RecorderAgent(
        name="recorder",
        persistence_dir=str(tmp_path / "agents"),
        topics=["sensors/imu/#", "sensors/hvac/ahu-1"],
        batch_rows=2,
    )
    yield agent
    await agent.flush()


def _rows(source: str) -> list[dict[str, Any]]:
    return frame(data_root(resolve_state_dir()), source, 0, 4_000_000_000).to_pylist()


class TestNaming:
    def test_a_filter_without_its_wildcards_is_a_source(self) -> None:
        assert source_from_topic("sensors/imu/#") == "sensors/imu"
        assert source_from_topic("sensors/+/temp") == "sensors"
        assert source_from_topic("sensors/hvac/ahu-1") == "sensors/hvac/ahu-1"

    def test_topics_come_as_one_string_or_several(self) -> None:
        assert topics_of("sensors/#, custom/x") == ("sensors/#", "custom/x")
        assert topics_of(["a", " b "]) == ("a", "b")
        assert topics_of(None) == ()

    def test_the_payloads_device_names_the_source(self, recorder: RecorderAgent) -> None:
        assert recorder.source_of({"device": "left-wrist"}, "sensors/imu/#") == "left-wrist"
        assert recorder.source_of({"x": 1}, "sensors/imu/#") == "sensors/imu"
        recorder.source_field = "unit"
        assert recorder.source_of({"device": "d", "unit": "ahu-1"}, "t") == "ahu-1"


class TestRecording:
    async def test_a_reading_is_a_row_of_its_source_in_event_time(
        self, recorder: RecorderAgent
    ) -> None:
        assert await recorder.record({"device": "left-wrist", "ts": T0, "ax": 1}, "sensors/imu/#")
        assert await recorder.record(
            {"device": "left-wrist", "ts": T0 + 1, "ax": 2}, "sensors/imu/#"
        )
        await recorder.flush()

        rows = _rows("left-wrist")

        assert [(r["ts"], r["ax"], r["time_source"]) for r in rows] == [
            (T0, 1.0, "payload"),
            (T0 + 1, 2.0, "payload"),
        ]
        assert rows[0]["arrived"] >= T0
        assert recorder.recorded == 2

    async def test_a_reading_without_a_time_is_filed_at_its_arrival(
        self, recorder: RecorderAgent
    ) -> None:
        await recorder.record({"temp": 21.5}, "sensors/hvac/ahu-1")
        await recorder.flush()

        (row,) = _rows("sensors/hvac/ahu-1")

        assert (row["time_source"], row["temp"]) == ("arrival", 21.5)
        assert row["ts"] == row["arrived"]

    async def test_what_is_not_a_reading_is_refused_and_counted(
        self, recorder: RecorderAgent
    ) -> None:
        assert not await recorder.record("just text", "sensors/imu/#")
        assert not await recorder.record({"device": "../x", "ts": T0}, "sensors/imu/#")

        assert recorder.refused == 2
        assert recorder.recorded == 0

    async def test_a_full_batch_reaches_the_disk_without_a_flush(
        self, recorder: RecorderAgent
    ) -> None:
        for i in range(2):
            await recorder.record({"device": "d", "ts": T0 + i}, "sensors/imu/#")

        assert len(_rows("d")) == 2
        assert sources(data_root(resolve_state_dir())) == ["d"]

    async def test_a_task_says_what_was_recorded(
        self, recorder: RecorderAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[tuple[str, Any, Any]] = []

        async def send(target: str, kind: Any, payload: Any) -> None:
            sent.append((target, kind, payload))

        monkeypatch.setattr(recorder, "send", send)
        await recorder.record({"device": "d", "ts": T0}, "sensors/imu/#")

        await recorder.handle_message(
            Message(
                sender_id="main",
                type=MessageType.TASK,
                payload={"action": "flush", "_task_id": "t"},
            )
        )

        ((target, kind, reply),) = sent
        assert (target, kind) == ("main", MessageType.RESULT)
        assert (reply["recorded"], reply["pending"], reply["sources"], reply["_task_id"]) == (
            1,
            0,
            ["d"],
            "t",
        )


class TestTheCatalogue:
    def test_the_recorder_is_a_built_in_plugin_that_waits_to_be_asked_for(self) -> None:
        plugin = plugins.built_in()["recorder"]

        assert plugin.kind == "actor"
        assert plugin.autostart is False
        assert "topics" in plugin.input_schema

    def test_it_is_built_with_its_topics_as_options(self, tmp_path: Path) -> None:
        plugin = plugins.built_in()["recorder"]

        built = plugin.build(
            name="recorder",
            persistence_dir=str(tmp_path),
            options={"topics": "sensors/#", "source_field": "device"},
        )

        assert isinstance(built, RecorderAgent)
        assert (built.topics, built.source_field) == (("sensors/#",), "device")
