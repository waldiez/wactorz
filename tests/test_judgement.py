# SPDX-License-Identifier: Apache-2.0
"""A model version says what it is judged on, keeps it with the version, and passes it on.

The declaration is checked as it is given; the store keeps it beside the
version, copies it to the next version stored without one, and keeps each
earlier declaration when it is changed.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from wactorz import artifacts_cli
from wactorz.agents.main.commands import models as models_commands
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.core.artifacts import ArtifactNotFoundError, ArtifactStore
from wactorz.core.judgement import (
    CLASSIFIER,
    DETECTOR,
    MAX_POSITIVE,
    REGRESSOR,
    Judgement,
    JudgementError,
    declared,
    from_json,
)

if TYPE_CHECKING:
    from wactorz.agents.main.actor import MainActor


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(tmp_path / "state")


@pytest.fixture(name="model")
def model_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "detector.onnx"
    path.write_bytes(b"onnx")
    return path


def faults(*values: str, source: str | None = None, by: str = "cli") -> Judgement:
    return declared(positive=values or ("fault",), source=source, by=by)


# ── The declaration ───────────────────────────────────────────────────────────


class TestTheDeclaration:
    def test_a_detector_names_its_positive_values_once_each(self) -> None:
        judgement = declared(positive=[" fault", "bearing", "fault"], by="cli", at=10.0)

        assert (judgement.kind, judgement.positive, judgement.source) == (
            DETECTOR,
            ("fault", "bearing"),
            None,
        )
        assert (judgement.by, judgement.at) == ("cli", 10.0)

    def test_values_may_come_as_one_comma_separated_word(self) -> None:
        assert from_json({"positive": "fault, bearing"}).positive == ("fault", "bearing")

    def test_it_reads_back_what_it_wrote(self) -> None:
        judgement = declared(positive=["fault"], source="pump-1", by="chat", at=5.0)

        assert from_json(judgement.to_json()) == judgement

    def test_a_kind_this_version_does_not_judge_keeps_what_it_declared(self) -> None:
        stored = {"kind": DETECTOR, "positive": ["fault"], "window_s": 60}

        assert from_json(stored).extra == {"window_s": 60}

    @pytest.mark.parametrize(
        ("given", "says"),
        [
            ([], "is an object"),
            ({"kind": "oracle", "positive": ["x"]}, "not a kind of model judged here"),
            ({"positive": []}, "name at least one"),
            ({"positive": ["ok", ""]}, "is not a label value"),
            ({"positive": ["x" * 65]}, "is not a label value"),
            ({"positive": [3]}, "is not a label value"),
            ({"positive": [f"v{i}" for i in range(MAX_POSITIVE + 1)]}, f"at most {MAX_POSITIVE}"),
            ({"positive": ["fault"], "source": "sensors/#"}, "is not a label source"),
            ({"positive": ["fault"], "source": 7}, "is not a label source"),
            ({"positive": ["fault"], "source": "s" * 257}, "is not a label source"),
            ({"kind": CLASSIFIER}, "the output field it answers in"),
            ({"kind": REGRESSOR, "field": "  "}, "the output field it answers in"),
            ({"kind": CLASSIFIER, "field": 3}, "is not an output field"),
            ({"kind": CLASSIFIER, "field": "f" * 65}, "is not an output field"),
            ({"kind": CLASSIFIER, "field": "x", "positive": ["a"]}, "what a detector flags"),
            ({"positive": ["fault"], "field": "score"}, "not on a field"),
        ],
    )
    def test_what_cannot_be_taken_says_why(self, given: Any, says: str) -> None:
        with pytest.raises(JudgementError, match=says):
            from_json(given)

    @pytest.mark.parametrize("kind", [CLASSIFIER, REGRESSOR])
    def test_a_kind_that_reads_a_field_names_it_and_reads_back(self, kind: str) -> None:
        judgement = declared(kind=kind, field=" appliance ", source="meter", by="cli", at=5.0)

        assert (judgement.kind, judgement.field, judgement.positive) == (kind, "appliance", ())
        assert from_json(judgement.to_json()) == judgement

    def test_a_detector_stores_no_field(self) -> None:
        assert "field" not in faults().to_json()

    def test_another_field_is_other_terms(self) -> None:
        one = declared(kind=CLASSIFIER, field="appliance")

        assert one.same_terms(declared(kind=CLASSIFIER, field="appliance", by="chat"))
        assert not one.same_terms(declared(kind=CLASSIFIER, field="device"))
        assert not one.same_terms(declared(kind=REGRESSOR, field="appliance"))

    def test_an_empty_source_is_each_inputs_own(self) -> None:
        assert from_json({"positive": ["fault"], "source": "  "}).source is None

    def test_the_same_terms_whoever_said_them(self) -> None:
        assert faults(by="cli").same_terms(faults(by="chat"))
        assert not faults("fault").same_terms(faults("bearing"))
        assert not faults().same_terms(None)


# ── Kept with the version ─────────────────────────────────────────────────────


class TestKeptWithTheVersion:
    def test_a_version_stored_with_one_keeps_it(self, store: ArtifactStore, model: Path) -> None:
        stored = store.add(model, "imu", judged_on=faults(source="pump-1"))

        assert stored.judgement is not None
        assert (stored.judgement.positive, stored.judgement.source) == (("fault",), "pump-1")
        assert store.resolve("imu:1").judged_on == stored.judged_on

    def test_a_version_stored_without_one_takes_the_previous_versions(
        self, store: ArtifactStore, model: Path
    ) -> None:
        store.add(model, "imu", judged_on=faults("fault", "bearing"))

        second = store.add(model, "imu")

        assert second.judgement is not None
        assert second.judgement.positive == ("fault", "bearing")
        assert second.judgement.from_version == 1

    def test_the_first_version_without_one_has_none(
        self, store: ArtifactStore, model: Path
    ) -> None:
        stored = store.add(model, "imu")

        assert (stored.judged_on, stored.judgement) == (None, None)

    def test_a_declaration_the_code_cannot_read_reads_as_none(
        self, store: ArtifactStore, model: Path
    ) -> None:
        stored = store.add(model, "imu", judged_on=faults())
        broken = type(stored)(**{**stored.__dict__, "judged_on": {"kind": "oracle"}})

        assert broken.judgement is None


class TestJudgingAStoredVersion:
    def test_a_version_stored_before_gets_one(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu")

        judged = store.judge("imu:1", faults(by="chat"))

        assert judged.judgement is not None and judged.judgement.by == "chat"
        assert judged.judged_on_history == []
        assert store.resolve("imu:1").judged_on == judged.judged_on

    def test_a_change_keeps_what_it_replaced(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu", judged_on=faults("fault"))

        judged = store.judge("imu:1", faults("fault", "bearing"))

        assert judged.judgement is not None and judged.judgement.positive == ("fault", "bearing")
        assert [h["positive"] for h in judged.judged_on_history] == [["fault"]]

    def test_the_same_terms_again_change_nothing(self, store: ArtifactStore, model: Path) -> None:
        stored = store.add(model, "imu", judged_on=faults(by="cli"))

        again = store.judge("imu:1", faults(by="chat"))

        assert again == stored

    def test_none_says_nothing_judges_it_and_keeps_what_did(
        self, store: ArtifactStore, model: Path
    ) -> None:
        store.add(model, "imu", judged_on=faults())

        cleared = store.judge("imu:1", None)

        assert cleared.judged_on is None
        assert len(cleared.judged_on_history) == 1
        assert store.judge("imu:1", None) == cleared

    def test_the_history_keeps_the_newest(
        self, store: ArtifactStore, model: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.core.artifacts.JUDGEMENTS_KEPT", 2)
        store.add(model, "imu", judged_on=faults("v0"))
        for value in ("v1", "v2", "v3"):
            judged = store.judge("imu:1", faults(value))

        assert [h["positive"] for h in judged.judged_on_history] == [["v1"], ["v2"]]

    def test_a_pointer_names_the_version_judged(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu")
        store.add(model, "imu")
        store.move("active", "imu:1", by="cli")

        store.judge("imu:active", faults())

        assert store.resolve("imu:1").judged_on is not None
        assert store.resolve("imu:2").judged_on is None

    def test_a_version_not_stored_is_refused(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.judge("imu:4", faults())


# ── From the command line and from chat ──────────────────────────────────────


class _Actor:
    """The chat verb reaches nothing of main's; this stands in for MainActor."""


