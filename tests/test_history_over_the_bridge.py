"""Main's stored history, read by an agent on a node.

The rows live in main's SQLite. A node's agent asks for them over the broker and
main answers a page at a time, each page small enough for one MQTT message. What
matters is that the two sides agree: the node, asking for pages until main says
there are no more, ends with exactly the rows main would have read itself, in
the same order, even when the history grows while it reads.

Main's side is :mod:`wactorz.agents.main.history`, which also refuses anything a
query does not name; the node's is :mod:`wactorz.node.history`. The admission
both share (a signature, the node's own reply topic) is pinned in
`test_llm_bridge.py`.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from wactorz.agents.main import history as main_history
from wactorz.agents.main.history import PAGE_ROWS, answer_history, fit_page
from wactorz.core.persistence import WactorzDB
from wactorz.node import history as node_history
from wactorz.node.history import read_history, read_stats

if TYPE_CHECKING:
    from wactorz.node.agent import NodeAgent

Ask = Callable[[str, dict[str, Any], float], Awaitable[Any]]


class _Agent:
    """Stands in for a NodeAgent: a name, and a way to ask main."""

    def __init__(self, ask: Ask) -> None:
        self.name = "collector"
        self._ask = ask
        self.asked: list[dict[str, Any]] = []

    async def ask_main(self, topic: str, payload: dict[str, Any], timeout: float) -> Any:
        self.asked.append(payload)
        return await self._ask(topic, payload, timeout)


async def _main_answers(topic: str, payload: dict[str, Any], _timeout: float) -> Any:
    """Main's real answer, through JSON as the broker carries it."""
    assert topic == main_history.HISTORY_TOPIC
    return json.loads(json.dumps(await answer_history(json.loads(json.dumps(payload)))))


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WactorzDB:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    monkeypatch.setattr(main_history, "get_db", lambda: db)
    return db


def _readings(db: WactorzDB, count: int, *, start: float | None = None) -> None:
    first = time.time() - 3600 if start is None else start
    db.write_sensor_batch(
        [
            (first + i, "sensors/t", "sensor.kitchen", "temp", float(i), "", "", "", "")
            for i in range(count)
        ]
    )


def _node(agent: _Agent) -> "NodeAgent":
    return cast("NodeAgent", agent)  # a fake standing in for the NodeAgent


class TestBothSidesAgree:
    async def test_every_row_arrives_once_and_in_order_across_pages(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main_history, "PAGE_BYTES", 2_000)
        _readings(db, 120)
        agent = _Agent(_main_answers)

        rows = await read_history(_node(agent), "ts", {}, hours=24, limit=1_000)

        assert rows is not None
        assert [row["value"] for row in rows] == [float(i) for i in range(120)]
        assert len(agent.asked) > 3, "the read should have taken several pages"

    async def test_the_window_is_fixed_by_the_first_page(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Recomputed from `hours` on each page, the window's start would move
        # forward between pages and the offsets would skip rows.
        monkeypatch.setattr(main_history, "PAGE_BYTES", 2_000)
        _readings(db, 60)
        agent = _Agent(_main_answers)

        await read_history(_node(agent), "ts", {}, hours=24, limit=1_000)

        since = {page.get("since") for page in agent.asked[1:]}
        assert len(since) == 1 and None not in since

    async def test_rows_written_during_the_read_do_not_shift_it(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main_history, "PAGE_BYTES", 2_000)
        _readings(db, 60)

        async def _while_writing(topic: str, payload: dict[str, Any], timeout: float) -> Any:
            reply = await _main_answers(topic, payload, timeout)
            # A reading taken now, written mid-read: it sorts after the rows
            # already there, so no page already read moves.
            _readings(db, 1, start=time.time())
            return reply

        rows = await read_history(_node(_Agent(_while_writing)), "ts", {}, hours=24, limit=1_000)

        assert rows is not None
        values = [row["value"] for row in rows]
        assert values[:60] == [float(i) for i in range(60)]

    async def test_the_limit_is_honoured(self, db: WactorzDB) -> None:
        _readings(db, 50)

        rows = await read_history(_node(_Agent(_main_answers)), "ts", {}, hours=24, limit=7)

        assert rows is not None
        assert [row["value"] for row in rows] == [float(i) for i in range(7)]

    async def test_filters_reach_the_database(self, db: WactorzDB) -> None:
        now = time.time()
        db.write_detection(now, "camera", "person", 0.91)
        db.write_detection(now, "camera", "cat", 0.4)
        db.write_ha_state(now, "light.hall", "off", "on", domain="light")
        db.write_ha_state(now, "switch.fan", "off", "on", domain="switch")
        agent = _node(_Agent(_main_answers))

        people = await read_history(agent, "detections", {"min_confidence": 0.5}, hours=1, limit=10)
        lights = await read_history(agent, "ha_states", {"domain": "light"}, hours=1, limit=10)

        assert [row["class_name"] for row in people or []] == ["person"]
        assert [row["entity_id"] for row in lights or []] == ["light.hall"]

    async def test_stats_are_read_whole(self, db: WactorzDB) -> None:
        _readings(db, 3)

        stats = await read_stats(_node(_Agent(_main_answers)))

        assert stats is not None
        assert stats["sensor_readings"] == 3


class TestWhatMainRefuses:
    @pytest.mark.parametrize(
        ("request_", "reason"),
        [
            ({"query": "users"}, "unknown query"),
            ({"query": None}, "unknown query"),
            ({"query": "ts", "filters": {"sql": "1=1"}}, "takes no filter"),
            ({"query": "ts", "filters": ["topic"]}, "filters must be"),
            ({"query": "ts", "filters": {"topic": 3}}, "topic must be"),
            ({"query": "ts", "filters": {"topic": "x" * 10_000}}, "out of range"),
            ({"query": "detections", "filters": {"min_confidence": "high"}}, "must be"),
            ({"query": "ts", "hours": -1}, "out of range"),
            ({"query": "ts", "hours": True}, "must be"),
            ({"query": "ts", "since": float("inf")}, "out of range"),
            ({"query": "ts", "offset": -5}, "out of range"),
            ({"query": "ts", "offset": 1.5}, "must be"),
            ({"query": "ts", "limit": 0}, "out of range"),
        ],
    )
    async def test_anything_a_query_does_not_name(
        self, db: WactorzDB, request_: dict[str, Any], reason: str
    ) -> None:
        reply = await answer_history(request_)

        assert reason in reply["error"]

    async def test_a_page_never_holds_more_than_main_reads_at_once(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main_history, "PAGE_ROWS", 10)
        _readings(db, 30)

        reply = await answer_history({"query": "ts", "limit": 1_000})

        assert len(reply["rows"]) == 10
        assert reply["next"] == 10

    async def test_without_a_database_there_is_nothing_to_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main_history, "get_db", lambda: None)

        assert (await answer_history({"query": "ts"}))["rows"] == []
        assert await answer_history({"query": "stats"}) == {"stats": {}}


