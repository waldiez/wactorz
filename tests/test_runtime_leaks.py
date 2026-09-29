"""What an actor starts, it owns and ends; what it writes, it writes whole.

Work handed to the background with a bare ``asyncio.create_task`` kept no
reference, so it could be collected part-way through, and nothing cancelled it
when its actor stopped. A stream window holds a broker connection that
reconnects for ever, and stopping its agent left it open. Two threads saving the
same state file shared one temporary. And a migration wiped an agent's state
before writing the snapshot that replaced it, so a crash in between left none.
"""

import asyncio
import contextlib
import logging
import pickle
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core import atomic_io
from wactorz.core import mqtt as core_mqtt
from wactorz.core.actor import Actor, Message
from wactorz.core.persistence.api import SQLITE_KEYS, PersistenceAPI
from wactorz.core.persistence.db import WactorzDB
from wactorz.core.persistence.pickle_store import PickleStore
from wactorz.core.registry import ActorRegistry
from wactorz.core.topic_bus import StreamWindow


class _Worker(Actor):
    async def handle_message(self, message: Message) -> None:
        return None


@pytest.fixture(name="actor")
async def actor_fixture(tmp_path: Path) -> Any:
    actor = _Worker(name="worker", persistence_dir=str(tmp_path))
    await actor.start()
    yield actor
    await actor.stop()


