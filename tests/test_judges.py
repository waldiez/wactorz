"""A detector's answers judged against the labels: per input, per fault, per day, and how sure.

The statistics are checked against values worked out by hand or published
tables; the detector against small trials whose right answers can be counted.
"""

import json
import math
from typing import Any

import pytest

from wactorz.core import judges
from wactorz.core.judgement import KINDS, declared
from wactorz.core.judges import judge, mcc, mcnemar, sources_to_read, wilson
from wactorz.core.labels import Interval, Label

T0 = 1_760_000_000.0
DAY = 86400.0


def interval(start: float, end: float | None, value: str, source: str = "pump-1") -> Interval:
    label = Label(
        id=f"{source}-{start}",
        source=source,
        start=start,
        end=end,
        value=value,
        status="accepted",
        recorded=start,
        actor="operator",
    )
    return Interval(start=start, end=end, label=label)


def pair(ts: float, active: bool, shadow: bool, source: str = "pump-1") -> dict[str, Any]:
    """An input at ``ts`` and whether each side flagged it."""
    return {
        "ts": ts,
        "source": source,
        "active_output": json.dumps({"score": 9}) if active else None,
        "shadow_output": json.dumps({"score": 9}) if shadow else None,
    }


FAULTS = declared(positive=["fault"], by="cli", at=T0)


# ── Statistics ────────────────────────────────────────────────────────────────


class TestStatistics:
    def test_the_wilson_interval_matches_the_published_values(self) -> None:
        # Newcombe (1998), table I: 81 of 263 -> 0.2553 to 0.3662.
        low, high = wilson(81, 263) or (0, 0)

        assert (round(low, 4), round(high, 4)) == (0.2553, 0.3662)

    def test_a_perfect_score_from_few_is_a_wide_interval(self) -> None:
        low, high = wilson(3, 3) or (0, 0)

        assert high == 1.0 and low < 0.5

    def test_no_trials_no_interval(self) -> None:
        assert wilson(0, 0) is None

    def test_mcc_is_one_for_a_perfect_table_and_none_for_an_empty_column(self) -> None:
        assert mcc(5, 0, 0, 5) == 1.0
        assert mcc(0, 5, 5, 0) == -1.0
        assert mcc(5, 5, 0, 0) is None

    def test_mcnemar_exact_for_few(self) -> None:
        # 0 against 6: two-sided binomial p = 2 / 2**6.
        result = mcnemar(0, 6)

        assert result["p"] == pytest.approx(2 / 64)
        assert result["better"] == "shadow"

    def test_mcnemar_says_no_winner_when_it_could_be_chance(self) -> None:
        result = mcnemar(3, 5)

        assert result["p"] > 0.05 and result["better"] is None
        assert mcnemar(0, 0) == {"active_only": 0, "shadow_only": 0, "p": 1.0, "better": None}

    def test_mcnemar_for_many_uses_the_chi_square(self) -> None:
        # (|300-400| - 1)^2 / 700 = 14.0014...; p = erfc(sqrt(x / 2)).
        result = mcnemar(400, 300)

        assert result["p"] == pytest.approx(math.erfc(math.sqrt((99**2 / 700) / 2)))
        assert result["better"] == "active"


# ── The detector, per input ───────────────────────────────────────────────────


