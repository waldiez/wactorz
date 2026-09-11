"""Retention: what is old enough goes, and nothing else does.

Deleting is the one thing here that cannot be undone, so most of these pin what
must *stay*: a row inside its window, a file a kept message still names, a file
uploaded a moment ago and not yet sent, a name in the uploads directory that is
not ours. One property is not about what goes at all: a backlog is deleted in
batches, because every writer on the event loop waits on the lock a delete holds.
"""

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from wactorz import config, retention
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.persistence.db import WactorzDB
from wactorz.web import uploads

DAY = 86400.0
ID_A = "a" * 32
ID_B = "b" * 32


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    store = WactorzDB(str(tmp_path / "wactorz.db"))
    yield store
    store.close()


def _turn(db: WactorzDB, age_days: float, files: tuple[str, ...] = ()) -> None:
    attached = [{"id": f, "name": "shot.png", "mime": "image/png", "size": 3} for f in files]
    db.write_chat_log(
        ts=time.time() - age_days * DAY,
        agent_name="main",
        role="user",
        content=f"{age_days:g} days old",
        attachments=attached or None,
    )


def _aged(path: Path, age_days: float) -> None:
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


def _upload(root: Path, file_id: str, age_days: float) -> None:
    """A stored file as the upload endpoint leaves one: its bytes and its record."""
    root.mkdir(exist_ok=True)
    for name in (file_id, f"{file_id}.json"):
        (root / name).write_text("{}", encoding="utf-8")
        _aged(root / name, age_days)


class TestTheChatLog:
    def test_a_turn_past_the_window_goes_and_one_inside_it_stays(self, db: WactorzDB) -> None:
        _turn(db, 366)
        _turn(db, 364)

        assert db.prune_chat_log(365) == 1
        assert [r["content"] for r in db.query_chat_log()] == ["364 days old"]

    def test_a_backlog_goes_in_short_transactions(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(WactorzDB, "PRUNE_BATCH_ROWS", 3)
        for _ in range(7):
            _turn(db, 400)
        opened: list[int] = []
        original = WactorzDB.transaction

        def counting(self: WactorzDB) -> object:
            opened.append(1)
            return original(self)

        monkeypatch.setattr(WactorzDB, "transaction", counting)

        assert db.prune_chat_log(365) == 7
        # 3 + 3 + 1: the lock is let go between batches, not held for the lot.
        assert len(opened) == 3
        assert db.query_chat_log() == []

    def test_pruning_alongside_writes_loses_none_of_them(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(WactorzDB, "PRUNE_BATCH_ROWS", 5)
        for _ in range(200):
            _turn(db, 400)
        errors: list[BaseException] = []

        def prune() -> None:
            try:
                db.prune_chat_log(365)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=prune)
        worker.start()
        for _ in range(100):
            _turn(db, 0)
        worker.join(timeout=30)

        assert not worker.is_alive()
        assert errors == []
        kept = db.query_chat_log(limit=1000)
        assert len(kept) == 100
        assert {r["content"] for r in kept} == {"0 days old"}

    def test_the_files_a_kept_turn_names_are_what_it_lists(self, db: WactorzDB) -> None:
        _turn(db, 1, (ID_A,))
        _turn(db, 2)
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO chat_log (ts, agent_name, role, content, attachments) "
                "VALUES (?, 'main', 'user', 'unreadable', 'not json')",
                (time.time(),),
            )

        assert db.chat_attachment_ids() == {ID_A}


class TestTheTimeSeries:
    def test_a_reading_past_the_window_goes_and_one_inside_it_stays(self, db: WactorzDB) -> None:
        now = time.time()
        db.write_sensor(now - 400 * DAY, "sensors/t", "sensor.t", "temp", 1.0)
        db.write_sensor(now - 10 * DAY, "sensors/t", "sensor.t", "temp", 2.0)

        assert db.prune_old_data(365) == 1
        assert db.stats()["sensor_readings"] == 1


