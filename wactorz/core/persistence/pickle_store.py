"""Pickle store — agent.state objects only.

Each agent's state is one file, and it is kept in memory once read: a recall is
then a lookup rather than the whole file unpickled again, and a persist changes
the copy in memory and leaves the file to be written a moment later, off the
event loop (see `deferred_write`). The file is what a restart reads; between
restarts the copy in memory is the state.

Inside the file each value is pickled on its own. A value that no longer
unpickles — a model object after a library upgrade, a class that was renamed —
then costs its own key and not the counters and settings stored beside it. Its
bytes are kept and written back unchanged, so the key returns once the code that
reads it does, and is gone only when the agent writes that key again or its
state is replaced or deleted. A file holding one plain pickled dict, as earlier
releases wrote, is still read, and written the new way on its next save. One
consequence of pickling values apart: two keys that held the same object hold
equal copies of it after a restart.

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
from typing import Any, NamedTuple

from ..atomic_io import quarantine_unreadable
from ..deferred_write import LARGE_STATE_BYTES, DeferredWriter
from ..paths import agent_state_dir, resolve_state_dir

logger = logging.getLogger(__name__)

#: Marks a state file written one value at a time, and says which layout it is.
STATE_FORMAT_KEY = "wactorz_state_format"
STATE_FORMAT = 1


class NotAStateFileError(TypeError):
    """A file in an agent's state directory that holds something other than a state."""

    def __init__(self, found: object) -> None:
        super().__init__(f"a state file holds a dict, not {type(found).__name__}")


class DecodedState(NamedTuple):
    """What a state file held, split by whether each value could be read."""

    #: The values that unpickled.
    values: dict[str, Any]
    #: The pickled bytes of each value that did not, to be written back as they are.
    unreadable: dict[str, bytes]
    #: Why each of those failed, for the log.
    reasons: dict[str, str]


def encode_state(
    state: dict[str, Any], unreadable: dict[str, bytes] | None = None
) -> tuple[bytes, list[str]]:
    """The bytes a state file should hold, and the keys that could not be pickled.

    A key that will not pickle is left out rather than stopping the write, so
    one open camera handle does not keep every other key from reaching disk.
    ``unreadable`` is carried through unchanged for each key ``state`` does not
    set.
    """
    values: dict[str, bytes] = {}
    unpicklable: list[str] = []
    for key, value in state.items():
        try:
            values[key] = pickle.dumps(value)
        except Exception:
            unpicklable.append(key)
    for key, raw in (unreadable or {}).items():
        if key not in state:
            values[key] = raw
    return pickle.dumps({STATE_FORMAT_KEY: STATE_FORMAT, "values": values}), unpicklable


def decode_state(data: bytes) -> DecodedState:
    """Read a state file's bytes, one value at a time.

    Raises when the file as a whole cannot be read — truncated, or not a state
    file — since there is then no key to save from it.
    """
    # Our own state file, written by this app under the state dir.
    outer = pickle.loads(data)  # noqa: S301
    if not isinstance(outer, dict):
        raise NotAStateFileError(outer)
    if outer.get(STATE_FORMAT_KEY) != STATE_FORMAT or not isinstance(outer.get("values"), dict):
        # A plain dict, as releases before the per-value layout wrote.
        return DecodedState(dict(outer), {}, {})
    values: dict[str, Any] = {}
    unreadable: dict[str, bytes] = {}
    reasons: dict[str, str] = {}
    for key, raw in outer["values"].items():
        try:
            values[key] = pickle.loads(raw)  # noqa: S301
        except Exception as exc:
            unreadable[key] = raw
            reasons[key] = f"{type(exc).__name__}: {exc}"
    return DecodedState(values, unreadable, reasons)