def chat() -> CommandContext:
    return CommandContext(actor=cast("MainActor", _Actor()))


class TestTheCommandLine:
    @pytest.fixture(autouse=True)
    def _capture(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.artifacts_cli")
        self.caplog = caplog

    def run(self, *argv: str) -> tuple[int, str]:
        self.caplog.clear()
        code = artifacts_cli.main(list(argv))
        return code, self.caplog.text

    def test_add_says_what_it_is_judged_on_and_the_next_add_takes_it(
        self, tmp_path: Path, model: Path
    ) -> None:
        state = str(tmp_path / "state")

        code, out = self.run(
            "add", str(model), "imu", "--positive", "fault", "--positive", "bearing",
            "--state-dir", state,
        )  # fmt: skip
        assert code == 0
        assert "Judged on fault, bearing as positive, against each input's own source" in out

        code, out = self.run("add", str(model), "imu", "--state-dir", state)
        assert code == 0 and "(from version 1)" in out

    def test_judge_sets_changes_and_clears_it(self, tmp_path: Path, model: Path) -> None:
        state = str(tmp_path / "state")
        assert self.run("add", str(model), "imu", "--state-dir", state)[0] == 0

        code, out = self.run(
            "judge",
            "imu:1",
            "--positive",
            "fault",
            "--label-source",
            "line-3",
            "--state-dir",
            state,
        )
        assert (
            code == 0
            and "imu:1 is judged on fault as positive, against the labels of line-3" in out
        )

        code, out = self.run("judge", "imu:1", "--clear", "--state-dir", state)
        assert code == 0 and "imu:1 is judged on nothing now" in out
        stored = ArtifactStore.in_state_dir(state).resolve("imu:1")
        assert stored.judged_on is None and len(stored.judged_on_history) == 1

    def test_a_classifier_and_a_regressor_name_their_field(
        self, tmp_path: Path, model: Path
    ) -> None:
        state = str(tmp_path / "state")

        code, out = self.run(
            "add", str(model), "meter", "--kind", "classifier", "--field", "appliance",
            "--state-dir", state,
        )  # fmt: skip
        assert code == 0
        assert "Judged on its 'appliance' field as a classifier, against each input's own" in out

        code, out = self.run(
            "judge", "meter:1", "--kind", "regressor", "--field", "watts",
            "--label-source", "clamp", "--state-dir", state,
        )  # fmt: skip
        assert code == 0
        assert (
            "meter:1 is judged on its 'watts' field as a regressor, against the labels of clamp"
            in out
        )
        judgement = ArtifactStore.in_state_dir(state).resolve("meter:1").judgement
        assert judgement is not None and (judgement.kind, judgement.field) == (REGRESSOR, "watts")

    @pytest.mark.parametrize(
        ("argv", "says"),
        [
            (("judge", "imu:1"), "name at least one"),
            (("judge", "imu:1", "--kind", "classifier"), "the output field it answers in"),
            (("judge", "imu:1", "--field", "watts"), "not on a field"),
            (("add", "MODEL", "imu", "--kind", "regressor"), "the output field it answers in"),
            (("judge", "imu:1", "--label-source", "line-3"), "name at least one"),
            (("judge", "imu:1", "--positive", "x" * 65), "is not a label value"),
            (("judge", "imu:9", "--positive", "fault"), "imu:9"),
        ],
    )
    def test_what_it_cannot_take_says_why(
        self, tmp_path: Path, model: Path, argv: tuple[str, ...], says: str
    ) -> None:
        state = str(tmp_path / "state")
        assert self.run("add", str(model), "imu", "--state-dir", state)[0] == 0

        argv = tuple(str(model) if a == "MODEL" else a for a in argv)
        code, out = self.run(*argv, "--state-dir", state)

        assert code == 1 and says in out


class TestFromChat:
    @pytest.fixture(autouse=True)
    def _store_here(self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(models_commands, "resolve_state_dir", lambda: str(store.root.parent))

    async def test_a_version_is_judged_from_chat(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu")

        reply = await models_commands.judge_model(chat(), "imu:1 fault,bearing line-3")

        assert (
            reply == "imu:1 is judged on fault, bearing as positive, against the labels of line-3."
        )
        judgement = store.resolve("imu:1").judgement
        assert judgement is not None and judgement.by == "chat"

    async def test_none_judges_it_on_nothing(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu", judged_on=faults())

        reply = await models_commands.judge_model(chat(), "imu:1 none")

        assert reply == "imu:1 is judged on nothing now."

    async def test_the_usage_says_where_the_other_kinds_are_declared(self) -> None:
        reply = await models_commands.judge_model(chat(), "")

        assert "wactorz-artifacts judge --kind" in reply

    @pytest.mark.parametrize(
        ("argument", "says"),
        [
            ("imu:1", "Usage: /artifacts judge"),
            ("", "Usage"),
            ("imu:7 fault", "Cannot judge 'imu:7'"),
        ],
    )
    async def test_what_it_cannot_take_says_why(
        self, store: ArtifactStore, model: Path, argument: str, says: str
    ) -> None:
        store.add(model, "imu")

        assert says in await models_commands.judge_model(chat(), argument)


def test_a_string_of_values_is_split_on_commas_not_into_letters() -> None:
    assert declared(positive="fault,bearing").positive == ("fault", "bearing")
