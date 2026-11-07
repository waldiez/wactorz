# SPDX-License-Identifier: Apache-2.0
"""The writer that keeps what broker messages and chat turns write, off the event loop.

Against a real database: what is checked is what ends up on disk, in what order,
and what is said when some of it cannot be.
"""

import asyncio
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.persistence.db import ChatRow, WactorzDB
from wactorz.monitoring import chat_metrics
from wactorz.web import cost, db_writer, runtime
from wactorz.web.db_writer import DbWriter

LEDGER = cost.LIFETIME_LEDGER_KEY


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    monkeypatch.setattr(runtime, "db", db)
    yield db
    db.close()


def _row(content: str, ts: float = 1.0, agent: str = "main") -> ChatRow:
    return ChatRow(ts=ts, agent_name=agent, agent_id=agent, role="user", content=content)


def _contents(db: WactorzDB) -> list[str]:
    return [r["content"] for r in reversed(db.query_chat_log(limit=1000))]


def _dropped() -> float:
    return chat_metrics.ROWS_DROPPED._value.get()  # pyright: ignore[reportAttributeAccessIssue]


class TestChatRows:
    async def test_rows_are_kept_in_the_order_they_were_handed_over(self, db: WactorzDB) -> None:
        writer = DbWriter()
        for n in range(5):
            writer.chat(_row(str(n), ts=float(n)))

        await writer.settled()

        assert _contents(db) == ["0", "1", "2", "3", "4"]

    async def test_rows_with_one_time_read_back_in_the_order_they_were_written(
        self, db: WactorzDB
    ) -> None:
        writer = DbWriter()
        for word in ("first", "second", "third"):
            writer.chat(_row(word, ts=5.0))

        await writer.settled()

        assert _contents(db) == ["first", "second", "third"]

    async def test_a_batch_is_one_transaction(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        batches: list[int] = []
        real = db.write_chat_logs

        def counted(rows: list[ChatRow]) -> None:
            batches.append(len(rows))
            real(rows)

        monkeypatch.setattr(db, "write_chat_logs", counted)
        writer = DbWriter()
        for n in range(10):
            writer.chat(_row(str(n)))

        await writer.settled()

        assert sum(batches) == 10 and len(batches) < 10, batches

    async def test_one_row_the_database_refuses_costs_only_itself(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        real = db.write_chat_logs

        def refusing(rows: list[ChatRow]) -> None:
            if any(r.content == "bad" for r in rows):
                raise ValueError("refused")
            real(rows)

        monkeypatch.setattr(db, "write_chat_logs", refusing)
        writer = DbWriter()
        for word in ("a", "bad", "b"):
            writer.chat(_row(word))

        with caplog.at_level(logging.WARNING, logger="wactorz.web.db_writer"):
            await writer.settled()

        assert _contents(db) == ["a", "b"]
        assert "not kept" in caplog.text

    async def test_the_writer_carries_on_after_a_failure(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = db.write_chat_logs
        calls = [0]

        def failing_once(rows: list[ChatRow]) -> None:
            calls[0] += 1
            if calls[0] <= 2:
                raise OSError("disk full, for now")
            real(rows)

        monkeypatch.setattr(db, "write_chat_logs", failing_once)
        writer = DbWriter()
        writer.chat(_row("lost"))
        await writer.settled()
        writer.chat(_row("kept"))
        await writer.settled()

        assert _contents(db) == ["kept"]


class TestAFullChatLane:
    async def test_past_the_row_cap_a_row_is_dropped_and_counted(self, db: WactorzDB) -> None:
        writer = DbWriter(max_rows=2)
        before = _dropped()

        for word in ("a", "b", "c"):
            writer.chat(_row(word))
        await writer.settled()

        assert _contents(db) == ["a", "b"]
        assert _dropped() == before + 1

    async def test_past_the_byte_budget_a_row_is_dropped(self, db: WactorzDB) -> None:
        writer = DbWriter(max_bytes=10)

        writer.chat(_row("12345"))
        writer.chat(_row("1234567890"))
        await writer.settled()

        assert _contents(db) == ["12345"]

    @pytest.mark.parametrize("since_boot", [5.0, 100_000.0])
    async def test_dropping_many_says_so_once_a_while(
        self,
        db: WactorzDB,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        since_boot: float,
    ) -> None:
        # The clock counts from the machine's boot: on one booted seconds ago, as a
        # CI runner or a Pi just started can be, the first drop is said all the same.
        monkeypatch.setattr(db_writer.time, "monotonic", lambda: since_boot)
        writer = DbWriter(max_rows=1)

        with caplog.at_level(logging.WARNING, logger="wactorz.web.db_writer"):
            for n in range(50):
                writer.chat(_row(str(n)))

        assert caplog.text.count("falling behind") == 1

    async def test_a_delete_is_taken_when_the_chat_lane_is_full(self, db: WactorzDB) -> None:
        db.kv_set("_system", LEDGER, {"old": 1.0})
        writer = DbWriter(max_rows=1)
        writer.chat(_row("fills it"))
        writer.chat(_row("dropped"))

        writer.kv_delete("_system", LEDGER)
        await writer.settled()

        assert db.kv_get("_system", LEDGER) is None


class TestKeyValueWrites:
    async def test_the_newest_value_of_a_key_is_what_is_kept(self, db: WactorzDB) -> None:
        writer = DbWriter()
        ledger = {"a": 1.0}
        writer.kv_set("_system", LEDGER, ledger)
        ledger["a"] = 2.0
        writer.kv_set("_system", LEDGER, ledger)
        ledger["a"] = 3.0

        await writer.settled()

        # A copy taken when queued: the last change, made after, is not written.
        assert db.kv_get("_system", LEDGER) == {"a": 2.0}

    async def test_a_save_after_a_delete_is_never_merged_into_one_before_it(
        self, db: WactorzDB
    ) -> None:
        writer = DbWriter()
        writer.kv_set("_system", LEDGER, {"before": 1.0})
        writer.kv_delete("_system", LEDGER)
        writer.kv_set("_system", LEDGER, {"after": 2.0})

        await writer.settled()

        assert db.kv_get("_system", LEDGER) == {"after": 2.0}

    async def test_a_delete_after_a_save_wins(self, db: WactorzDB) -> None:
        writer = DbWriter()
        writer.kv_set("_system", LEDGER, {"before": 1.0})
        writer.kv_delete("_system", LEDGER)

        await writer.settled()

        assert db.kv_get("_system", LEDGER) is None

    async def test_a_failed_write_is_said_and_the_next_still_goes(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        real = db.kv_set

        def refusing(agent: str, key: str, value: Any) -> None:
            if key == "broken":
                raise OSError("refused")
            real(agent, key, value)

        monkeypatch.setattr(db, "kv_set", refusing)
        writer = DbWriter()
        writer.kv_set("_system", "broken", 1)
        writer.kv_set("_system", "fine", 2)

        with caplog.at_level(logging.WARNING, logger="wactorz.web.db_writer"):
            await writer.settled()

        assert db.kv_get("_system", "fine") == 2
        assert "broken was not written" in caplog.text


def _slow_writes(
    db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Event, threading.Event]:
    """Make chat writes wait, on their thread, until released."""
    started, release = threading.Event(), threading.Event()
    real = db.write_chat_logs

    def slow(rows: list[ChatRow]) -> None:
        started.set()
        release.wait(timeout=10)
        real(rows)

    monkeypatch.setattr(db, "write_chat_logs", slow)
    return started, release


class TestWaiting:
    async def test_settled_waits_for_the_write_in_progress(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started, release = _slow_writes(db, monkeypatch)
        writer = DbWriter()
        writer.chat(_row("slow"))
        waiting = asyncio.ensure_future(writer.settled())
        await asyncio.to_thread(started.wait, 10)

        await asyncio.sleep(0.05)
        assert not waiting.done(), "settled before the write it waited for finished"
        release.set()
        await waiting

        assert _contents(db) == ["slow"]

    async def test_settled_waits_for_a_save_merged_into_one_it_was_waiting_for(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A later save of the key replaces the value of the one queued, not its
        # place: whoever was already waiting for that write still waits for it.
        started, release = _slow_writes(db, monkeypatch)
        saving, saved = threading.Event(), threading.Event()
        real_set = db.kv_set

        def held_set(agent: str, key: str, value: Any) -> None:
            saving.set()
            saved.wait(timeout=10)
            real_set(agent, key, value)

        monkeypatch.setattr(db, "kv_set", held_set)
        writer = DbWriter()
        writer.chat(_row("holds the writer"))
        await asyncio.to_thread(started.wait, 10)
        writer.kv_set("_system", LEDGER, {"a": 1.0})
        waiting = asyncio.ensure_future(writer.settled())
        await asyncio.sleep(0)
        writer.kv_set("_system", LEDGER, {"a": 2.0})
        release.set()
        await asyncio.to_thread(saving.wait, 10)

        await asyncio.sleep(0.05)
        assert not waiting.done(), "settled returned while the save it waited for was running"
        saved.set()
        await waiting

        assert db.kv_get("_system", LEDGER) == {"a": 2.0}


class TestShutdown:
    async def test_what_is_queued_is_written_before_it_stops(self, db: WactorzDB) -> None:
        writer = DbWriter()
        writer.chat(_row("last words"))
        writer.kv_set("_system", LEDGER, {"a": 1.0})

        await writer.close()

        assert _contents(db) == ["last words"]
        assert db.kv_get("_system", LEDGER) == {"a": 1.0}

    async def test_a_row_handed_over_while_stopping_is_dropped(self, db: WactorzDB) -> None:
        writer = DbWriter()
        closing = asyncio.ensure_future(writer.close())
        await asyncio.sleep(0)
        writer.chat(_row("too late"))
        await closing

        assert _contents(db) == []

    async def test_past_its_time_it_gives_up_the_queue_but_waits_for_the_write_running(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        started, release = _slow_writes(db, monkeypatch)
        writer = DbWriter()
        writer.chat(_row("in flight"))
        await asyncio.to_thread(started.wait, 10)
        writer.kv_set("_system", LEDGER, {"queued": 1.0})
        threading.Timer(0.3, release.set).start()

        with caplog.at_level(logging.WARNING, logger="wactorz.web.db_writer"):
            await writer.close(timeout=0.05)

        # The write that was running finished before close returned: the
        # database closes next, and must not close under it.
        assert _contents(db) == ["in flight"]
        assert db.kv_get("_system", LEDGER) is None
        assert "1 other write(s) not kept" in caplog.text

    async def test_it_can_start_again_after_stopping(self, db: WactorzDB) -> None:
        writer = DbWriter()
        await writer.close()

        writer.chat(_row("again"))
        await writer.settled()

        assert _contents(db) == ["again"]


class TestTheTotalsCache:
    async def test_an_older_refresh_finishing_last_does_not_replace_a_newer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release_old = threading.Event()
        answers = iter(
            [
                (release_old, cost.StoredTotals(alltime=1.0)),
                (None, cost.StoredTotals(alltime=2.0)),
            ]
        )

        def read() -> cost.StoredTotals:
            wait, totals = next(answers)
            if wait is not None:
                wait.wait(timeout=10)
            return totals

        monkeypatch.setattr(cost, "read_stored_totals", read)
        older = asyncio.ensure_future(cost.refresh_totals())
        await asyncio.sleep(0.05)
        await cost.refresh_totals()
        release_old.set()
        await older

        assert cost.stored_totals().alltime == 2.0

    async def test_settled_totals_read_after_the_queued_writes(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db.kv_set("gone", "_final_cost", {"name": "gone", "cost_usd": 0.5})
        await cost.refresh_totals()
        assert cost.historical_cost_usd(set()) == 0.5
        db_writer.writer.kv_delete("gone", "_final_cost")

        await cost.settled_totals()

        assert cost.historical_cost_usd(set()) == 0.0
