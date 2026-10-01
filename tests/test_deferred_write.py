"""A file is written a moment after it is asked for, off the event loop.

Replacing a file safely ends in forcing it to the disk, which on an SD card
takes tens of milliseconds whatever its size. On the event loop every actor
waits for that, each time any agent saves anything. So the write is put off for
a moment and done by a thread: the loop is not held, and what was asked for in
that moment goes out as one write.

What must still hold: the file ends up with the latest content, a write never
overtakes a later one, a file that was withdrawn does not come back, and
nothing waiting is lost at shutdown.
"""

import asyncio
import threading
from pathlib import Path

import pytest

from tests.waiting import quiet
from wactorz.core import deferred_write
from wactorz.core.deferred_write import DeferredWriter

#: The delay these writers are given: short, so a test is not spent waiting on it.
#: Nothing here depends on how long it is, only on what has happened by the
#: time the writer has gone quiet.
DELAY_S = 0.1


class _Writes:
    """Stands in for the write to disk: does it, and remembers where it ran."""

    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.threads: list[int] = []

    def __call__(self, path: Path, data: bytes) -> None:
        self.paths.append(path)
        self.threads.append(threading.get_ident())
        path.write_bytes(data)


@pytest.fixture(name="writes")
def writes_fixture(monkeypatch: pytest.MonkeyPatch) -> _Writes:
    writes = _Writes()
    monkeypatch.setattr(deferred_write, "write_bytes", writes)
    return writes


class TestWithNoLoopRunning:
    def test_the_write_happens_at_once(self, tmp_path: Path, writes: _Writes) -> None:
        # Nothing is being kept waiting, and nothing would write it later.
        target = tmp_path / "state"

        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"one")

        assert target.read_bytes() == b"one"


class TestUnderARunningLoop:
    async def test_the_loop_is_not_held_and_the_file_follows(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        target = tmp_path / "state"

        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"one")

        assert not target.exists()
        await quiet(writer)
        assert target.read_bytes() == b"one"

    async def test_the_write_runs_on_another_thread(self, tmp_path: Path, writes: _Writes) -> None:
        writer = DeferredWriter(DELAY_S)
        writer.submit(tmp_path / "state", lambda: b"one")

        await quiet(writer)

        assert writes.threads
        assert threading.get_ident() not in writes.threads

    async def test_changes_made_inside_the_delay_are_one_write_of_the_last(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)

        for number in range(50):
            writer.submit(target, lambda number=number: str(number).encode())
        await quiet(writer)

        assert writes.paths == [target]
        assert target.read_bytes() == b"49"

    async def test_the_content_is_asked_for_when_it_is_written(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        # So a state changed after the request, without asking again, is still
        # written as it is rather than as it was.
        target = tmp_path / "state"
        state = {"value": "early"}

        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: state["value"].encode())
        state["value"] = "late"
        await quiet(writer)

        assert target.read_bytes() == b"late"

    async def test_each_path_is_written(self, tmp_path: Path, writes: _Writes) -> None:
        writer = DeferredWriter(DELAY_S)

        writer.submit(tmp_path / "a", lambda: b"a")
        writer.submit(tmp_path / "b", lambda: b"b")
        await quiet(writer)

        assert sorted(path.name for path in writes.paths) == ["a", "b"]

    async def test_what_is_asked_for_during_a_write_goes_out_next(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)

        writer.submit(target, lambda: b"one")
        await quiet(writer)
        writer.submit(target, lambda: b"two")
        await quiet(writer)

        assert writes.paths == [target, target]
        assert target.read_bytes() == b"two"

    async def test_a_request_from_a_thread_is_written_too(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        # Agent code runs its blocking work on threads, and persists from them.
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)
        writer.submit(tmp_path / "first", lambda: b"x")  # the loop is known from here on

        await asyncio.to_thread(writer.submit, target, lambda: b"from a thread")
        await quiet(writer)

        assert target.read_bytes() == b"from a thread"

    async def test_content_that_cannot_be_produced_costs_only_its_own_file(
        self, tmp_path: Path, writes: _Writes, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _fails() -> bytes:
            raise ValueError("cannot be encoded")

        writer = DeferredWriter(DELAY_S)
        writer.submit(tmp_path / "bad", _fails)
        writer.submit(tmp_path / "good", lambda: b"good")
        await quiet(writer)

        assert (tmp_path / "good").read_bytes() == b"good"
        assert not (tmp_path / "bad").exists()
        assert "bad was not written: cannot be encoded" in caplog.text


class TestFlushing:
    async def test_everything_waiting_is_written_before_it_returns(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"one")

        writer.flush()

        assert target.read_bytes() == b"one"
        await quiet(writer)
        assert writes.paths == [target], "the delay finds nothing left to write"

    def test_with_nothing_waiting_it_does_nothing(self, writes: _Writes) -> None:
        DeferredWriter(DELAY_S).flush()

        assert writes.paths == []


class TestWritesToOnePathStayInOrder:
    async def test_an_earlier_write_that_arrives_late_is_dropped(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        # A write handed to a thread, then a flush on this one: the thread may
        # get to the disk second, and must not put the older content back.
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"older")
        late = writer._take()
        writer.submit(target, lambda: b"newer")
        writer.flush()

        writer._write(late)

        assert target.read_bytes() == b"newer"


class TestWithdrawingAPath:
    async def test_a_write_still_waiting_is_not_made(self, tmp_path: Path, writes: _Writes) -> None:
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"one")

        writer.discard(target)
        await quiet(writer)

        assert not target.exists()
        assert writes.paths == []

    async def test_a_write_already_handed_to_a_thread_does_not_bring_the_file_back(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"one")
        on_its_way = writer._take()

        writer.discard(target)
        writer._write(on_its_way)

        assert not target.exists()

    async def test_the_path_can_be_written_again_afterwards(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        target = tmp_path / "state"
        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"one")
        writer.discard(target)

        writer.submit(target, lambda: b"two")
        await quiet(writer)

        assert target.read_bytes() == b"two"