class TestTheUploadSweep:
    def test_an_unreferenced_file_goes_with_its_record(self, tmp_path: Path) -> None:
        root = tmp_path / "uploads"
        _upload(root, ID_A, 2)

        assert uploads.sweep(set(), state_dir=str(tmp_path)) == 1
        assert list(root.iterdir()) == []

    def test_a_file_a_kept_turn_names_stays(self, tmp_path: Path) -> None:
        root = tmp_path / "uploads"
        _upload(root, ID_A, 400)

        assert uploads.sweep({ID_A}, state_dir=str(tmp_path)) == 0
        assert sorted(p.name for p in root.iterdir()) == [ID_A, f"{ID_A}.json"]

    def test_a_file_not_yet_sent_stays(self, tmp_path: Path) -> None:
        root = tmp_path / "uploads"
        _upload(root, ID_A, 0.5)  # inside the day a file is given to be sent

        assert uploads.sweep(set(), state_dir=str(tmp_path)) == 0
        assert (root / ID_A).exists()

    def test_what_an_interrupted_upload_left_goes(self, tmp_path: Path) -> None:
        root = tmp_path / "uploads"
        root.mkdir()
        part = root / f".{ID_B}.part"
        part.write_bytes(b"half")
        _aged(part, 2)

        assert uploads.sweep(set(), state_dir=str(tmp_path)) == 1
        assert not part.exists()

    def test_a_name_that_is_not_ours_is_left_alone(self, tmp_path: Path) -> None:
        root = tmp_path / "uploads"
        root.mkdir()
        stray = root / "notes.txt"
        stray.write_text("mine", encoding="utf-8")
        _aged(stray, 400)

        assert uploads.sweep(set(), state_dir=str(tmp_path)) == 0
        assert stray.exists()

    def test_with_no_uploads_there_is_nothing_to_do(self, tmp_path: Path) -> None:
        assert uploads.sweep(set(), state_dir=str(tmp_path)) == 0
        assert not (tmp_path / "uploads").exists()


class TestTheJob:
    @pytest.fixture(autouse=True)
    def _wired(self, db: WactorzDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(retention, "_last_run", None)
        monkeypatch.setattr(retention, "get_db", lambda: db)
        monkeypatch.setattr(uploads, "resolve_state_dir", lambda _state_dir=None: str(tmp_path))

    def test_a_file_whose_last_turn_is_pruned_goes_in_the_same_run(
        self, db: WactorzDB, tmp_path: Path
    ) -> None:
        _turn(db, 400, (ID_A,))
        _upload(tmp_path / "uploads", ID_A, 400)

        assert retention.prune() == {"timeseries": 0, "chat": 1, "uploads": 1}

    def test_zero_keeps_the_chat_log_and_its_files(
        self, db: WactorzDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "RETENTION_CHAT_DAYS", 0)
        _turn(db, 4000, (ID_A,))
        _upload(tmp_path / "uploads", ID_A, 4000)

        done = retention.prune()

        assert "chat" not in done
        assert done["uploads"] == 0
        assert len(db.query_chat_log()) == 1

    def test_it_runs_at_most_hourly(self) -> None:
        assert retention.prune() != {}
        assert retention.prune() == {}

    def test_without_a_database_nothing_is_swept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing could say which files are still referenced, so none may go.
        monkeypatch.setattr(retention, "get_db", lambda: None)
        _upload(tmp_path / "uploads", ID_A, 400)

        assert retention.prune() == {}
        assert (tmp_path / "uploads" / ID_A).exists()


class TestTheOutbox:
    @staticmethod
    def _publisher(tmp_path: Path, days: float = 7.0) -> MQTTPublisher:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"), dead_letter_days=days)
        pub._init_db()
        return pub

    @staticmethod
    def _stored(tmp_path: Path, topic: str, age_days: float) -> None:
        with sqlite3.connect(str(tmp_path / "outbox.db")) as db:
            db.execute(
                "INSERT INTO outbox (topic, payload, retain, qos, ts) VALUES (?, '{}', 0, 1, ?)",
                (topic, time.time() - age_days * DAY),
            )

    @staticmethod
    def _replayed(pub: MQTTPublisher) -> list[str]:
        pub._load_pending_from_db()
        topics = []
        while not pub._queue.empty():
            topics.append(pub._queue.get_nowait()[0])
        return topics

    async def test_an_undelivered_message_expires_and_its_topic_is_named(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pub = self._publisher(tmp_path)
        self._stored(tmp_path, "nodes/pi/spawn", 8)
        self._stored(tmp_path, "agents/x/status", 1)

        with caplog.at_level(logging.WARNING):
            pub._expire()

        assert self._replayed(pub) == ["agents/x/status"]
        assert "nodes/pi/spawn" in caplog.text
        pub._close_db()

    async def test_zero_keeps_every_message(self, tmp_path: Path) -> None:
        pub = self._publisher(tmp_path, days=0)
        self._stored(tmp_path, "nodes/pi/spawn", 4000)

        pub._expire()

        assert self._replayed(pub) == ["nodes/pi/spawn"]
        pub._close_db()
