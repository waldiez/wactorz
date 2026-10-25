"""Labels: periods of a source, as someone said, and how a later one overrides an earlier.

Against a real on-disk SQLite database: supersession and resolution are what
the graph's tables hold, so the test reads them back rather than a fake's
memory. The story throughout is the HVAC valve: replaced at 14:02, called
maintenance at first, and a fault after 15:00 when the operator corrects it.
"""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core import labels as core_labels
from wactorz.core.labels import (
    OPERATOR,
    Label,
    LabelError,
    Labels,
    LabelSpec,
    event_time,
    vouch,
    vouched,
)
from wactorz.core.persistence import WactorzDB
from wactorz.core.provenance import (
    ANSWERED_BY,
    INVALIDATED,
    PARTLY_SUPERSEDED,
    QUESTION,
    SUPERSEDED,
    VALID,
)
from wactorz.core.questions import Question, answered

T0 = 1_760_000_000.0  # 14:02, the valve
HOUR = 3600.0
SOURCE = "sensors/ahu-1"
VALUES = {"Maintenance": "maintenance", "Real problem": "fault"}


@pytest.fixture(autouse=True)
def _no_vouches_from_other_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the dashboard vouched for is process-wide; each test starts with none."""
    monkeypatch.setattr(core_labels, "_vouched", {})


@pytest.fixture(name="labels")
def labels_fixture(tmp_path: Path) -> Iterator[Labels]:
    with WactorzDB(tmp_path / "state" / "wactorz.db") as database:
        yield Labels(database)


def maintenance(labels: Labels, **over: Any) -> Label:
    fields: dict[str, Any] = {
        "source": SOURCE,
        "start": T0,
        "value": "maintenance",
        "actor": OPERATOR,
        "recorded": T0 + 0.5 * HOUR,
    }
    fields.update(over)
    return labels.write(**fields)


class TestLabelSpec:
    def test_a_spec_maps_options_to_values(self) -> None:
        spec = LabelSpec.parse({"source": SOURCE, "start": T0, "values": VALUES}, tuple(VALUES))

        assert spec.value_for("Real problem") == "fault"
        assert spec.end is None

    def test_an_option_left_out_makes_no_label(self) -> None:
        spec = LabelSpec.parse(
            {"source": SOURCE, "start": T0, "values": {**VALUES, "Not sure": None}},
            (*VALUES, "Not sure"),
        )

        assert spec.value_for("Not sure") is None

    def test_a_start_may_be_iso_8601_with_an_offset(self) -> None:
        spec = LabelSpec.parse(
            {"source": SOURCE, "start": "2025-10-09T08:53:20+00:00", "values": VALUES}
        )

        assert spec.start == T0

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ({"start": T0, "values": VALUES}, "names the source"),
            ({"source": "sensors/#", "start": T0, "values": VALUES}, "not a pattern"),
            ({"source": SOURCE, "start": "yesterday", "values": VALUES}, "event time"),
            ({"source": SOURCE, "start": "2025-10-09T08:53:20", "values": VALUES}, "event time"),
            ({"source": SOURCE, "start": T0, "end": T0, "values": VALUES}, "ends after it starts"),
            ({"source": SOURCE, "start": T0, "values": {}}, "at least one option"),
            ({"source": SOURCE, "start": T0, "values": {"Maybe": "x"}}, "not one of its options"),
            ("sensors/ahu-1", "is a mapping"),
        ],
    )
    def test_a_spec_that_cannot_be_used_says_why(self, raw: object, match: str) -> None:
        with pytest.raises(LabelError, match=match):
            LabelSpec.parse(raw, tuple(VALUES))

    def test_a_spec_round_trips_through_its_dict(self) -> None:
        spec = LabelSpec.parse({"source": SOURCE, "start": T0, "end": T0 + HOUR, "values": VALUES})

        assert LabelSpec.from_dict(spec.to_dict()) == spec


class TestWriting:
    def test_a_label_reads_back_as_written(self, labels: Labels) -> None:
        written = labels.write(
            source=SOURCE, start=T0, value="maintenance", actor="agent:hvac", note="valve"
        )

        (found,) = labels.query(SOURCE)
        assert found == written
        assert (found.value, found.status, found.note, found.end) == (
            "maintenance",
            VALID,
            "valve",
            None,
        )

    def test_a_label_without_a_value_is_refused(self, labels: Labels) -> None:
        with pytest.raises(LabelError, match="has a value"):
            labels.write(source=SOURCE, start=T0, value=" ", actor=OPERATOR)

    def test_a_label_written_twice_under_one_id_is_one_label(self, labels: Labels) -> None:
        first = maintenance(labels, label_id="fixed")
        again = maintenance(labels, label_id="fixed", value="fault")

        assert again == first
        assert len(labels.query(SOURCE, include_superseded=True)) == 1

    def test_labels_on_different_sources_do_not_touch(self, labels: Labels) -> None:
        maintenance(labels)
        labels.write(source="sensors/ahu-2", start=T0, value="fault", actor=OPERATOR)

        assert [lab.status for lab in labels.query(include_superseded=True)] == [VALID, VALID]


class TestALaterAnswerWins:
    def test_a_correction_over_the_whole_window_supersedes_the_first(self, labels: Labels) -> None:
        first = maintenance(labels)

        labels.write(source=SOURCE, start=T0, value="fault", actor=OPERATOR, recorded=T0 + HOUR)

        old = labels.get(first.id)
        assert old is not None and old.status == SUPERSEDED
        assert [lab.value for lab in labels.query(SOURCE)] == ["fault"]

    def test_a_correction_over_part_of_the_window_supersedes_that_part(
        self, labels: Labels
    ) -> None:
        first = maintenance(labels)

        labels.write(
            source=SOURCE, start=T0 + HOUR, value="fault", actor=OPERATOR, recorded=T0 + 2 * HOUR
        )

        old = labels.get(first.id)
        assert old is not None and old.status == PARTLY_SUPERSEDED

    def test_the_first_is_kept_with_what_was_said(self, labels: Labels) -> None:
        first = maintenance(labels)
        labels.write(source=SOURCE, start=T0, value="fault", actor=OPERATOR, recorded=T0 + HOUR)

        everything = labels.query(SOURCE, include_superseded=True)

        assert [(lab.id, lab.value) for lab in everything] == [
            (first.id, "maintenance"),
            (everything[1].id, "fault"),
        ]

    def test_pieces_that_cover_the_window_between_them_supersede_it(self, labels: Labels) -> None:
        first = maintenance(labels, end=T0 + 2 * HOUR)
        labels.write(
            source=SOURCE,
            start=T0,
            end=T0 + HOUR,
            value="fault",
            actor=OPERATOR,
            recorded=T0 + HOUR,
        )
        labels.write(
            source=SOURCE,
            start=T0 + HOUR,
            end=T0 + 2 * HOUR,
            value="maintenance",
            actor=OPERATOR,
            recorded=T0 + 2 * HOUR,
        )

        old = labels.get(first.id)
        assert old is not None and old.status == SUPERSEDED

    def test_later_means_recorded_later_not_about_a_later_time(self, labels: Labels) -> None:
        correction = labels.write(
            source=SOURCE, start=T0, value="fault", actor=OPERATOR, recorded=T0 + HOUR
        )

        # The first answer reaches main only now, replayed from a retained book.
        replayed = maintenance(labels, recorded=T0 + 0.5 * HOUR)

        replayed_now, correction_now = labels.get(replayed.id), labels.get(correction.id)
        assert replayed_now is not None and replayed_now.status == SUPERSEDED
        assert correction_now is not None and correction_now.status == VALID

    def test_two_labels_written_at_the_same_instant_are_ordered_as_written(
        self, labels: Labels
    ) -> None:
        first = maintenance(labels, recorded=T0 + 1)
        labels.write(
            source=SOURCE, start=T0, end=T0 + HOUR, value="fault", actor=OPERATOR, recorded=T0 + 1
        )

        held = [i.label.value for i in labels.effective(SOURCE, since=T0, until=T0 + HOUR)]
        now = labels.get(first.id)

        assert held == ["fault"]
        assert now is not None and now.status == PARTLY_SUPERSEDED

    def test_supersession_reaches_past_a_listing_s_limit(self, labels: Labels) -> None:
        """A source with more labels than a listing returns still has every one checked."""
        count = 1200
        for i in range(count):
            labels.write(
                source=SOURCE,
                start=T0 + i * HOUR,
                end=T0 + (i + 1) * HOUR,
                value=f"v{i}",
                actor="agent:x",
                recorded=T0 + i,
            )

        labels.write(
            source=SOURCE,
            start=T0 + (count - 2) * HOUR,
            value="fault",
            actor=OPERATOR,
            recorded=T0 + count,
        )

        last_two = labels.query(SOURCE, since=T0 + (count - 2) * HOUR, include_superseded=True)
        assert [(lab.value, lab.status) for lab in last_two] == [
            (f"v{count - 2}", SUPERSEDED),
            (f"v{count - 1}", SUPERSEDED),
            ("fault", VALID),
        ]
        held = [i.label.value for i in labels.effective(SOURCE, since=T0 + (count - 2) * HOUR)]
        assert held == ["fault"]

    def test_an_invalidated_label_stays_invalidated(self, labels: Labels) -> None:
        first = maintenance(labels)
        labels.graph.set_status(first.id, INVALIDATED)

        labels.write(source=SOURCE, start=T0, value="fault", actor=OPERATOR, recorded=T0 + HOUR)

        now = labels.get(first.id)
        assert now is not None and now.status == INVALIDATED


class TestWhatHeldWhen:
    def test_a_correction_from_a_later_time_splits_the_period(self, labels: Labels) -> None:
        maintenance(labels)
        labels.write(
            source=SOURCE, start=T0 + HOUR, value="fault", actor=OPERATOR, recorded=T0 + 2 * HOUR
        )

        held = [(i.start, i.end, i.label.value) for i in labels.effective(SOURCE)]

        assert held == [(T0, T0 + HOUR, "maintenance"), (T0 + HOUR, None, "fault")]

    def test_a_window_cuts_what_holds_to_its_edges(self, labels: Labels) -> None:
        maintenance(labels)

        held = [
            (i.start, i.end) for i in labels.effective(SOURCE, since=T0 + HOUR, until=T0 + 2 * HOUR)
        ]

        assert held == [(T0 + HOUR, T0 + 2 * HOUR)]

    def test_a_listing_s_limit_cuts_after_the_window(self, labels: Labels) -> None:
        for i in range(5):
            labels.write(
                source=SOURCE,
                start=T0 + i * HOUR,
                end=T0 + (i + 1) * HOUR,
                value=f"v{i}",
                actor="agent:x",
                recorded=T0 + i,
            )

        found = labels.query(SOURCE, since=T0 + 4 * HOUR, until=T0 + 5 * HOUR, limit=3)

        assert [lab.value for lab in found] == ["v4"]

    def test_a_gap_no_label_covers_is_left_out(self, labels: Labels) -> None:
        maintenance(labels, end=T0 + HOUR)
        labels.write(
            source=SOURCE,
            start=T0 + 2 * HOUR,
            value="fault",
            actor=OPERATOR,
            recorded=T0 + 3 * HOUR,
        )

        held = [(i.start, i.end, i.label.value) for i in labels.effective(SOURCE)]

        assert held == [(T0, T0 + HOUR, "maintenance"), (T0 + 2 * HOUR, None, "fault")]

    def test_a_source_with_no_labels_held_nothing(self, labels: Labels) -> None:
        assert labels.effective(SOURCE) == []


class TestAnswersBecomeLabels:
    def question(self, option: str | None) -> Question:
        asked = Question(
            id="q1",
            text="Did something change on AHU-1?",
            options=(*VALUES, "Not sure"),
            channel="telegram",
            asked_at=T0 + 60,
            label={"source": SOURCE, "start": T0, "values": VALUES},
        )
        if option is None:
            return asked
        return answered(asked, option, asked.options.index(option), "telegram", "valve replaced")

    def test_an_answer_that_maps_to_a_value_makes_a_label(self, labels: Labels) -> None:
        label = labels.from_answer(self.question("Maintenance"), agent="hvac-watch")

        assert label is not None
        assert (label.source, label.start, label.value) == (SOURCE, T0, "maintenance")
        assert (label.actor, label.channel, label.note) == (OPERATOR, "telegram", "valve replaced")
        assert label.verified is False

    def test_the_question_is_joined_to_the_label_it_made(self, labels: Labels) -> None:
        label = labels.from_answer(self.question("Maintenance"), agent="hvac-watch")
        assert label is not None

        (question,) = labels.graph.nodes(QUESTION)
        (edge,) = labels.graph.edges(question.id)
        assert (edge.src, edge.dst, edge.relation) == (question.id, label.id, ANSWERED_BY)
        assert question.payload["answer"] == "Maintenance"

    def test_not_sure_makes_no_label_but_the_question_is_kept(self, labels: Labels) -> None:
        assert labels.from_answer(self.question("Not sure"), agent="hvac-watch") is None

        assert labels.query(SOURCE) == []
        assert len(labels.graph.nodes(QUESTION)) == 1

    def test_an_open_question_makes_nothing(self, labels: Labels) -> None:
        assert labels.from_answer(self.question(None), agent="hvac-watch") is None
        assert labels.graph.nodes(QUESTION) == []

    def test_the_label_is_recorded_when_main_writes_it_not_when_the_node_says(
        self, labels: Labels
    ) -> None:
        """A node's clock running ahead must not make its answer newer than a later correction."""
        ahead = self.question("Maintenance")
        ahead = Question(
            **{**ahead.to_dict(), "options": ahead.options, "answered_at": T0 + 10 * HOUR}
        )
        before = time.time()

        label = labels.from_answer(ahead, agent="hvac-watch")

        assert label is not None
        assert before <= label.recorded <= time.time()
        correction = labels.write(source=SOURCE, start=T0, value="fault", actor=OPERATOR)
        assert [i.label.id for i in labels.effective(SOURCE)] == [correction.id]

    def test_the_same_answer_seen_again_is_recorded_once(self, labels: Labels) -> None:
        question = self.question("Maintenance")

        first = labels.from_answer(question, agent="hvac-watch")
        again = labels.from_answer(question, agent="hvac-watch")

        assert again == first
        assert len(labels.query(SOURCE)) == 1

    def test_a_question_without_a_spec_makes_nothing(self, labels: Labels) -> None:
        question = answered(
            Question(id="q2", text="Ok?", options=("yes", "no"), channel="dashboard"),
            "yes",
            0,
            "dashboard",
        )

        assert labels.from_answer(question, agent="hvac-watch") is None

    def test_a_question_keeps_its_spec_through_the_book(self) -> None:
        question = self.question("Maintenance")

        assert Question.from_dict(question.to_dict()) == question


class TestTrustAndTime:
    def test_an_answer_the_dashboard_sent_is_vouched_for(self) -> None:
        vouch("agent-id", "q9", "Maintenance")

        assert vouched("agent-id", "q9", "Maintenance")
        assert not vouched("agent-id", "q9", "Real problem")
        assert not vouched("agent-id", "q9", None)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"ts": T0, "timestamp": 1.0}, T0),
            ({"timestamp": "2025-10-09T08:53:20Z"}, T0),
            ({"ts": "garbage", "timestamp": T0}, T0),
            ({"ts": "2025-10-09T08:53:20Z"}, 42.0),
            ({"value": 3}, 42.0),
            ("not a mapping", 42.0),
        ],
    )
    def test_event_time_is_what_the_payload_says_else_the_default(
        self, payload: object, expected: float
    ) -> None:
        assert event_time(payload, default=42.0) == expected
