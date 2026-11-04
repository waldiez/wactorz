"""A model's answers judged against the labels, as its kind says: detector, classifier, regressor.

The statistics are checked against values worked out by hand or published
tables; each kind against small trials whose right answers can be counted.
"""

import json
import math
import random
from typing import Any

import pytest

from wactorz.core import judges
from wactorz.core.judgement import CLASSIFIER, KINDS, REGRESSOR, declared
from wactorz.core.judges import (
    class_of,
    judge,
    mcc,
    mcnemar,
    number_of,
    ratio,
    sources_to_read,
    wilson,
)
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

    def test_a_fault_labelled_again_inside_itself_is_one_fault(self) -> None:
        # The site's label over the whole fault, then an operator's newer label
        # inside it: the effective intervals come as three touching pieces.
        site = interval(T0, T0 + 600, "fault")
        labels = {
            "pump-1": [
                interval(T0, T0 + 200, "fault"),
                interval(T0 + 200, T0 + 400, "fault"),
                Interval(start=T0 + 400, end=T0 + 600, label=site.label),
            ]
        }
        rows = [pair(T0 + 100, False, False), pair(T0 + 300, True, False)]

        faults = judge(rows, FAULTS, labels)["active"]["faults"]

        assert (faults["seen"], faults["caught"]) == (1, 1)
        assert faults["delay_s"]["max"] == 300.0

    def test_touching_faults_of_different_positive_values_are_one_fault(self) -> None:
        several = declared(positive=["fault", "bearing"])
        labels = {"pump-1": [interval(T0, T0 + 10, "bearing"), interval(T0 + 10, None, "fault")]}

        faults = judge([pair(T0 + 1, False, False), pair(T0 + 20, True, True)], several, labels)

        assert (faults["active"]["faults"]["seen"], faults["active"]["faults"]["caught"]) == (1, 1)
        assert faults["active"]["faults"]["delay_s"]["max"] == 20.0

    def test_a_gap_or_a_negative_label_between_faults_keeps_them_apart(self) -> None:
        labels = {
            "pump-1": [
                interval(T0, T0 + 10, "fault"),
                interval(T0 + 20, T0 + 30, "fault"),  # a gap no label holds before it
                interval(T0 + 30, T0 + 40, "normal"),
                interval(T0 + 40, T0 + 50, "fault"),
            ]
        }
        rows = [pair(T0 + 5, True, True), pair(T0 + 25, True, True), pair(T0 + 45, True, True)]

        faults = judge(rows, FAULTS, labels)["active"]["faults"]

        assert (faults["seen"], faults["caught"]) == (3, 3)
        assert faults["delay_s"]["max"] == 5.0

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


# ── The classifier ────────────────────────────────────────────────────────────


APPLIANCE = declared(kind=CLASSIFIER, field="appliance", by="cli", at=T0)


def answered(ts: float, active: Any, shadow: Any, source: str = "meter") -> dict[str, Any]:
    """An input at ``ts`` and what each side published for it: a value in the field, or None."""
    return {
        "ts": ts,
        "source": source,
        "active_output": None
        if active is None
        else json.dumps({"appliance": active, "co2": active}),
        "shadow_output": None
        if shadow is None
        else json.dumps({"appliance": shadow, "co2": shadow}),
    }


APPLIANCES = {
    "meter": [
        interval(T0, T0 + 100, "kettle", source="meter"),
        interval(T0 + 100, T0 + 200, "washer", source="meter"),
    ]
}

#: Truths kettle, kettle, kettle, washer, washer; then an input no label covers.
CLASSIFIED = [
    answered(T0 + 10, "kettle", "kettle"),
    answered(T0 + 20, "washer", "kettle"),
    answered(T0 + 30, None, "kettle"),
    answered(T0 + 110, "washer", "washer"),
    answered(T0 + 120, "washer", "kettle"),
    answered(T0 + 300, "kettle", "kettle"),
]


