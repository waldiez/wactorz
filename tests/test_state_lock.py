"""One process at a time on a state directory.

A server or a node writes its memory back to the state directory as it runs,
so a second process on the same directory, or an import into it, would be
undone by the first one's next write. The running one holds a lock that ends
with it.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from wactorz import app as app_module
from wactorz.core.state_lock import LOCK_FILE, StateInUseError, StateLock, in_use
from wactorz.errors import StartupError
from wactorz.node.runner import NodeRunner

#: Holds the lock on the directory it is given until its input closes, and says
#: its process id once it has it. Its own, not the one `Popen` reports: on
#: Windows a venv's `python.exe` is a launcher that runs the interpreter as a
#: child, so the two differ.
HOLDER = """
import os
import sys
from wactorz.core.state_lock import StateLock
lock = StateLock(sys.argv[1])
lock.acquire()
print("held", os.getpid(), flush=True)
sys.stdin.read()
"""


class TestTheLock:
    def test_a_second_holder_is_refused_until_the_first_lets_go(self, tmp_path: Path) -> None:
        first, second = StateLock(tmp_path), StateLock(tmp_path)
        first.acquire()

        with pytest.raises(StateInUseError):
            second.acquire()

        first.release()
        second.acquire()
        second.release()

    def test_asking_does_not_take_it(self, tmp_path: Path) -> None:
        assert not in_use(tmp_path)
        lock = StateLock(tmp_path)
        lock.acquire()

        assert in_use(tmp_path)
        lock.release()
        assert not in_use(tmp_path)

    def test_a_lock_file_left_behind_locks_nothing(self, tmp_path: Path) -> None:
        (tmp_path / LOCK_FILE).write_text("12345")

        assert not in_use(tmp_path)

    def test_another_process_holding_it_is_seen_and_named(self, tmp_path: Path) -> None:
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            # Waits for the holder to say it has it, however slow the machine.
            said, pid = holder.stdout.readline().split()
            assert said == "held"

            assert in_use(tmp_path)
            with pytest.raises(StateInUseError, match=rf"process {pid}\b"):
                StateLock(tmp_path).acquire()
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=30)

        assert not in_use(tmp_path), "it ends with the process"


class TestWhatHoldsIt:
    async def test_the_server_will_not_start_on_a_directory_in_use(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = tmp_path / "state"
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
        other = StateLock(state)
        other.acquire()
        try:
            with pytest.raises(StartupError, match="another Wactorz process"):
                app_module._check_startable(argparse.Namespace(reload=False), handle_signals=False)
        finally:
            other.release()
            app_module._state_lock.release()

    async def test_a_node_will_not_start_on_a_directory_in_use(self, tmp_path: Path) -> None:
        other = StateLock(tmp_path)
        other.acquire()
        runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))
        try:
            with pytest.raises(StateInUseError):
                await runner.run()
        finally:
            other.release()
