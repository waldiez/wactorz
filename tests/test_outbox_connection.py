"""The outbox keeps one connection, and knows when to let go of it.

A QoS-1 message costs two statements, and each used to open, write, commit and
close a connection of its own. On the SD card a Pi runs from that is ~16ms of
event loop per message — paid on the control plane, since `nodes/` and
`agents/by-name/` are forced up to QoS 1, so every spawn, stop and migration ack
stopped every actor in the process for that long.

Keeping the handle and setting the pragmas are one fix, not two: WAL on a
per-statement connection is *slower* than the default it replaces, and a kept
handle at `synchronous=FULL` still fsyncs on every commit. Both together are the
316x. So these tests pin both halves, and the failure path that a kept handle
introduces — one connection per statement healed itself, and a kept one has to
be told to.
"""

import asyncio
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from wactorz.core import mqtt_publisher as publisher_module
from wactorz.core.mqtt_publisher import MQTTPublisher


def _ready(tmp_path: Path) -> MQTTPublisher:
    pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
    pub._init_db()
    pub._available = True
    return pub


def _rows(db_path: str | os.PathLike[str]) -> int:
    with sqlite3.connect(str(db_path)) as db:
        return int(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])


class TestTheConnectionIsKept:
    async def test_two_writes_open_the_database_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pub = _ready(tmp_path)
        opens = 0
        real = sqlite3.connect

        def counting(*args: object, **kwargs: object) -> sqlite3.Connection:
            nonlocal opens
            opens += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(publisher_module.sqlite3, "connect", counting)

        await pub.publish("nodes/alpha/spawn", "one", qos=1)
        await pub.publish("nodes/alpha/spawn", "two", qos=1)

        assert opens == 0, "the handle from _init_db must be reused, not reopened"
        assert _rows(pub._db_path) == 2

    def test_the_pragmas_are_actually_in_effect(self, tmp_path: Path) -> None:
        # Asserted on what SQLite reports, not on the statements having been
        # issued: a pragma that failed to apply would still have been executed.
        db = _ready(tmp_path)._connect()

        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


class TestAFailedStatementLetsGoOfTheHandle:
    """A connection per statement healed itself; a kept one has to be told to."""

    async def test_a_failed_write_drops_the_handle(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)
        pub._connect().close()  # as if the handle had gone bad underneath us

        await pub.publish("nodes/alpha/spawn", "payload", qos=1)

        assert pub._db is None, "a bad handle kept is every later write failing too"

    async def test_the_next_write_reopens_and_succeeds(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)
        pub._connect().close()
        await pub.publish("nodes/alpha/spawn", "lost", qos=1)

        await pub.publish("nodes/alpha/spawn", "landed", qos=1)

        assert _rows(pub._db_path) == 1, "a transient fault must not become permanent"

    def test_a_failed_delete_drops_the_handle(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)
        pub._connect().close()

        pub._delete_from_db(1)

        assert pub._db is None

    def test_a_failed_load_drops_the_handle(self, tmp_path: Path) -> None:
        db = tmp_path / "outbox.db"
        db.write_bytes(b"this is not a database")
        pub = MQTTPublisher(db_path=str(db))

        pub._load_pending_from_db()

        assert pub._db is None
        assert pub.queue_depth == 0


class TestShutdown:
    async def test_disconnect_closes_the_handle_and_checkpoints(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)
        await pub.publish("nodes/alpha/spawn", "payload", qos=1)
        assert Path(f"{pub._db_path}-wal").exists(), "WAL mode should have produced one"

        await pub.disconnect()

        assert pub._db is None
        assert not Path(f"{pub._db_path}-wal").exists(), (
            "closing checkpoints the WAL back into the database"
        )


class TestTheCheckpointStaysOffTheLoop:
    """Left to SQLite, the checkpoint fires inline on whichever commit trips the
    page threshold. On an SD card that is ~62ms with every actor in the process
    stopped for it — rarer than the fsync this class stopped paying, and worse.
    """

    def test_the_inline_threshold_is_raised_but_not_disabled(self, tmp_path: Path) -> None:
        # Not zero: with no inline checkpoint at all, a background thread that
        # dies leaves the WAL growing without bound on the card.
        db = _ready(tmp_path)._connect()

        pages = db.execute("PRAGMA wal_autocheckpoint").fetchone()[0]

        assert pages == MQTTPublisher.WAL_AUTOCHECKPOINT_PAGES
        assert pages > 1000, "the default is what fires inline; it has to be beaten"
        assert pages != 0, "0 would make the background thread the only checkpointer"

    async def test_checkpointing_folds_the_wal_away(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)
        for i in range(20):
            await pub.publish("nodes/alpha/spawn", f"payload-{i}", qos=1)
        before = Path(f"{pub._db_path}-wal").stat().st_size
        assert before > 0

        await asyncio.to_thread(pub._checkpoint)

        assert Path(f"{pub._db_path}-wal").stat().st_size < before

    async def test_the_checkpoint_runs_off_the_event_loop(self, tmp_path: Path) -> None:
        # The whole point: whichever thread folds the WAL back in, it is not the
        # one running everyone else's coroutines.
        pub = _ready(tmp_path)
        await pub.publish("nodes/alpha/spawn", "payload", qos=1)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = pub._checkpoint

        def recording() -> None:
            seen.append(threading.get_ident())
            real()

        pub._checkpoint = recording  # type: ignore[method-assign]
        pub.CHECKPOINT_INTERVAL_S = 0.01  # type: ignore[misc]
        task = asyncio.create_task(pub._checkpoint_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert seen, "the timer never fired"
        assert all(t != loop_thread for t in seen)

    async def test_disconnect_stops_the_checkpoint_timer(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)
        pub._checkpoint_task = asyncio.create_task(pub._checkpoint_loop())

        await pub.disconnect()

        assert pub._checkpoint_task is None


class TestReplayStillWorksUnderWAL:
    async def test_undelivered_rows_replay_from_a_live_wal(self, tmp_path: Path) -> None:
        # The rows have to still be *in* the WAL when the restart happens, or
        # this exercises the old journal behaviour and would pass either way.
        first = _ready(tmp_path)
        await first.publish("nodes/alpha/spawn", "payload", qos=1)
        assert Path(f"{first._db_path}-wal").exists()

        second = MQTTPublisher(db_path=first._db_path)
        second._load_pending_from_db()

        assert second.queue_depth == 1
        topic, payload, _retain, qos, row_id = second._queue.get_nowait()
        assert (topic, payload, qos) == ("nodes/alpha/spawn", "payload", 1)
        assert row_id > 0


class TestOnlyTheControlPlanePays:
    async def test_telemetry_never_reaches_the_outbox(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)

        await pub.publish("agents/a1/heartbeat", "beat", qos=1)

        assert _rows(pub._db_path) == 0, "telemetry is forced to QoS 0 before the outbox"
        assert pub.queue_depth == 1

    async def test_a_nodes_topic_is_forced_into_it(self, tmp_path: Path) -> None:
        pub = _ready(tmp_path)

        await pub.publish("nodes/alpha/spawn", "payload", qos=0)

        assert _rows(pub._db_path) == 1, "the control plane is upgraded to QoS 1"