class TestDetachedWork:
    async def test_it_is_held_while_it_runs_and_let_go_after(self, actor: Actor) -> None:
        release = asyncio.Event()

        task = actor.run_detached(release.wait(), name="waiting")

        assert task in actor._tasks
        release.set()
        await task
        await asyncio.sleep(0)
        assert task not in actor._tasks

    async def test_stopping_the_actor_cancels_it(self, tmp_path: Path) -> None:
        actor = _Worker(name="stopper", persistence_dir=str(tmp_path))
        await actor.start()
        task = actor.run_detached(asyncio.sleep(60))

        await actor.stop()

        assert task.cancelled()

    async def test_a_failure_is_logged_with_its_name(
        self, actor: Actor, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def fail() -> None:
            raise RuntimeError("extraction broke")

        with caplog.at_level(logging.ERROR):
            task = actor.run_detached(fail(), name="facts")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        assert "facts" in caplog.text and "extraction broke" in caplog.text

    async def test_work_handed_over_after_a_stop_is_not_started(self, tmp_path: Path) -> None:
        # stop() sets STOPPED before winding tasks down, so work added later
        # would be cleared from the list without ever being cancelled.
        actor = _Worker(name="late", persistence_dir=str(tmp_path))
        await actor.start()
        await actor.stop()
        ran: list[bool] = []

        async def work() -> None:
            ran.append(True)

        task = actor.run_detached(work())
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert ran == []
        assert task not in actor._tasks


class TestASpawnThatDoesNotStart:
    @pytest.mark.parametrize("how", ["raises", "is cancelled"])
    async def test_the_child_is_not_left_registered(self, tmp_path: Path, how: str) -> None:
        registry = ActorRegistry()
        parent = _Worker(name="parent", persistence_dir=str(tmp_path))
        parent._registry = registry
        started = asyncio.Event()

        class _Child(_Worker):
            async def on_start(self) -> None:
                started.set()
                if how == "raises":
                    raise RuntimeError("camera missing")
                await asyncio.sleep(60)

        spawn = asyncio.create_task(
            parent.spawn(_Child, name="child", persistence_dir=str(tmp_path))
        )
        await started.wait()
        if how == "is cancelled":
            spawn.cancel()
        await asyncio.gather(spawn, return_exceptions=True)

        assert registry.find_by_name("child") is None


class _Window:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class TestStreamWindows:
    async def test_stopping_the_agent_stops_its_windows(self, tmp_path: Path) -> None:
        agent = DynamicAgent(name="watcher", code="", persistence_dir=str(tmp_path))
        window = _Window()
        agent._api._windows["sensors/temp"] = window

        await agent.on_stop()

        assert window.stopped is True
        assert agent._api._windows == {}

    async def test_a_real_window_s_connection_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The reconnect loop is what leaks: it is the thing stop() must end.
        connected = asyncio.Event()

        class _Client:
            async def subscribe(self, _topic: str) -> None:
                connected.set()

            @property
            def messages(self) -> Any:
                async def never() -> Any:
                    await asyncio.Event().wait()
                    yield None

                return never()

        @contextlib.asynccontextmanager
        async def broker(*_args: Any, **_kwargs: Any) -> AsyncIterator[_Client]:
            yield _Client()

        monkeypatch.setattr(core_mqtt, "mqtt_client", broker)
        window = StreamWindow("sensors/temp", seconds=60).start("localhost", 1883)
        await asyncio.wait_for(connected.wait(), 2)
        task = window._task
        assert task is not None

        window.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert task.done()

    def test_one_that_will_not_stop_does_not_keep_the_others_open(self, tmp_path: Path) -> None:
        agent = DynamicAgent(name="watcher", code="", persistence_dir=str(tmp_path))

        class _Stuck:
            def stop(self) -> None:
                raise RuntimeError("already closed")

        after = _Window()
        agent._api._windows.update({"a": _Stuck(), "b": after})

        agent._api._close_windows()

        assert after.stopped is True


class TestTemporaryFiles:
    def test_each_write_gets_its_own(self, tmp_path: Path) -> None:
        target = tmp_path / "state.pkl"

        assert atomic_io._temporary(target) != atomic_io._temporary(target)

    def test_threads_saving_one_file_leave_it_whole(self, tmp_path: Path) -> None:
        target = tmp_path / "state.pkl"
        errors: list[BaseException] = []

        def save(n: int) -> None:
            try:
                for _ in range(50):
                    atomic_io.write_pickle(target, {"n": n, "pad": "x" * 10_000})
            except BaseException as exc:  # collected for the assert
                errors.append(exc)

        threads = [threading.Thread(target=save, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Windows refuses to replace a file another thread is replacing at that
        # moment: that save is lost and says so, and the file keeps the previous
        # contents whole (see write_pickle). Nothing else may go wrong.
        expected = (PermissionError,) if sys.platform == "win32" else ()
        assert [e for e in errors if not isinstance(e, expected)] == []
        with target.open("rb") as f:
            assert pickle.load(f)["pad"] == "x" * 10_000  # written just above
        assert list(tmp_path.glob(".*.tmp")) == []


@pytest.fixture(name="persistence")
def persistence_fixture(tmp_path: Path) -> Any:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    yield PersistenceAPI(db, PickleStore(str(tmp_path / "state")), "sensor")
    db.close()


DURABLE = next(iter(sorted(SQLITE_KEYS)))


class TestAMigratedSnapshot:
    def test_a_crash_before_the_old_keys_go_keeps_the_new_state(
        self, persistence: PersistenceAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        persistence.set(DURABLE, "old")
        persistence.set("stale", 1)

        def crash(_keep: Any) -> None:
            raise RuntimeError("power cut")

        monkeypatch.setattr(persistence, "_remove_all_but", crash)

        with pytest.raises(RuntimeError):
            persistence.load_snapshot({DURABLE: "migrated", "counter": 7})

        assert persistence.get(DURABLE) == "migrated"
        assert persistence.get("counter") == 7

    def test_what_the_snapshot_leaves_out_goes(self, persistence: PersistenceAPI) -> None:
        persistence.set(DURABLE, "old")
        persistence.set("stale", 1)

        persistence.load_snapshot({"counter": 7})

        assert persistence.all() == {"counter": 7}

    def test_a_key_that_fails_to_write_keeps_its_old_value(
        self, persistence: PersistenceAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deleting it would turn one failed write into a lost value.
        persistence.set(DURABLE, "old")
        real_set = persistence.db.kv_set

        def refuse(agent: str, key: str, value: Any) -> None:
            if key == DURABLE:
                raise TypeError("not serialisable")
            real_set(agent, key, value)

        monkeypatch.setattr(persistence.db, "kv_set", refuse)

        persistence.load_snapshot({DURABLE: "new", "counter": 7})

        assert persistence.get(DURABLE) == "old"
        assert persistence.get("counter") == 7

    def test_merging_keeps_the_pickled_keys_already_there(
        self, persistence: PersistenceAPI
    ) -> None:
        persistence.set("kept", 1)

        persistence.load_snapshot({"added": 2}, replace=False)

        assert persistence.all() == {"kept": 1, "added": 2}
