"""Working out when a scheduled agent should next fire.

These are pure functions over a supplied "now", so every case pins a real clock
arithmetic answer rather than waiting for one. The boundary cases are the point:
a schedule whose time has just passed must roll to the next occurrence, not fire
immediately and then again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wactorz.agents.scheduled_agent import (
    _next_fire_daily,
    _next_fire_interval,
    _next_fire_once,
    _next_fire_weekly,
    _parse_duration,
    _parse_hhmm,
    _resolve_timezone,
    describe_schedule,
)

# A Wednesday, so weekday arithmetic has somewhere to go in both directions.
WED_NOON = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


class TestParseHhmm:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("09:30", (9, 30)), ("00:00", (0, 0)), ("23:59", (23, 59)), (" 7:05 ", (7, 5))],
    )
    def test_accepted_forms(self, text: str, expected: tuple[int, int]) -> None:
        assert _parse_hhmm(text) == expected

    def test_seconds_are_accepted_and_dropped(self) -> None:
        assert _parse_hhmm("09:30:45") == (9, 30)

    @pytest.mark.parametrize("text", ["9", "", "24:00", "12:60", "-1:00"])
    def test_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            _parse_hhmm(text)

    def test_a_non_numeric_time_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _parse_hhmm("noon:30")


class TestDaily:
    def test_later_today(self) -> None:
        assert _next_fire_daily(WED_NOON, "17:00") == WED_NOON.replace(hour=17, minute=0)

    def test_already_past_rolls_to_tomorrow(self) -> None:
        assert _next_fire_daily(WED_NOON, "09:00") == WED_NOON.replace(day=20, hour=9, minute=0)

    def test_exactly_now_rolls_rather_than_firing_twice(self) -> None:
        """`<=` not `<`: firing at the instant it is computed would fire again
        on the next tick, because the time still matches.
        """
        assert _next_fire_daily(WED_NOON, "12:00") == WED_NOON.replace(day=20)

    def test_seconds_are_zeroed(self) -> None:
        messy = WED_NOON.replace(second=37, microsecond=999)
        fired = _next_fire_daily(messy, "17:00")
        assert (fired.second, fired.microsecond) == (0, 0)


class TestWeekly:
    def test_later_the_same_day(self) -> None:
        assert _next_fire_weekly(WED_NOON, "17:00", ["wed"]) == WED_NOON.replace(hour=17)

    def test_past_today_rolls_a_full_week(self) -> None:
        assert _next_fire_weekly(WED_NOON, "09:00", ["wed"]) == WED_NOON.replace(day=26, hour=9)

    def test_the_soonest_of_several_days_wins(self) -> None:
        fired = _next_fire_weekly(WED_NOON, "09:00", ["mon", "fri"])
        assert fired.weekday() == 4  # Friday
        assert fired.day == 21

    def test_day_names_are_case_and_space_insensitive(self) -> None:
        assert _next_fire_weekly(WED_NOON, "17:00", [" WED "]).hour == 17

    def test_an_empty_day_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _next_fire_weekly(WED_NOON, "17:00", [])

    def test_an_unknown_day_is_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="Unknown day"):
            _next_fire_weekly(WED_NOON, "17:00", ["funday"])


class TestInterval:
    def test_without_a_previous_fire_it_counts_from_now(self) -> None:
        assert _next_fire_interval(WED_NOON, 300, None) == WED_NOON + timedelta(seconds=300)

    def test_with_a_previous_fire_it_counts_from_that(self) -> None:
        """Otherwise a slow tick would drift the schedule forward each time."""
        last = WED_NOON - timedelta(seconds=60)
        assert _next_fire_interval(WED_NOON, 300, last) == last + timedelta(seconds=300)

    def test_a_missed_window_catches_up_from_now_instead_of_the_past(self) -> None:
        """A long outage must not queue a backlog of overdue fires."""
        last = WED_NOON - timedelta(days=2)
        assert _next_fire_interval(WED_NOON, 300, last) == WED_NOON + timedelta(seconds=300)

    @pytest.mark.parametrize("seconds", [0, -1])
    def test_a_non_positive_interval_is_refused(self, seconds: int) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            _next_fire_interval(WED_NOON, seconds, None)


class TestOnce:
    def test_a_naive_time_is_read_in_the_schedule_timezone(self) -> None:
        fired = _next_fire_once("2026-05-02T17:00:00", timezone.utc)
        assert fired == datetime(2026, 5, 2, 17, 0, tzinfo=timezone.utc)

    def test_an_explicit_offset_is_kept(self) -> None:
        fired = _next_fire_once("2026-05-02T17:00:00+02:00", timezone.utc)
        assert fired.utcoffset() == timedelta(hours=2)

    def test_a_time_already_past_is_returned_as_given(self) -> None:
        """Deciding what to do about it belongs to the caller, not the parser."""
        assert _next_fire_once("2000-01-01T00:00:00", timezone.utc).year == 2000

    def test_a_non_iso_string_is_refused_with_the_expected_form(self) -> None:
        with pytest.raises(ValueError, match="ISO-8601"):
            _next_fire_once("next tuesday", timezone.utc)


class TestTimezoneResolution:
    def test_an_explicit_zone_wins(self) -> None:
        tz = _resolve_timezone("Europe/Athens", "America/New_York")
        assert "Athens" in str(tz)

    def test_the_user_zone_is_the_fallback(self) -> None:
        assert "New_York" in str(_resolve_timezone(None, "America/New_York"))

    def test_an_unknown_zone_falls_through_to_the_next_candidate(self) -> None:
        assert "Athens" in str(_resolve_timezone("Not/AZone", "Europe/Athens"))

    def test_with_nothing_supplied_it_still_returns_a_tzinfo(self) -> None:
        assert _resolve_timezone(None, None) is not None


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("90", 90), ("1.5h", 5400)],
    )
    def test_accepted_forms(self, text: str, seconds: int) -> None:
        assert _parse_duration(text) == seconds

    def test_case_and_spacing_are_tolerated(self) -> None:
        assert _parse_duration("  2H ") == 7200

    @pytest.mark.parametrize("text", ["", "soon", "5x", "m"])
    def test_unparseable_returns_none_rather_than_raising(self, text: str) -> None:
        """The caller treats None as "not a duration" and tries another form."""
        assert _parse_duration(text) is None


class TestDescribeSchedule:
    @pytest.mark.parametrize(
        ("schedule", "expected"),
        [
            ({"type": "daily", "at": "17:00"}, "every day at 17:00"),
            ({"type": "weekly", "days": ["mon", "fri"], "at": "08:00"}, "every mon, fri at 08:00"),
            ({"type": "interval", "seconds": 300}, "every 300 seconds"),
            ({"type": "once", "at": "2026-05-02T17:00"}, "once at 2026-05-02T17:00"),
            ({"type": "cron", "expr": "0 9 * * 1"}, "cron 0 9 * * 1"),
        ],
    )
    def test_each_kind_renders(self, schedule: dict, expected: str) -> None:
        assert describe_schedule(schedule) == expected

    def test_a_timezone_is_appended_where_it_is_meaningful(self) -> None:
        assert describe_schedule({"type": "daily", "at": "17:00"}, "Europe/Athens") == (
            "every day at 17:00 (Europe/Athens)"
        )

    def test_an_interval_carries_no_timezone(self) -> None:
        """A period has no wall-clock anchor, so a zone would be noise."""
        assert "Athens" not in describe_schedule(
            {"type": "interval", "seconds": 60}, "Europe/Athens"
        )

    def test_the_alternate_interval_key_is_accepted(self) -> None:
        assert describe_schedule({"type": "interval", "every_seconds": 60}) == "every 60 seconds"

    def test_missing_fields_render_a_placeholder_rather_than_raising(self) -> None:
        assert describe_schedule({"type": "daily"}) == "every day at ?"

    def test_an_unknown_type_renders_the_whole_dict(self) -> None:
        """So a malformed schedule is diagnosable from the rule listing itself."""
        assert "unknown schedule" in describe_schedule({"type": "yearly"})

    def test_a_schedule_with_no_type_is_unknown(self) -> None:
        assert "unknown schedule" in describe_schedule({})