def read_state_file(path: Path) -> DecodedState:
    """Read the state file at ``path``. Raises as `decode_state` does, or on I/O."""
    return decode_state(path.read_bytes())


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
        #: agent name -> key -> the pickled bytes of a value that would not
        #: unpickle, written back as they are until the key is set again.
        self.unreadable: dict[str, dict[str, bytes]] = {}
        #: agent name -> the keys last left out of its file for not pickling,
        #: so the warning is given when that changes rather than on every write.
        self.unpicklable: dict[str, tuple[str, ...]] = {}
        #: The agents already told their state has grown large, told once each.
        self.warned_large: set[str] = set()
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
        # Made when a state is first written, not here: a store can be built,
        # and read from, without creating anything.
        self._base = Path(base_dir or resolve_state_dir()).resolve()
        held = _held_for(self._base)
        #: Kept, so the shared states live for as long as this store does.
        self._held = held
        self._lock = held.lock
        self._states = held.states
        self._unreadable = held.unreadable
        self._unpicklable = held.unpicklable
        self._warned_large = held.warned_large
        self._writer = held.writer

    def _path(self, agent_name: str) -> Path:
        """The agent's state file, guaranteed to be inside the base directory.

        Replacing separators is not enough on its own: ``..`` contains none, so
        it survives the substitution and walks up a level. The containment check
        is what actually holds — it does not depend on having predicted every
        way a name can climb out, which matters because these files are
        unpickled, and unpickling a file an attacker placed is code execution.
        """
        return agent_state_dir(self._base, agent_name) / "state.pkl"

    def save(self, agent_name: str, state: dict[str, Any]) -> bool:
        """Make ``state`` the agent's state, replacing any previous one.

        It is the state from this call on, and reaches the file shortly after.
        Values that could not be read when the file was loaded go with the rest
        of the old state; `merge` keeps them. True always: whether the write
        lands is not known yet, and one that does not is reported at warning
        when it fails, with the file's path.
        """
        with self._lock:
            self._states[agent_name] = state
            self._unreadable.pop(agent_name, None)
        self._schedule(agent_name)
        return True

    def merge(self, agent_name: str, values: dict[str, Any]) -> None:
        """Set each key in ``values``, leaving the agent's other keys as they are."""
        with self._lock:
            self.load(agent_name).update(values)
            kept = self._unreadable.get(agent_name, {})
            for key in values:
                kept.pop(key, None)
        self._schedule(agent_name)

    def update(self, agent_name: str, key: str, value: Any) -> None:
        """Set one key of the agent's state."""
        self.merge(agent_name, {key: value})

    def remove(self, agent_name: str, key: str) -> None:
        """Drop one key of the agent's state, if it is there."""
        with self._lock:
            self.load(agent_name).pop(key, None)
            self._unreadable.get(agent_name, {}).pop(key, None)
        self._schedule(agent_name)

    def flush(self) -> None:
        """Write every state that is waiting to be written, before returning."""
        self._writer.flush()

    def _schedule(self, agent_name: str) -> None:
        self._writer.submit(self._path(agent_name), lambda: self._encoded(agent_name))

    def _encoded(self, agent_name: str) -> bytes:
        """The agent's state as the bytes its file should hold.

        Under the lock, so a persist from a thread cannot change the state half
        way through. A value that will not pickle is left out of the file and
        kept in memory, and said once each time the set of such keys changes.
        """
        with self._lock:
            data, unpicklable = encode_state(
                self._states[agent_name], self._unreadable.get(agent_name)
            )
            left_out = tuple(unpicklable)
            if left_out and left_out != self._unpicklable.get(agent_name):
                logger.warning(
                    "[Persistence] Not writing %s for '%s': it cannot be pickled, so it "
                    "lasts only until the process stops. The rest of the state was written.",
                    ", ".join(left_out),
                    agent_name,
                )
            self._unpicklable[agent_name] = left_out
            self._note_if_large(agent_name, len(data))
            return data

    def _note_if_large(self, agent_name: str, size: int) -> None:
        """Say so once when an agent's state has grown expensive to write.

        Every persist rewrites the agent's whole state, so what a save costs
        follows the size of everything it remembers, not of what changed, and
        the pickling happens on the event loop that every other agent shares. A
        warning rather than a limit: what an agent keeps is its author's call.
        """
        if size < LARGE_STATE_BYTES or agent_name in self._warned_large:
            return
        self._warned_large.add(agent_name)
        logger.warning(
            "[Persistence] '%s' persists %.1fMB, and every persist rewrites all of it on "
            "the event loop every agent shares. Keep what it remembers bounded -- a recent "
            "slice rather than the whole history.",
            agent_name,
            size / 1_048_576,
        )

    def load(self, agent_name: str) -> dict[str, Any]:
        """An agent's state, or an empty dict if there is none to read.

        The state itself rather than a copy: what a caller changes in it is
        changed for every later reader, and reaches the file only through
        `save`, `update` or `remove`.

        A value that cannot be unpickled is left out, and the rest are read. A
        file that cannot be read at all is treated as absent, so a corrupt state
        file degrades to a fresh start rather than a crash loop — but it is
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
        if not path.exists():
            return {}
        try:
            decoded = read_state_file(path)
        except Exception as e:
            kept = quarantine_unreadable(path)
            logger.warning(
                "[Persistence] Pickle load failed for %s: %s — %s",
                agent_name,
                e,
                f"kept at {kept}" if kept else "the file could not be preserved",
            )
            return {}
        if decoded.unreadable:
            self._unreadable[agent_name] = dict(decoded.unreadable)
            logger.warning(
                "[Persistence] '%s' starts without %s: %s. Kept in %s as they were, "
                "and read again on the next start; persisting one of them replaces it.",
                agent_name,
                ", ".join(sorted(decoded.unreadable)),
                "; ".join(f"{k}: {r}" for k, r in sorted(decoded.reasons.items())),
                path,
            )
        return decoded.values

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
            self._unreadable.pop(agent_name, None)
            self._unpicklable.pop(agent_name, None)
            self._warned_large.discard(agent_name)
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
