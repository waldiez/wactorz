"""The sliding window agent code queries for recent readings.

Every accessor trims first, so age is enforced on read rather than on a timer.
Ageing is driven here by writing `_ts` directly instead of sleeping — a window
measured in minutes cannot be waited out.
"""

from __future__ import annotations

import time

import pytest

from wactorz.core.topic_bus import StreamWindow


def age(window: StreamWindow, index: int, seconds: float) -> None:
    """Backdate one buffered entry by `seconds`."""
    window._buffer[index]["_ts"] = time.time() - seconds


@pytest.fixture(name="window")
def window_fixture() -> StreamWindow:
    return StreamWindow("sensors/temp", seconds=60, max_size=5)


class TestPush:
    def test_a_dict_payload_keeps_its_fields(self, window: StreamWindow) -> None:
        window.push({"value": 21.5, "unit": "C"})
        latest = window.latest()
        assert latest is not None
        assert latest["value"] == 21.5
        assert latest["unit"] == "C"

    def test_a_scalar_payload_is_stored_under_value(self, window: StreamWindow) -> None:
        window.push(42)
        assert window.values() == [42]

    def test_every_entry_is_timestamped(self, window: StreamWindow) -> None:
        window.push(1)
        latest = window.latest()
        assert latest is not None
        assert "_ts" in latest

    def test_max_size_bounds_the_buffer(self, window: StreamWindow) -> None:
        """The deque is the memory bound; the time window is the freshness one."""
        for i in range(20):
            window.push(i)
        assert window.count() == 5
        assert window.values() == [15, 16, 17, 18, 19]


class TestAgeing:
    def test_entries_older_than_the_window_are_dropped_on_read(self, window: StreamWindow) -> None:
        window.push(1)
        window.push(2)
        age(window, 0, 120)
        assert window.values() == [2]

    def test_an_entirely_stale_window_reads_empty(self, window: StreamWindow) -> None:
        window.push(1)
        age(window, 0, 120)
        assert window.count() == 0
        assert window.latest() is None
        assert not window.values()


class TestStatistics:
    def test_mean_min_max_over_the_window(self, window: StreamWindow) -> None:
        for v in (10, 20, 30):
            window.push(v)
        assert window.mean() == 20
        assert window.min() == 10
        assert window.max() == 30

    def test_statistics_are_none_on_an_empty_window(self, window: StreamWindow) -> None:
        assert window.mean() is None
        assert window.min() is None
        assert window.max() is None

    def test_a_key_no_entry_carries_is_simply_absent(self, window: StreamWindow) -> None:
        window.push({"value": 1})
        assert not window.values("humidity")
        assert window.mean("humidity") is None

    def test_values_reads_the_named_key(self, window: StreamWindow) -> None:
        window.push({"value": 1, "humidity": 55})
        window.push({"value": 2, "humidity": 60})
        assert window.values("humidity") == [55, 60]


class TestTrendPredicates:
    """First-to-last comparisons, not a fitted slope."""

    def test_rising_compares_the_ends(self, window: StreamWindow) -> None:
        for v in (10, 5, 30):
            window.push(v)
        assert window.rising(threshold=5)
        assert not window.falling(threshold=5)

    def test_falling_compares_the_ends(self, window: StreamWindow) -> None:
        for v in (30, 50, 10):
            window.push(v)
        assert window.falling(threshold=5)
        assert not window.rising(threshold=5)

    def test_a_move_smaller_than_the_threshold_is_neither(self, window: StreamWindow) -> None:
        window.push(10)
        window.push(10.5)
        assert not window.rising(threshold=5)
        assert not window.falling(threshold=5)

    def test_one_reading_is_not_a_trend(self, window: StreamWindow) -> None:
        window.push(10)
        assert not window.rising()
        assert not window.falling()

    def test_a_spike_between_the_ends_does_not_register(self, window: StreamWindow) -> None:
        """Only the first and last readings are compared, so an excursion that
        returns to where it started reads as no movement at all.
        """
        for v in (10, 900, 10):
            window.push(v)
        assert not window.rising(threshold=5)
        assert not window.falling(threshold=5)

    def test_stable_uses_the_spread_so_a_spike_does_register(self, window: StreamWindow) -> None:
        for v in (10, 900, 10):
            window.push(v)
        assert not window.stable(tolerance=1)

    def test_stable_within_tolerance(self, window: StreamWindow) -> None:
        for v in (10, 10.2, 10.1):
            window.push(v)
        assert window.stable(tolerance=0.5)

    def test_too_little_data_counts_as_stable(self, window: StreamWindow) -> None:
        """Nothing has been seen to move, which is the safe answer for an
        alarm predicate — the opposite default would fire on the first reading.
        """
        assert window.stable()
        window.push(10)
        assert window.stable()


class TestAbsence:
    def test_an_empty_window_is_absent(self, window: StreamWindow) -> None:
        assert window.absent_for(10)

    def test_a_fresh_reading_is_not_absent(self, window: StreamWindow) -> None:
        window.push(1)
        assert not window.absent_for(10)

    def test_absence_is_measured_from_the_newest_reading(self, window: StreamWindow) -> None:
        window.push(1)
        age(window, 0, 30)
        assert window.absent_for(10)
        assert not window.absent_for(45)


class TestEventCount:
    def test_counts_everything_by_default(self, window: StreamWindow) -> None:
        for _ in range(3):
            window.push({"kind": "motion"})
        assert window.event_count() == 3

    def test_filters_on_a_key_and_value(self, window: StreamWindow) -> None:
        window.push({"kind": "motion"})
        window.push({"kind": "door"})
        window.push({"kind": "motion"})
        assert window.event_count(key="kind", value="motion") == 2

    def test_a_key_with_no_value_counts_entries_carrying_it(self, window: StreamWindow) -> None:
        window.push({"kind": "motion"})
        window.push({"other": 1})
        assert window.event_count(key="kind") == 1

    def test_a_shorter_horizon_than_the_window(self, window: StreamWindow) -> None:
        window.push({"kind": "motion"})
        window.push({"kind": "motion"})
        age(window, 0, 30)
        assert window.event_count(seconds=10) == 1
        assert window.event_count(seconds=45) == 2


class TestListenerLifecycle:
    def test_stop_is_safe_before_start(self, window: StreamWindow) -> None:
        """Agent code may stop a window it never started."""
        window.stop()
        assert window._task is None
