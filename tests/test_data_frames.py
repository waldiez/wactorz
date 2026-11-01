"""A frame is cut into segments, split without shuffling, labelled, described and exported.

Every split is a few row ranges and a manifest that says how it was made, so
a trainer stores it with the version and anyone replays it; a segment is never
cut by a split. ``docs/design/data-storage.md`` is the decision these pin.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from wactorz.core.labels import Interval, Label
from wactorz.data import sources
from wactorz.data.frames import (
    Fold,
    SplitError,
    export,
    leave_k_out,
    profile,
    rolling_origin,
    segments,
    take,
    time_split,
    with_labels,
)
from wactorz.data.parquet import Writer

T0 = 1_760_000_000.0


def _frame(ts: list[float], **columns: list) -> pa.Table:
    return pa.table({"ts": pa.array(ts, pa.float64()), **columns})


def _interval(start: float, end: float | None, value: str) -> Interval:
    label = Label(
        id=f"l-{value}-{start}",
        source="s",
        start=start,
        end=end,
        value=value,
        status="valid",
        actor="op",
        recorded=start,
    )
    return Interval(start=start, end=end, label=label)


class TestSegments:
    def test_a_gap_in_event_time_cuts(self) -> None:
        frame = _frame([T0, T0 + 1, T0 + 2, T0 + 100, T0 + 101, T0 + 300])

        found = segments(frame)

        assert [(s.start, s.end, s.key) for s in found] == [(0, 3, "0"), (3, 5, "1"), (5, 6, "2")]
        assert (found[0].ts_start, found[0].ts_end, found[0].rows) == (T0, T0 + 2, 3)
        assert [s.end for s in segments(frame, gap_s=500)] == [6]

    def test_a_labels_intervals_cut_and_leave_the_unlabelled_out(self) -> None:
        frame = _frame([T0 + i for i in range(6)])
        intervals = [_interval(T0, T0 + 2, "fault"), _interval(T0 + 4, None, "normal")]

        found = segments(frame, "label", intervals=intervals)

        assert [(s.start, s.end, s.key) for s in found] == [(0, 2, "fault"), (4, 6, "normal")]

    def test_a_column_changing_cuts(self) -> None:
        frame = _frame([T0 + i for i in range(5)], device=["a", "a", "b", "b", "a"])

        found = segments(frame, "column", column="device")

        assert [(s.start, s.end, s.key) for s in found] == [(0, 2, "a"), (2, 4, "b"), (4, 5, "a")]

    def test_a_fixed_length_cuts(self) -> None:
        frame = _frame([T0 + i * 10 for i in range(7)])

        found = segments(frame, "length", length_s=30)

        assert [(s.start, s.end) for s in found] == [(0, 3), (3, 6), (6, 7)]

    def test_an_empty_frame_has_no_segments_and_a_bad_cut_is_refused(self) -> None:
        assert segments(_frame([])) == []
        with pytest.raises(SplitError, match="not 'sideways'"):
            segments(_frame([T0]), "sideways")
        with pytest.raises(SplitError, match="needs intervals"):
            segments(_frame([T0]), "label")


class TestSplits:
    def test_a_time_split_trains_before_and_scores_from_the_moment(self) -> None:
        frame = _frame([T0 + i for i in range(10)], x=list(range(10)))

        fold = time_split(frame, T0 + 6)

        assert (fold.train, fold.test) == (((0, 6),), ((6, 10),))
        assert take(frame, fold.test).column("x").to_pylist() == [6, 7, 8, 9]
        assert fold.to_dict()["rows"] == {"train": 6, "test": 4}
        assert Fold.from_dict(fold.to_dict()) == fold
        with pytest.raises(SplitError, match="needs rows"):
            time_split(_frame([]), T0)

    def test_forward_folds_never_see_their_own_future(self) -> None:
        frame = _frame([T0 + i for i in range(10)])

        folds = rolling_origin(frame, 3, min_train=4)

        assert [(f.train, f.test) for f in folds] == [
            (((0, 4),), ((4, 6),)),
            (((0, 6),), ((6, 8),)),
            (((0, 8),), ((8, 10),)),
        ]
        assert folds[1].params == {"k": 3, "fold": 1, "min_train": 4}
        with pytest.raises(SplitError, match="forward folds need"):
            rolling_origin(_frame([T0, T0 + 1]), 3)

    def test_leave_k_out_holds_whole_segments_out(self) -> None:
        frame = _frame([T0, T0 + 1, T0 + 100, T0 + 101, T0 + 200])
        segs = segments(frame, gap_s=10)

        folds = leave_k_out(segs, 1)

        assert [f.test for f in folds] == [((0, 2),), ((2, 4),), ((4, 5),)]
        assert folds[0].train == ((2, 4), (4, 5))
        assert folds[0].params["held_out"] == ["0"]
        assert folds[0].params["seed"] is None

    def test_too_many_choices_are_sampled_with_a_seed_that_is_recorded(self) -> None:
        frame = _frame([T0 + i * 100 for i in range(8)])
        segs = segments(frame, gap_s=1)

        first = leave_k_out(segs, 2, seed=7, max_folds=5)
        again = leave_k_out(segs, 2, seed=7, max_folds=5)

        assert len(first) == 5
        assert [f.test for f in first] == [f.test for f in again]
        assert first[0].params["seed"] == 7
        with pytest.raises(SplitError, match="needs more than 8 segments"):
            leave_k_out(segs, 8)


class TestLabels:
    def test_each_row_gets_the_label_holding_at_its_time(self) -> None:
        frame = _frame([T0, T0 + 1, T0 + 2, T0 + 3])
        intervals = [_interval(T0 + 1, T0 + 3, "fault")]

        labelled = with_labels(frame, intervals)

        assert labelled.column("label").to_pylist() == [None, "fault", "fault", None]
        again = with_labels(labelled, [_interval(T0, None, "normal")])
        assert again.column("label").to_pylist() == ["normal"] * 4


class TestTheCard:
    def test_a_window_is_described(self) -> None:
        frame = _frame(
            [T0, T0 + 1, T0 + 2, T0 + 50],
            ax=[1.0, 2.0, None, 4.0],
            device=["a", "a", "b", "a"],
            ok=[True, False, True, True],
        )

        card = profile(frame, source="sensors/imu")

        assert (card["rows"], card["from"], card["to"], card["span_s"]) == (4, T0, T0 + 50, 50.0)
        assert card["rate_per_s"] == pytest.approx(4 / 50)
        assert card["gaps"] == {"count": 1, "longest_s": 48.0}
        ax = card["columns"]["ax"]
        assert (ax["type"], ax["count"], ax["missing"], ax["min"], ax["max"]) == (
            "number",
            3,
            1,
            1.0,
            4.0,
        )
        assert ax["quantiles"]["0.5"] == 2.0
        device = card["columns"]["device"]
        assert (device["type"], device["distinct"], device["top"][0]) == (
            "text",
            2,
            {"value": "a", "count": 3},
        )
        assert card["columns"]["ok"]["type"] == "bool"
        assert profile(_frame([]))["rows"] == 0


class TestExportAndSources:
    def test_a_frame_is_exported_as_one_file(self, tmp_path: Path) -> None:
        frame = _frame([T0, T0 + 1], x=[1, 2])

        path = export(frame, tmp_path / "out" / "imu.parquet")

        assert pq.read_table(path).column("x").to_pylist() == [1, 2]

    def test_the_sources_filed_are_listed(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        for source in ("sensors/imu/left", "shadow/imu", "a"):
            writer = Writer(root, source)
            writer.append({"ts": T0, "arrived": T0, "time_source": "payload"})
            writer.flush()
        (root / "empty").mkdir()

        assert sources(root) == ["a", "sensors/imu/left", "shadow/imu"]
        assert sources(tmp_path / "none") == []