class TestPerInput:
    def test_each_side_is_counted_against_the_labels(self) -> None:
        labels = {"pump-1": [interval(T0, T0 + 4, "fault"), interval(T0 + 4, T0 + 8, "normal")]}
        rows = [
            pair(T0 + 0, True, True),  # fault: both caught it
            pair(T0 + 1, False, True),  # fault: only shadow
            pair(T0 + 5, True, False),  # normal: active false alarm
            pair(T0 + 6, False, False),  # normal: both right
        ]

        result = judge(rows, FAULTS, labels)

        active, shadow = result["active"], result["shadow"]
        assert (active["tp"], active["fp"], active["fn"], active["tn"]) == (1, 1, 1, 1)
        assert (shadow["tp"], shadow["fp"], shadow["fn"], shadow["tn"]) == (2, 0, 0, 2)
        assert (active["precision"], active["recall"], active["f1"]) == (0.5, 0.5, 0.5)
        assert (shadow["false_positive_rate"], shadow["specificity"], shadow["mcc"]) == (
            0.0,
            1.0,
            1.0,
        )
        assert shadow["intervals"]["recall"] == wilson(2, 2)
        assert (result["labelled"], result["positives"], result["unlabelled"]) == (4, 2, 0)
        assert result["mcnemar"]["active_only"] == 0 and result["mcnemar"]["shadow_only"] == 2

    def test_f1_is_zero_when_nothing_was_caught_and_none_with_nothing_to_count(self) -> None:
        labels = {"pump-1": [interval(T0, T0 + 1, "fault"), interval(T0 + 1, T0 + 2, "normal")]}
        rows = [pair(T0, False, False), pair(T0 + 1, True, False)]

        result = judge(rows, FAULTS, labels)

        assert (result["active"]["tp"], result["active"]["fp"], result["active"]["fn"]) == (0, 1, 1)
        assert result["active"]["f1"] == 0.0
        assert judge([pair(T0 + 1, False, False)], FAULTS, labels)["active"]["f1"] is None

    def test_the_answer_says_what_it_was_judged_on(self) -> None:
        result = judge([], FAULTS, {})

        assert (result["kind"], result["positive"]) == ("detector", ["fault"])
        assert result["judged_on"] == FAULTS.to_json()

    def test_each_input_is_held_against_its_own_sources_labels(self) -> None:
        labels = {
            "pump-1": [interval(T0, T0 + 10, "fault", "pump-1")],
            "pump-2": [interval(T0, T0 + 10, "normal", "pump-2")],
        }
        rows = [pair(T0 + 1, True, True, "pump-1"), pair(T0 + 1, True, True, "pump-2")]

        result = judge(rows, FAULTS, labels)

        assert (result["active"]["tp"], result["active"]["fp"]) == (1, 1)
        assert result["sources"] == ["pump-1", "pump-2"]

    def test_a_declared_source_judges_every_input_against_it(self) -> None:
        line = declared(positive=["fault"], source="line-3")
        labels = {"line-3": [interval(T0, T0 + 10, "fault", "line-3")]}
        rows = [pair(T0 + 1, True, False, "pump-1"), pair(T0 + 2, True, False, "pump-2")]

        result = judge(rows, line, labels)

        assert (result["active"]["tp"], result["shadow"]["fn"]) == (2, 2)
        assert sources_to_read(rows, line) == {"line-3": (T0 + 1, T0 + 3)}

    def test_an_input_no_label_covers_is_not_judged_but_its_alarm_counts(self) -> None:
        rows = [pair(T0, True, False), pair(T0 + 1, False, False)]

        result = judge(rows, FAULTS, {})

        assert (result["labelled"], result["unlabelled"]) == (0, 2)
        assert result["active"]["precision"] is None
        assert result["active"]["alarms_without_fault"] == 1

    def test_several_values_count_as_positive(self) -> None:
        several = declared(positive=["fault", "bearing"])
        labels = {"pump-1": [interval(T0, T0 + 1, "bearing"), interval(T0 + 1, T0 + 2, "fault")]}

        result = judge([pair(T0, True, True), pair(T0 + 1, True, True)], several, labels)

        assert (result["positives"], result["active"]["tp"]) == (2, 2)

    def test_the_sources_to_read_span_their_inputs_event_times(self) -> None:
        rows = [
            pair(T0 + 5, False, False, "a"),
            pair(T0, False, False, "a"),
            pair(T0 + 2, False, False, "b"),
        ]
        rows.append({"ts": T0, "source": None})

        assert sources_to_read(rows, FAULTS) == {"a": (T0, T0 + 6), "b": (T0 + 2, T0 + 3)}


# ── The detector, per fault and per day ───────────────────────────────────────


class TestPerFaultAndPerDay:
    def test_a_fault_is_caught_by_any_flag_inside_it_and_the_delay_is_from_its_start(self) -> None:
        labels = {
            "pump-1": [interval(T0, T0 + 600, "fault"), interval(T0 + 1000, T0 + 1600, "fault")]
        }
        rows = [
            pair(T0 + 60, False, False),
            pair(T0 + 120, False, True),  # shadow catches the first fault after 120 s
            pair(T0 + 300, True, True),  # active after 300 s
            pair(T0 + 1100, False, True),  # only shadow catches the second, after 100 s
            pair(T0 + 2000, False, False),  # no fault here
        ]

        result = judge(rows, FAULTS, labels)

        active, shadow = result["active"]["faults"], result["shadow"]["faults"]
        assert (active["seen"], active["caught"], active["recall"]) == (2, 1, 0.5)
        assert (shadow["caught"], shadow["recall"]) == (2, 1.0)
        assert active["delay_s"] == {"mean": 300.0, "median": 300.0, "max": 300.0}
        assert shadow["delay_s"] == {"mean": 110.0, "median": 110.0, "max": 120.0}

    def test_a_fault_no_input_fell_inside_was_not_seen(self) -> None:
        labels = {"pump-1": [interval(T0, T0 + 10, "fault"), interval(T0 + 100, None, "fault")]}

        result = judge([pair(T0 + 200, True, False)], FAULTS, labels)

        assert result["active"]["faults"]["seen"] == 1
        assert result["shadow"]["faults"]["delay_s"]["mean"] is None

    def test_alarms_without_a_fault_are_counted_per_day(self) -> None:
        labels = {"pump-1": [interval(T0, T0 + 10, "fault")]}
        rows = [
            pair(T0, True, True),
            pair(T0 + DAY, True, False),
            pair(T0 + 2 * DAY, True, False),
        ]

        result = judge(rows, FAULTS, labels)

        assert result["days"] == 2.0
        assert (result["active"]["alarms_without_fault"], result["active"]["alarms_per_day"]) == (
            2,
            1.0,
        )
        assert result["shadow"]["alarms_per_day"] == 0.0

    def test_under_an_hour_of_inputs_gives_no_rate_per_day(self) -> None:
        result = judge([pair(T0, True, True), pair(T0 + 60, True, True)], FAULTS, {})

        assert result["active"]["alarms_per_day"] is None


def test_every_kind_a_declaration_may_name_has_a_judge() -> None:
    assert set(KINDS) == set(judges.JUDGES)
