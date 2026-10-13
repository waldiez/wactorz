"""Which process is running on a state directory.

A server or a node keeps much of its state in memory and writes it back to the
state directory as it goes, so anything that replaces the directory while one
runs is undone by its next write: an import, say, or a second server started
on the same directory by mistake.

So a running process holds an exclusive lock on a file in the directory, and
whatever must not run beside it asks for the lock first. The lock is the
operating system's, on an open file: it ends with the process however the
process ends, crash and `kill -9` included, so a stale lock file left behind
locks nothing.
"""

import contextlib
import os
import sys
from pathlib import Path
from typing import IO

# The two ways to lock a file, one per family of systems: imported at module
# level for whichever this is, since the other does not exist here.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

#: The file held locked, in the state directory.
LOCK_FILE = ".wactorz.lock"

#: Where Windows locks the file, which it does by byte range, and a locked
#: range cannot be read by anyone else: well past the process id written at the
#: start, so the message saying who holds it can still read it.
_WINDOWS_LOCK_AT = 1 << 30


class StateInUseError(RuntimeError):
    """Another process is running on the state directory."""

    def __init__(self, directory: Path, holder: str) -> None:
        who = f" (process {holder})" if holder else ""
        super().__init__(
            f"another Wactorz process{who} is running on {directory}. Stop it first, or "
            "point this one at another state directory (WACTORZ_STATE_DIR)."
        )


def _try_lock(file: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            # The position is where Windows locks from.
            file.seek(_WINDOWS_LOCK_AT)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(file: IO[bytes]) -> None:
    if sys.platform == "win32":
        file.seek(_WINDOWS_LOCK_AT)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _holder(path: Path) -> str:
    """The process id the holder wrote, for the message; "" when it cannot be read."""
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""


class StateLock:
    """The lock on one state directory, held from :meth:`acquire` to :meth:`release`."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)
        self._file: IO[bytes] | None = None

    @property
    def path(self) -> Path:
        return self.directory / LOCK_FILE

    def acquire(self) -> None:
        """Hold the lock. Raises `StateInUseError` when another process does."""
        if self._file is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        # Opened without truncating: the file may be another process's, and
        # its process id is in it until the lock says otherwise.
        file = self.path.open("a+b")
        if not _try_lock(file):
            file.close()
            raise StateInUseError(self.directory, _holder(self.path))
        file.seek(0)
        file.truncate()
        file.write(str(os.getpid()).encode("ascii"))
        file.flush()
        self._file = file

    def release(self) -> None:
        """Let go of the lock. Safe to call when it is not held."""
        file, self._file = self._file, None
        if file is None:
            return
        with contextlib.suppress(OSError):
            _unlock(file)
        file.close()


def in_use(directory: str | os.PathLike[str]) -> bool:
    """Whether a process holds the lock on ``directory``, without taking it."""
    probe = StateLock(directory)
    if not probe.path.exists():
        return False
    try:
        probe.acquire()
    except StateInUseError:
        return True
    probe.release()
    return False
