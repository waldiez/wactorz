"""Housekeeping runs on a timer, off the loop, and cannot take the loop with it.

The reason this exists at all is that SQLite folds its WAL back inline, on
whichever write crosses the page threshold — and on an SD card that is ~69ms with
every actor in the process stopped. So the property under test is not "the job
ran"; it is "the job ran *somewhere else*", plus the failure behaviour that lets
a broken job be survivable rather than silently fatal.
"""

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from wactorz.core.persistence import maintenance
from wactorz.core.persistence.db import WactorzDB


@pytest.fixture(autouse=True)
def _clean_registry():
    """The module holds process-wide state; no test may leak into the next."""
    maintenance._JOBS.clear()
    maintenance._task = None
    maintenance._stopping = None
    yield
    maintenance._JOBS.clear()
    maintenance._task = None
    maintenance._stopping = None


class TestTheRotation:
    async def test_a_job_runs_off_the_event_loop(self) -> None:
        # The whole point. A job that ran here would be the stall this replaces.
        loop_thread = threading.get_ident()
        seen: list[int] = []
        maintenance.register("probe", lambda: seen.append(threading.get_ident()))

        await maintenance._run_once()

        assert seen and all(t != loop_thread for t in seen)

    async def test_one_failing_job_does_not_stop_the_others(self) -> None:
        # Housekeeping failing is not a reason to stop housekeeping.
        ran: list[str] = []

        def boom() -> None:
            raise RuntimeError("disk on fire")

        maintenance.register("boom", boom)
        maintenance.register("after", lambda: ran.append("after"))

        await maintenance._run_once()

        assert ran == ["after"]

    async def test_registering_the_same_name_replaces_it(self) -> None:
        ran: list[str] = []
        maintenance.register("job", lambda: ran.append("first"))
        maintenance.register("job", lambda: ran.append("second"))

        await maintenance._run_once()

        assert ran == ["second"]

    async def test_the_checkpoint_job_is_harmless_before_a_database_exists(self) -> None:
        # Startup order is not something this module gets to assume.
        assert maintenance._checkpoint_db() is None


class TestStartAndStop:
    async def test_start_is_idempotent(self) -> None:
        maintenance.start()
        first = maintenance._task
        maintenance.start()

        assert maintenance._task is first
        await maintenance.stop()

    async def test_start_registers_the_checkpoint(self) -> None:
        maintenance.start()
        try:
            assert "wal-checkpoint" in maintenance._JOBS
        finally:
            await maintenance.stop()

    async def test_stop_is_idempotent_and_leaves_nothing_running(self) -> None:
        maintenance.start()
        await maintenance.stop()
        await maintenance.stop()

        assert maintenance._task is None

    async def test_a_cancelled_loop_stays_cancelled(self) -> None:
        """It must not swallow the cancellation and run a job anyway.

        The failure this guards is subtle: `wait_for` on Python 3.10 can return
        from inside its own CancelledError handling, so a loop written with it
        would start a job *after* being cancelled — putting a thread on the
        connection lock during shutdown.
        """
        ran: list[str] = []
        maintenance.register("job", lambda: ran.append("job"))
        maintenance.INTERVAL_S, original = 0.01, maintenance.INTERVAL_S
        try:
            stopping = asyncio.Event()
            task = asyncio.create_task(maintenance._loop(stopping))
            await asyncio.sleep(0)  # let it reach the wait
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            maintenance.INTERVAL_S = original

        assert ran == [], "a job ran after the loop was cancelled"

    async def test_stop_waits_for_a_job_in_flight(self) -> None:
        # It holds the connection lock while it runs, and shutdown is about to
        # want that lock to write actor state out.
        finished = threading.Event()
        started = threading.Event()

        def slow() -> None:
            started.set()
            time.sleep(0.2)
            finished.set()

        maintenance.register("slow", slow)
        maintenance.INTERVAL_S, original = 0.01, maintenance.INTERVAL_S
        try:
            maintenance.start()
            await asyncio.to_thread(started.wait, 2.0)
            await maintenance.stop()
        finally:
            maintenance.INTERVAL_S = original

        assert finished.is_set(), "stop() returned while a job was still on a thread"


class TestTheCheckpointItself:
    def test_it_folds_the_wal_away(self, tmp_path: Path) -> None:
        db = WactorzDB(str(tmp_path / "w.db"))
        for i in range(200):
            db.kv_set("agent", f"k{i}", "x" * 500)
        wal = tmp_path / "w.db-wal"
        assert wal.stat().st_size > 0

        before = db.checkpoint()

        assert before > 0
        assert wal.stat().st_size < before
        db.close()

    def test_the_inline_threshold_is_raised_but_not_disabled(self, tmp_path: Path) -> None:
        # 0 would make the maintenance task the only checkpointer, and a task
        # that dies would then grow the WAL until the disk filled.
        db = WactorzDB(str(tmp_path / "w.db"))

        pages = db.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]

        assert pages == WactorzDB.WAL_AUTOCHECKPOINT_PAGES
        assert pages > 1000, "the default is what fires inline; it has to be beaten"
        assert pages != 0
        db.close()

    def test_the_data_survives_the_fold(self, tmp_path: Path) -> None:
        db = WactorzDB(str(tmp_path / "w.db"))
        db.kv_set("agent", "keep", "value")

        db.checkpoint()

        assert db.kv_get("agent", "keep") == "value"
        # And from a second connection, which is what a restart amounts to.
        db.close()
        with sqlite3.connect(str(tmp_path / "w.db")) as conn:
            row = conn.execute("SELECT COUNT(*) FROM kv_store WHERE key='keep'").fetchone()
        assert row[0] == 1