class TestSayingWhatAFileNowHolds:
    """Whoever tracks a file's content learns it from the write, not the request."""

    def test_it_is_told_the_bytes_once_they_are_written(self, tmp_path: Path) -> None:
        landed: list[bytes] = []

        writer = DeferredWriter(DELAY_S)
        writer.submit(tmp_path / "state", lambda: b"one", landed.append)

        assert landed == [b"one"]

    def test_it_is_not_told_of_a_write_that_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _full(path: Path, data: bytes) -> None:
            raise OSError("No space left on device")

        monkeypatch.setattr(deferred_write, "write_bytes", _full)
        landed: list[bytes] = []

        writer = DeferredWriter(DELAY_S)
        writer.submit(tmp_path / "state", lambda: b"one", landed.append)

        assert landed == []

    async def test_it_is_not_told_of_a_write_that_was_overtaken(self, tmp_path: Path) -> None:
        target = tmp_path / "state"
        landed: list[bytes] = []
        writer = DeferredWriter(DELAY_S)
        writer.submit(target, lambda: b"older", landed.append)
        late = writer._take()
        writer.submit(target, lambda: b"newer", landed.append)
        writer.flush()

        writer._write(late)

        assert landed == [b"newer"]


class TestAcrossEventLoops:
    def test_a_writer_that_outlives_a_loop_still_writes_under_the_next(
        self, tmp_path: Path, writes: _Writes
    ) -> None:
        # A timer left by a loop that has closed never fires; held on to, it
        # would stop every later write from being scheduled.
        writer = DeferredWriter(DELAY_S)

        async def _ask(name: str, wait: bool) -> None:
            writer.submit(tmp_path / name, name.encode)
            if wait:
                await quiet(writer)

        asyncio.run(_ask("first", wait=False))
        asyncio.run(_ask("second", wait=True))

        assert (tmp_path / "second").read_bytes() == b"second"


def test_the_real_write_replaces_the_file_whole(tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.write_bytes(b"before")

    writer = DeferredWriter(DELAY_S)
    writer.submit(target, lambda: b"after")

    assert target.read_bytes() == b"after"
    assert [path.name for path in tmp_path.iterdir()] == ["state"], "no temporary is left"
