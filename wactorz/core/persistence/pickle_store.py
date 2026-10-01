"""Pickle store — agent.state objects only.

Each agent's state is one file, and it is kept in memory once read: a recall is
then a lookup rather than the whole file unpickled again, and a persist changes
the copy in memory and leaves the file to be written a moment later, off the
event loop (see `deferred_write`). The file is what a restart reads; between
restarts the copy in memory is the state.

That makes the copy in memory the one thing that must not be bypassed. Two
stores over one directory therefore share it, and whatever changes or removes a
state file in a running process does so through a store, or the next write
would put back what it removed.
"""

import logging
import pickle
import threading
import weakref
from pathlib import Path
from typing import Any

from ..atomic_io import quarantine_unreadable
from ..deferred_write import DeferredWriter
from ..paths import agent_state_dir, resolve_state_dir

logger = logging.getLogger(__name__)

# ── Pickle Store (for agent.state only) ───────────────────────────────────


class _Held:
    """The states under one directory, as every store over that directory sees them."""

    def __init__(self) -> None:
        #: Guards `states`. Agent code runs blocking work on threads, and may
        #: persist from one while the event loop is encoding a state to write
        #: it. Reentrant, since reading a state for the first time happens
        #: inside an update that already holds it.
        self.lock = threading.RLock()
        #: agent name -> its state, as last read from disk or set through a store.
        self.states: dict[str, dict[str, Any]] = {}
        self.writer = DeferredWriter()


#: One per directory in use, shared by the stores over it and gone with the last.
_HELD: "weakref.WeakValueDictionary[Path, _Held]" = weakref.WeakValueDictionary()
_HELD_LOCK = threading.Lock()


def _held_for(base: Path) -> _Held:
    with _HELD_LOCK:
        held = _HELD.get(base)
        if held is None:
            held = _HELD[base] = _Held()
        return held


class PickleStore:
    """Pickle-based persistence for arbitrary Python objects.
    Used ONLY for agent.state dicts (ML models, numpy arrays, cv2 captures).
    """

    def __init__(self, base_dir: str | None = None) -> None:
        self._base = Path(base_dir or resolve_state_dir()).resolve()
        self._base.mkdir(parents=True, exist_ok=True)
        held = _held_for(self._base)
        #: Kept, so the shared states live for as long as this store does.
        self._held = held
        self._lock = held.lock
        self._states = held.states
        self._writer = held.writer

    def _path(self, agent_name: str) -> Path:
        """The agent's state file, guaranteed to be inside the base directory.

        Replacing separators is not enough on its own: ``..`` contains none, so
        it survives the substitution and walks up a level. The containment check
        is what actually holds — it does not depend on having predicted every
        way a name can climb out, which matters because these files are
        unpickled, and unpickling a file an attacker placed is code execution.
        """
        p = agent_state_dir(self._base, agent_name)
        p.mkdir(parents=True, exist_ok=True)
        return p / "state.pkl"

    def save(self, agent_name: str, state: dict[str, Any]) -> bool:
        """Make ``state`` the agent's state, replacing any previous one.

        It is the state from this call on, and reaches the file shortly after.
        True always: whether the write lands is not known yet, and one that
        does not is reported at warning when it fails, with the file's path.
        """
        with self._lock:
            self._states[agent_name] = state
        self._schedule(agent_name)
        return True

    def update(self, agent_name: str, key: str, value: Any) -> None:
        """Set one key of the agent's state."""
        with self._lock:
            self.load(agent_name)[key] = value
        self._schedule(agent_name)

    def remove(self, agent_name: str, key: str) -> None:
        """Drop one key of the agent's state, if it is there."""
        with self._lock:
            self.load(agent_name).pop(key, None)
        self._schedule(agent_name)

    def flush(self) -> None:
        """Write every state that is waiting to be written, before returning."""
        self._writer.flush()

    def _schedule(self, agent_name: str) -> None:
        self._writer.submit(self._path(agent_name), lambda: self._encoded(agent_name))

    def _encoded(self, agent_name: str) -> bytes:
        """The agent's state as the bytes its file should hold.

        Under the lock, so a persist from a thread cannot change the state half
        way through. An object that will not pickle fails here, and the agent
        keeps running with its state in memory.
        """
        with self._lock:
            return pickle.dumps(self._states[agent_name])

    def load(self, agent_name: str) -> dict[str, Any]:
        """An agent's state, or an empty dict if there is none to read.

        The state itself rather than a copy: what a caller changes in it is
        changed for every later reader, and reaches the file only through
        `save`, `update` or `remove`.

        A file that exists but cannot be read is treated as absent, so a corrupt
        state file degrades to a fresh start rather than a crash loop — but it is
        moved aside first. Left in place it would be overwritten by this agent's
        next save, destroying the only copy of whatever it held.
        """
        with self._lock:
            state = self._states.get(agent_name)
            if state is None:
                state = self._states[agent_name] = self._read(agent_name)
            return state

    def _read(self, agent_name: str) -> dict[str, Any]:
        path = self._path(agent_name)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    # Our own state file, written by this app under the state dir.
                    return pickle.load(f)  # noqa: S301
            except Exception as e:
                kept = quarantine_unreadable(path)
                logger.warning(
                    "[Persistence] Pickle load failed for %s: %s — %s",
                    agent_name,
                    e,
                    f"kept at {kept}" if kept else "the file could not be preserved",
                )
        return {}

    def delete(self, agent_name: str) -> None:
        """Remove the agent's state.pkl AND its containing directory.

        Without removing the directory, a subsequent re-spawn of an agent
        with the same name would find an empty folder rather than a truly
        clean slate — harmless but easy to misread when debugging.
        """
        path = self._path(agent_name)
        # Before the file goes: a write of this state still on its way out
        # would otherwise put the file back.
        self._writer.discard(path)
        with self._lock:
            self._states.pop(agent_name, None)
        if path.exists():
            try:
                path.unlink()
            except Exception as e:
                logger.warning("[Persistence] Pickle unlink failed for %s: %s", agent_name, e)
                return
        # Try to drop the parent directory too. rmdir only succeeds if empty,
        # which is what we want — never remove a folder a user populated.
        parent = path.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as e:
            logger.debug("[Persistence] Pickle rmdir skipped for %s: %s", parent, e)
