"""Asking with a label spec, writing a label from an agent, and a rule that records one.

``ask_operator(..., label=...)`` checks the spec before anything is sent and
keeps it in the question book, where main finds it once the question is
answered. ``Actor.label`` writes a label as the agent's own, on main; a rule's
``label`` action writes one as the rule's, from when the trigger says it
happened.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_ask_operator import Wired, hvac_watch
from wactorz.agents.function_agent import spec_of
from wactorz.agents.rule_agent import RuleAction, RuleAgent, RuleConfig, RuleError
from wactorz.core.labels import LabelError, Labels
from wactorz.core.persistence import WactorzDB
from wactorz.core.persistence.stores import Stores

T0 = 1_760_000_000.0
SOURCE = "sensors/ahu-1"
OPTIONS = ["Maintenance", "Real problem", "Not sure"]
SPEC = {
    "source": SOURCE,
    "start": T0,
    "values": {"Maintenance": "maintenance", "Real problem": "fault"},
}


@pytest.fixture(name="wired")
def wired_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wired:
    spec = spec_of(hvac_watch)
    assert spec is not None
    return Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "state" / "wactorz.db") as database:
        monkeypatch.setattr(Stores, "db", database)
        yield database


class TestAskingWithALabelSpec:
    async def test_the_spec_is_kept_in_the_book_with_the_answer(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator("Did something change?", options=OPTIONS, label=SPEC)
        )
        await wired.settle()

        (pending,) = wired.book
        assert pending.label is not None
        assert pending.label["source"] == SOURCE
        assert pending.label["values"] == SPEC["values"]

        await wired.answer("Maintenance", index=0)
        assert await asking == "Maintenance"

        (done,) = wired.book
        assert (done.answer, done.label) == ("Maintenance", pending.label)

    async def test_a_spec_that_cannot_be_used_sends_nothing(self, wired: Wired) -> None:
        bad = {**SPEC, "values": {"Maybe": "x"}}

        with pytest.raises(LabelError, match="not one of its options"):
            await wired.actor.ask_operator("Did something change?", options=OPTIONS, label=bad)

        assert wired.chat_frames == []
        assert wired.actor.questions() == {}

    async def test_a_question_without_a_spec_keeps_none(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()

        (pending,) = wired.book
        assert pending.label is None
        asking.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asking


class TestAnAgentsOwnLabel:
    async def test_an_agent_on_main_writes_a_label_as_its_own(
        self, wired: Wired, db: WactorzDB
    ) -> None:
        label = await wired.actor.label(SOURCE, "maintenance", start=T0, note="valve")

        assert (label.actor, label.value, label.note) == (
            "agent:hvac-watch",
            "maintenance",
            "valve",
        )
        assert Labels(db).query(SOURCE) == [label]

    async def test_without_main_s_database_an_agent_cannot_write_one(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Stores, "db", None)

        with pytest.raises(LabelError, match="kept by main"):
            await wired.actor.label(SOURCE, "maintenance", start=T0)

    async def test_an_agent_on_a_node_cannot_write_one(
        self, wired: Wired, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wired.actor, "reads_history_from_main", True, raising=False)

        with pytest.raises(LabelError, match="kept by main"):
            await wired.actor.label(SOURCE, "maintenance", start=T0)
        assert Labels(db).query() == []


def _rule(tmp_path: Path, action: dict[str, Any]) -> RuleAgent:
    config = RuleConfig.from_dict({"triggers": ["hvac/maintenance"], "actions": [action]})
    return RuleAgent(config, name="maintenance-switch", persistence_dir=str(tmp_path))


@pytest.fixture(name="quiet")
def quiet_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(RuleAgent, "_mqtt_publish", record)


class TestARuleThatLabels:
    def test_the_action_is_checked_and_round_trips(self) -> None:
        raw = {
            "type": "label",
            "source": "sensors/{unit}",
            "value": "maintenance",
            "text": "switch",
        }

        action = RuleAction.from_dict(raw)

        assert RuleAction.from_dict(action.to_dict()) == action
        assert action.to_dict()["source"] == "sensors/{unit}"

    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            ({"type": "label", "value": "maintenance"}, "source"),
            ({"type": "label", "source": SOURCE}, "value"),
        ],
    )
    def test_an_incomplete_label_action_is_refused(self, raw: dict[str, Any], why: str) -> None:
        with pytest.raises(RuleError, match=why):
            RuleAction.from_dict(raw)

    async def test_it_labels_from_when_the_trigger_says_it_happened(
        self, tmp_path: Path, db: WactorzDB, quiet: None
    ) -> None:
        rule = _rule(
            tmp_path,
            {
                "type": "label",
                "source": "sensors/{unit}",
                "value": "maintenance",
                "text": "switch on",
            },
        )

        assert await rule.evaluate({"unit": "ahu-1", "ts": T0})

        (label,) = Labels(db).query()
        assert (label.source, label.start, label.value) == (SOURCE, T0, "maintenance")
        assert (label.actor, label.note) == ("rule:maintenance-switch", "switch on")

    async def test_a_label_it_cannot_write_is_logged_and_the_rule_goes_on(
        self, tmp_path: Path, db: WactorzDB, quiet: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        rule = _rule(
            tmp_path, {"type": "label", "source": "sensors/{unit}", "value": "maintenance"}
        )

        # A trigger naming a wildcard as its unit makes a source that is a pattern.
        assert await rule.evaluate({"unit": "#", "ts": T0})

        assert Labels(db).query() == []
        assert "not a pattern" in caplog.text
