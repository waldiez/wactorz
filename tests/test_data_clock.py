# SPDX-License-Identifier: Apache-2.0
"""Now, per source: the newest event time a source has seen, held back by an allowed lateness.

Where the model loop needs "now" it reads the source's own time, so a replay
is checked at its own moments and a live run at the wall clock less a minute;
a source nothing is recorded for answers with the wall clock.
``docs/design/event-time.md`` is the decision these pin.
"""

from pathlib import Path

import pytest

from wactorz.data import (
    DEFAULT_LATENESS_S,
    FUTURE_ALLOWANCE_S,
    data_root,
    newest_event_time,
    now_of,
)
from wactorz.data import clock as data_clock
from wactorz.data.parquet import Writer

#: 2025-10-09 08:53:20 UTC, inside hour 08.
T0 = 1_760_000_000.0
HOUR = 3600.0
SOURCE = "sensors/ahu-1"


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    return data_root(tmp_path / "state")


def _record(root: Path, source: str, times: list[float], *, batch_rows: int = 1000) -> None:
    writer = Writer(root, source, batch_rows=batch_rows)
    for ts in times:
        writer.append({"ts": ts, "x": 1.0})
    writer.close()


class TestTheNewestEventTime:
    def test_nothing_recorded_is_none(self, root: Path) -> None:
        assert newest_event_time(root, SOURCE) is None
        assert newest_event_time(root, "sensors/nobody") is None

    def test_the_newest_hours_newest_row_whatever_order_it_was_written_in(self, root: Path) -> None:
        _record(root, SOURCE, [T0 + 2 * HOUR, T0, T0 + 2 * HOUR + 30, T0 + HOUR])

        assert newest_event_time(root, SOURCE) == T0 + 2 * HOUR + 30

    def test_every_part_of_the_newest_hour_is_read(self, root: Path) -> None:
        _record(root, SOURCE, [T0, T0 + 10, T0 + 20, T0 + 5], batch_rows=2)

        assert newest_event_time(root, SOURCE) == T0 + 20

    def test_a_source_that_cannot_be_named_is_none(self, root: Path) -> None:
        assert newest_event_time(root, "../elsewhere") is None


class TestNow:
    def test_it_is_the_newest_reading_held_back(self, root: Path) -> None:
        _record(root, SOURCE, [T0, T0 + 300])

        assert now_of(root, SOURCE) == T0 + 300 - DEFAULT_LATENESS_S
        assert now_of(root, SOURCE, lateness=0) == T0 + 300
        assert now_of(root, SOURCE, lateness=-5) == T0 + 300

    def test_nothing_recorded_is_the_wall_clock_or_the_fallback_given(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(data_clock.time, "time", lambda: T0 + 9 * HOUR)

        assert now_of(root, SOURCE) == T0 + 9 * HOUR
        assert now_of(root, SOURCE, fallback=T0) == T0
        assert now_of(root, "") == T0 + 9 * HOUR


class TestATimeAheadOfTheClock:
    """A file written before readings ahead of the clock were filed at arrival may hold one."""

    @pytest.fixture(autouse=True)
    def _clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(data_clock.time, "time", lambda: T0 + 10 * HOUR)
        monkeypatch.setattr(data_clock, "_ahead_said", set())

    def test_now_passes_over_an_hour_ahead_of_the_clock(self, root: Path) -> None:
        year_2100 = 4_102_444_800.0
        _record(root, SOURCE, [T0, T0 + 300, year_2100])

        assert now_of(root, SOURCE, lateness=0) == T0 + 300

    def test_now_passes_over_a_reading_ahead_inside_the_newest_hour(self, root: Path) -> None:
        clock = T0 + 10 * HOUR
        _record(root, SOURCE, [clock - 60, clock + FUTURE_ALLOWANCE_S + 60])

        assert now_of(root, SOURCE, lateness=0) == clock - 60

    def test_a_reading_ahead_within_the_allowance_is_now(self, root: Path) -> None:
        clock = T0 + 10 * HOUR
        _record(root, SOURCE, [clock - 60, clock + FUTURE_ALLOWANCE_S - 1])

        assert now_of(root, SOURCE, lateness=0) == clock + FUTURE_ALLOWANCE_S - 1

    def test_only_times_ahead_is_the_wall_clock(self, root: Path) -> None:
        _record(root, SOURCE, [4_102_444_800.0])

        assert now_of(root, SOURCE) == T0 + 10 * HOUR

    def test_it_is_said_once_per_source(self, root: Path, caplog: pytest.LogCaptureFixture) -> None:
        _record(root, SOURCE, [T0, 4_102_444_800.0])

        with caplog.at_level("WARNING", logger=data_clock.__name__):
            now_of(root, SOURCE)
            now_of(root, SOURCE)

        said = [r for r in caplog.records if SOURCE in r.getMessage()]
        assert len(said) == 1
        assert "ahead of this machine's clock" in said[0].getMessage()

    def test_without_a_bound_the_newest_time_is_whatever_was_filed(self, root: Path) -> None:
        _record(root, SOURCE, [T0, 4_102_444_800.0])

        assert newest_event_time(root, SOURCE) == 4_102_444_800.0
        assert newest_event_time(root, SOURCE, latest=T0 + HOUR) == T0