class TestPagesFitAMessage:
    def test_a_page_stays_under_the_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main_history, "PAGE_BYTES", 500)
        rows = [{"value": i, "text": "x" * 40} for i in range(PAGE_ROWS)]

        page, used, skipped = fit_page(rows)

        assert len(json.dumps(page)) <= 500
        assert used == len(page) and skipped == 0

    def test_a_row_too_large_for_any_page_is_skipped_not_stuck_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main_history, "PAGE_BYTES", 500)
        rows = [{"text": "x" * 1_000}, {"value": 1}]

        page, used, skipped = fit_page(rows)

        assert page == [{"value": 1}]
        assert (used, skipped) == (2, 1)

    async def test_the_node_says_a_row_was_left_out(
        self,
        db: WactorzDB,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(main_history, "PAGE_BYTES", 1_000)
        db.write_ha_state(time.time(), "light.hall", "off", "on", attributes="x" * 5_000)
        db.write_ha_state(time.time(), "light.porch", "off", "on")

        with caplog.at_level(logging.WARNING, logger=node_history.__name__):
            rows = await read_history(
                _node(_Agent(_main_answers)), "ha_states", {}, hours=1, limit=10
            )

        assert [row["entity_id"] for row in rows or []] == ["light.porch"]
        assert "too large" in caplog.text


class TestWhenMainDoesNotAnswer:
    async def test_no_answer_is_no_rows_and_says_what_to_check(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _silence(_topic: str, _payload: dict[str, Any], _timeout: float) -> Any:
            return None

        with caplog.at_level(logging.WARNING, logger=node_history.__name__):
            rows = await read_history(_node(_Agent(_silence)), "ts", {}, hours=1, limit=10)

        assert rows is None
        assert "main/history_request" in caplog.text

    async def test_a_read_cut_short_hands_back_none_of_it(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Half the rows passed off as the whole is worse than none.
        monkeypatch.setattr(main_history, "PAGE_BYTES", 2_000)
        _readings(db, 60)
        pages = 0

        async def _then_silence(topic: str, payload: dict[str, Any], timeout: float) -> Any:
            nonlocal pages
            pages += 1
            return await _main_answers(topic, payload, timeout) if pages == 1 else None

        rows = await read_history(_node(_Agent(_then_silence)), "ts", {}, hours=24, limit=1_000)

        assert rows is None

    async def test_a_refusal_is_no_rows_and_says_why(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _refused(_topic: str, _payload: dict[str, Any], _timeout: float) -> Any:
            return {"error": "request not signed for this node"}

        with caplog.at_level(logging.WARNING, logger=node_history.__name__):
            rows = await read_history(_node(_Agent(_refused)), "ts", {}, hours=1, limit=10)
            stats = await read_stats(_node(_Agent(_refused)))

        assert rows is None and stats is None
        assert "not signed" in caplog.text


def test_both_sides_name_the_same_topic() -> None:
    assert node_history.HISTORY_TOPIC == main_history.HISTORY_TOPIC