class TestTheClassifier:
    def test_each_side_is_scored_against_the_label_values(self) -> None:
        result = judge(CLASSIFIED, APPLIANCE, APPLIANCES)

        assert (result["kind"], result["labelled"], result["unlabelled"]) == (CLASSIFIER, 5, 1)
        assert result["classes"] == ["kettle", "washer"]
        active, shadow = result["active"], result["shadow"]
        assert (active["right"], active["accuracy"], active["no_answer"]) == (3, 0.6, 1)
        assert (shadow["right"], shadow["accuracy"], shadow["no_answer"]) == (4, 0.8, 0)
        assert active["accuracy_interval"] == wilson(3, 5)

    def test_per_class_figures_and_their_means(self) -> None:
        active = judge(CLASSIFIED, APPLIANCE, APPLIANCES)["active"]

        kettle, washer = active["per_class"]["kettle"], active["per_class"]["washer"]
        assert (kettle["precision"], kettle["recall"], kettle["f1"], kettle["support"]) == (
            1.0, 1 / 3, 0.5, 3,
        )  # fmt: skip
        assert (washer["precision"], washer["recall"], washer["f1"], washer["support"]) == (
            2 / 3, 1.0, 0.8, 2,
        )  # fmt: skip
        assert active["macro_f1"] == pytest.approx(0.65)
        assert active["weighted_f1"] == pytest.approx((0.5 * 3 + 0.8 * 2) / 5)

    def test_the_confusion_has_a_column_for_no_answer(self) -> None:
        result = judge(CLASSIFIED, APPLIANCE, APPLIANCES)

        # Rows as labelled (kettle, washer); columns as answered (kettle, washer, no answer).
        assert result["active"]["confusion"] == [[1, 1, 1], [0, 2, 0]]
        assert result["shadow"]["confusion"] == [[3, 0, 0], [1, 1, 0]]

    def test_the_sides_are_compared_on_the_inputs_only_one_got_right(self) -> None:
        test = judge(CLASSIFIED, APPLIANCE, APPLIANCES)["mcnemar"]

        assert (test["active_only"], test["shadow_only"]) == (1, 2)

    def test_an_output_without_a_class_in_the_field_is_no_answer(self) -> None:
        rows = [
            {"ts": T0 + 1, "source": "meter", "active_output": json.dumps({"score": 1}),
             "shadow_output": json.dumps(["kettle"])},
            {"ts": T0 + 2, "source": "meter", "active_output": "not json",
             "shadow_output": json.dumps({"appliance": None})},
        ]  # fmt: skip

        result = judge(rows, APPLIANCE, APPLIANCES)

        assert result["active"]["no_answer"] == result["shadow"]["no_answer"] == 2
        assert result["active"]["accuracy"] == 0.0

    def test_a_class_answered_that_no_label_holds_is_a_class_too(self) -> None:
        result = judge([answered(T0 + 1, "toaster", "kettle")], APPLIANCE, APPLIANCES)

        assert result["classes"] == ["kettle", "toaster"]
        assert result["active"]["per_class"]["toaster"] == {
            "precision": 0.0, "recall": None, "f1": 0.0, "support": 0,
        }  # fmt: skip

    @pytest.mark.parametrize(
        ("value", "reads"),
        [("kettle", "kettle"), (" washer ", "washer"), (3, "3"), (3.0, "3"), (True, "true"),
         (2.5, None), ("", None), (None, None), ({"a": 1}, None)],
    )  # fmt: skip
    def test_a_class_is_a_word_a_whole_number_or_a_truth_value(
        self, value: Any, reads: Any
    ) -> None:
        assert class_of(value) == reads

    def test_many_inputs_and_classes_count_as_one_by_one_would(self) -> None:
        rng = random.Random(3)
        names = [f"c{i:02}" for i in range(40)]
        truths = {"meter": [interval(T0 + 100 * i, T0 + 100 * (i + 1), n, source="meter")
                            for i, n in enumerate(names)]}  # fmt: skip
        rows = [
            answered(T0 + i * 2, rng.choice([*names, None]), rng.choice(names))
            for i in range(2_000)
        ]

        result = judge(rows, APPLIANCE, truths)

        # Each cell counted the slow way: the label of each input, and what the side answered.
        classes = result["classes"]
        cells = [(names[(i * 2) // 100], json.loads(r["active_output"])["appliance"] if r["active_output"] else None)
                 for i, r in enumerate(rows)]  # fmt: skip
        assert result["active"]["confusion"] == [
            [*(cells.count((truth, said)) for said in classes), cells.count((truth, None))]
            for truth in classes
        ]
        for name in classes:
            figures = result["active"]["per_class"][name]
            tp = cells.count((name, name))
            assert figures["support"] == sum(1 for truth, _ in cells if truth == name)
            assert figures["precision"] == ratio(tp, sum(1 for _, said in cells if said == name))

    def test_nothing_labelled_has_no_figures(self) -> None:
        result = judge([answered(T0 + 900, "kettle", "kettle")], APPLIANCE, APPLIANCES)

        assert (result["labelled"], result["active"]["accuracy"], result["active"]["macro_f1"]) == (
            0, None, None,
        )  # fmt: skip


# ── The regressor ─────────────────────────────────────────────────────────────


CO2 = declared(kind=REGRESSOR, field="co2", by="cli", at=T0)

READINGS = {
    "meter": [
        interval(T0, T0 + 100, "420", source="meter"),
        interval(T0 + 100, T0 + 200, "high", source="meter"),
    ]
}

#: Truth 420 for the first four; a label that is not a number; an input no label covers.
REGRESSED = [
    answered(T0 + 10, 400, 430),
    answered(T0 + 20, 450, 400),
    answered(T0 + 30, 420, None),
    answered(T0 + 40, 410, 430),
    answered(T0 + 110, 1, 1),
    answered(T0 + 500, 1, 1),
]


class TestTheRegressor:
    def test_each_sides_errors_against_the_label_values(self) -> None:
        result = judge(REGRESSED, CO2, READINGS)

        assert (result["kind"], result["labelled"], result["not_numbers"], result["unlabelled"]) == (
            REGRESSOR, 4, 1, 1,
        )  # fmt: skip
        active, shadow = result["active"], result["shadow"]
        assert (active["judged"], active["no_answer"], active["mae"], active["bias"]) == (
            4,
            0,
            15,
            0,
        )
        assert active["rmse"] == pytest.approx(math.sqrt(350))
        assert active["worst"] == 30
        assert (shadow["judged"], shadow["no_answer"], shadow["bias"], shadow["worst"]) == (
            3,
            1,
            0,
            20,
        )
        assert shadow["mae"] == pytest.approx(40 / 3)
        assert shadow["rmse"] == pytest.approx(math.sqrt(200))

    def test_closer_counts_each_input_once_and_an_answer_beats_none(self) -> None:
        closer = judge(REGRESSED, CO2, READINGS)["closer"]

        assert (closer["active"], closer["shadow"], closer["ties"]) == (1, 2, 1)
        assert (closer["p"], closer["better"]) == (1.0, None)

    def test_a_side_closer_every_time_is_better(self) -> None:
        rows = [answered(T0 + i, 400, 419) for i in range(10)]

        closer = judge(rows, CO2, READINGS)["closer"]

        assert closer["shadow"] == 10 and closer["better"] == "shadow"
        assert closer["p"] == pytest.approx(2 / 2**10)

    @pytest.mark.parametrize(
        ("value", "reads"),
        [(3, 3.0), (2.5, 2.5), (" 420.5 ", 420.5), ("high", None), (True, None),
         (float("nan"), None), ("inf", None), (None, None)],
    )  # fmt: skip
    def test_a_number_is_a_finite_number_or_its_text(self, value: Any, reads: Any) -> None:
        assert number_of(value) == reads

    def test_nothing_labelled_has_no_errors(self) -> None:
        result = judge([answered(T0 + 150, 1, 1)], CO2, READINGS)

        assert result["not_numbers"] == 1 and result["active"]["mae"] is None
        assert result["sources"] == []
