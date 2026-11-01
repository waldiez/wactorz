"""Readings are kept as Parquet files, one directory per source and hour, and read back as a frame.

A file is written whole and never appended to; a batch that closes inside the
hour is a further part of it; a reader takes every part of every hour a window
overlaps, unifies their columns, and keeps the rows inside the window, oldest
first. ``docs/design/data-storage.md`` is the decision these pin.
"""

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from wactorz.data import DataError, data_root, event_time_of, files_for, hour_dir
from wactorz.data.files import FROM_ARRIVAL, FROM_PAYLOAD, check_source, hour_start
from wactorz.data.parquet import EMPTY_SCHEMA, Writer, frame, normalise

#: 2025-10-09 08:53:20 UTC, inside hour 08.
T0 = 1_760_000_000.0
HOUR = 3600.0


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    return data_root(tmp_path / "state")


def reading(ts: float, **fields: object) -> dict[str, object]:
    return {"ts": ts, "arrived": ts + 0.5, "time_source": FROM_PAYLOAD, **fields}


class TestTheLayout:
    def test_an_hour_is_a_directory_under_the_source(self, root: Path) -> None:
        assert hour_dir(root, "sensors/imu/left", T0) == root / "sensors/imu/left/2025/10/09"
        assert hour_start(T0) == T0 - (T0 % HOUR)

    @pytest.mark.parametrize("bad", ["", "a//b", "../x", "a/./b", "/a", "a b", "shadow/..", "-x"])
    def test_a_source_cannot_walk_out_of_the_data_directory(self, bad: str) -> None:
        with pytest.raises(DataError):
            check_source(bad)

    def test_a_source_may_name_a_model_or_a_device(self) -> None:
        for good in ("sensors/imu/left-wrist", "shadow/imu-detector", "serving/m.1", "a@node"):
            assert check_source(good) == good


class TestEventTime:
    def test_the_payloads_time_is_taken_and_said_so(self) -> None:
        assert event_time_of({"ts": T0, "x": 1}, arrived=T0 + 9) == (T0, FROM_PAYLOAD)
        assert event_time_of({"timestamp": "2025-10-09T08:53:20+00:00"}, arrived=1) == (
            T0,
            FROM_PAYLOAD,
        )

    def test_without_one_the_arrival_is_taken_and_marked(self) -> None:
        assert event_time_of({"x": 1}, arrived=T0 + 9) == (T0 + 9, FROM_ARRIVAL)
        assert event_time_of("not a mapping", arrived=T0) == (T0, FROM_ARRIVAL)
        assert event_time_of({"ts": "soon"}, arrived=T0) == (T0, FROM_ARRIVAL)


class TestTheWriter:
    def test_an_hour_is_written_when_a_newer_hour_arrives(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu", batch_rows=100)

        assert writer.append(reading(T0, ax=1)) == []
        assert writer.append(reading(T0 + 60, ax=2)) == []
        made = writer.append(reading(T0 + HOUR, ax=3))

        assert [p.relative_to(root).as_posix() for p in made] == [
            "sensors/imu/2025/10/09/08.parquet"
        ]
        assert pq.read_table(made[0]).column("ax").to_pylist() == [1.0, 2.0]
        assert writer.pending == 1

    def test_a_full_batch_is_a_further_part_of_the_hour(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu", batch_rows=2)

        first = writer.append(reading(T0, ax=1)) + writer.append(reading(T0 + 1, ax=2))
        second = writer.append(reading(T0 + 2, ax=3)) + writer.append(reading(T0 + 3, ax=4))

        assert [p.name for p in first + second] == ["08.parquet", "08-0001.parquet"]
        assert files_for(root, "sensors/imu", T0, T0 + 10) == first + second

    def test_flush_writes_what_is_pending_whatever_hour_it_is_in(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu")
        writer.append(reading(T0, ax=1))
        writer.append(reading(T0 + HOUR, ax=2))
        writer.append(reading(T0 + 2 * HOUR, ax=3))
        assert writer.pending == 1

        made = writer.flush()

        assert [p.name for p in made] == ["10.parquet"]
        assert [p.name for p in writer.written] == ["08.parquet", "09.parquet", "10.parquet"]
        assert writer.pending == 0
        assert writer.close() == []

    def test_a_file_is_whole_or_absent(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu")
        writer.append(reading(T0, ax=1))
        (made,) = writer.flush()

        assert not list(made.parent.glob("*.tmp"))

    def test_rows_are_filed_as_the_decision_says(self) -> None:
        kept = normalise({"ts": T0, "n": 3, "f": 2.5, "b": True, "s": "x", "nested": {"a": [1]}})

        assert kept == {"ts": T0, "n": 3.0, "f": 2.5, "b": True, "s": "x", "nested": '{"a":[1]}'}
        with pytest.raises(DataError, match="no event time"):
            normalise({"x": 1})


class TestTheFrame:
    def test_a_window_takes_the_rows_inside_it_oldest_first(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu")
        for offset in (HOUR, 10, 0, 2 * HOUR):
            writer.append(reading(T0 + offset, ax=offset))
        writer.flush()

        table = frame(root, "sensors/imu", T0 + 5, T0 + 2 * HOUR)

        assert table.column("ax").to_pylist() == [10.0, HOUR]
        assert table.column("time_source").to_pylist() == [FROM_PAYLOAD, FROM_PAYLOAD]

    def test_columns_a_later_file_added_are_null_in_the_earlier_rows(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu")
        writer.append(reading(T0, ax=1))
        writer.flush()
        writer.append(reading(T0 + 1, ax=2, ay=9))
        writer.flush()

        table = frame(root, "sensors/imu", T0, T0 + 10)

        assert table.column("ay").to_pylist() == [None, 9.0]
        narrowed = frame(root, "sensors/imu", T0, T0 + 10, columns=["ay"])
        assert narrowed.column_names == ["ts", "ay"]

    def test_a_window_with_nothing_is_an_empty_frame_with_the_fixed_columns(
        self, root: Path
    ) -> None:
        table = frame(root, "sensors/none", T0, T0 + 10)

        assert table.num_rows == 0
        assert table.schema == EMPTY_SCHEMA
        assert files_for(root, "sensors/none", T0, T0 + 10) == []

    def test_only_the_hours_the_window_overlaps_are_read(self, root: Path) -> None:
        writer = Writer(root, "sensors/imu")
        for offset in (0, HOUR, 2 * HOUR, 25 * HOUR):
            writer.append(reading(T0 + offset, ax=offset))
        writer.flush()

        names = [p.name for p in files_for(root, "sensors/imu", T0 + HOUR, T0 + 2 * HOUR + 1)]

        assert names == ["09.parquet", "10.parquet"]
