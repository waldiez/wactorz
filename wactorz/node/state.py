"""An agent's persistent state on a node, kept as JSON.

Main keeps an agent's state as a pickle, which is the right choice there: it is
written and read by one process, and it carries whatever the agent put in it.
A node's state file has a second reader — a migration ships it over MQTT to
whichever machine the agent moves to — and the two machines need not be running
the same Python. JSON is what survives that trip, and what can be read back by a
node that was redeployed from a newer release.

The same trade-off is why a migration drops what it cannot serialise rather than
failing: a counter, a calibration value or a threshold travels, and a cv2 capture
does not survive a process restart either way.

A model, an array or raw bytes is the exception: it is kept as a blob of its own
beside the file (see `wactorz.core.blobs`), and the file holds a marker in its
place, which is JSON like everything else in it.
"""

import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.blobs import Blobs, is_marker, marker_for
from ..core.deferred_write import LARGE_STATE_BYTES, DeferredWriter
from ..core.paths import agent_state_dir
from ..core.state_snapshot import json_safe

logger = logging.getLogger(__name__)


def state_path(state_dir: Path | str, agent_name: str) -> Path:
    """Where this agent's state file lives.

    The name is flattened rather than nested, because it is one file per agent
    in one directory, and a name containing a separator would otherwise put it
    in a directory of its own -- or, with enough of them, outside the state
    directory altogether.
    """
    safe = agent_name.replace("/", "_").replace("\\", "_")
    return Path(state_dir) / f"{safe}_state.json"


def agent_state(state_dir: Path | str, agent_name: str) -> "JsonState":
    """This agent's state file, with its blobs in the agent's own directory."""
    blobs = agent_state_dir(state_dir, agent_name) / "blobs"
    return JsonState(state_path(state_dir, agent_name), agent_name, blobs)


#: Writes every agent's state file on this node, a moment after it changes and
#: off the event loop. One for the process, so one call at shutdown covers all.
_WRITER = DeferredWriter()


def flush_states() -> None:
    """Write every state file on this node that is waiting to be written."""
    _WRITER.flush()


class JsonState:
    """One agent's state file: read it, write it, and remove it for good."""

    def __init__(self, path: Path, agent_name: str, blob_dir: Path | None = None) -> None:
        self.path = path
        self._name = agent_name
        #: Where values kept as blobs go; without one, they are dropped like
        #: anything else that is not JSON.
        self._blobs = Blobs(blob_dir, _WRITER) if blob_dir is not None else None
        #: The keys whose value is kept as a blob, so a key that stops being
        #: one has its file removed.
        self._blob_keys: set[str] = set()
        #: The marker of each blob that could not be read back, written back
        #: until its key is set again so the blob is not lost.
        self._unreadable: dict[str, Any] = {}
        #: What the agent remembers, as last handed to :meth:`save`. The dict
        #: itself, so the file is written from what it holds when it is written.
        self._values: dict[str, Any] = {}
        #: What the file was last written with, so a save that would rewrite
        #: the same bytes can be skipped. Agents persist a value every tick
        #: that changes far less often --
        #: `agent.persist("plugs", agent.state["plugs"])` -- and the whole file
        #: is rewritten for any one key. Set once the write has landed, not
        #: when it is asked for: a write that failed must be tried again by the
        #: next save, changed or not.
        self._written: bytes | None = None
        #: Whether this agent has been told its state is big enough to hurt.
        self._warned_large = False
        #: The values last reported as impossible to write, so each is named
        #: when it appears rather than at every write.
        self._reported_dropped: tuple[str, ...] = ()

    def save(self, values: dict[str, Any], changed: Iterable[str] | None = None) -> None:
        """Have the file hold what the agent remembers, shortly.

        The agent's memory is ``values`` itself, in the process; the file is
        what a restart reads, and it is written a moment later, off the event
        loop. Saving on every tick therefore costs the loop nothing but this
        call, and the ticks inside that moment become one write.

        ``changed`` names the keys set since the last save, whose blobs are
        written again; None means any may have been, as when the whole state
        is replaced.
        """
        self._values = values
        if self._blobs is not None:
            self._stow(self._blobs, values, changed)
        _WRITER.submit(self.path, self._content, self._landed)

    def _stow(self, blobs: Blobs, values: dict[str, Any], changed: Iterable[str] | None) -> None:
        if changed is None:
            blobs.stow_all(values)
            self._blob_keys = {k for k, v in values.items() if marker_for(v) is not None}
            self._unreadable.clear()
            return
        for key in changed:
            self._unreadable.pop(key, None)
            if key in values and blobs.stow(key, values[key]):
                self._blob_keys.add(key)
            elif key in self._blob_keys:
                self._blob_keys.discard(key)
                blobs.forget(key)

    def _landed(self, data: bytes) -> None:
        """The file now holds ``data``."""
        self._written = data

    def _content(self) -> bytes | None:
        """What the file should hold now, or None when it holds that already.

        Encoded in full before anything is written, and written by replacing
        the file, for two failures that both ended with the agent's memory gone
        rather than stale. Streaming into an opened file truncated it first and
        then stopped at the first value that would not serialise, leaving
        invalid JSON where a good file had been — so one
        ``agent.persist('when', datetime.now())`` cost the agent every other key
        it held. And an interrupted write did the same on a board that lost
        power mid-save.

        A value that cannot travel is dropped and named, rather than taking the
        rest with it: the same call :meth:`json_safe` makes for a migration, and
        for the same reason — a counter or a calibration is worth keeping, and a
        capture object would not survive a restart either way.
        """
        marked = {**self._unreadable, **self._marked(self._values)}
        keepable, dropped = json_safe(marked)
        if dropped and tuple(dropped) != self._reported_dropped:
            logger.warning(
                "[%s] Not persisting %s: nothing there can be written as JSON, which is "
                "what a node keeps. The rest of this agent's state was saved.",
                self._name,
                ", ".join(dropped),
            )
        self._reported_dropped = tuple(dropped)
        encoded = json.dumps(keepable)
        data = encoded.encode("utf-8")
        if data == self._written and self.path.exists():
            # The file already says this. Writing it again costs the same as
            # writing something new -- the whole file goes out and is forced
            # to disk -- for no change at all.
            return None
        self._note_if_large(encoded)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return data

    def _marked(self, values: dict[str, Any]) -> dict[str, Any]:
        """``values`` with each one kept as a blob replaced by its marker."""
        if self._blobs is None:
            return values
        return {key: marker_for(value) or value for key, value in values.items()}

    def _note_if_large(self, encoded: str) -> None:
        """Say so once when this agent's state has grown expensive to write.

        Every key is written by rewriting the whole file, so what a save costs
        follows the size of everything the agent remembers, not the size of what
        changed. The write itself happens off the event loop, but encoding the
        state does not, and takes milliseconds per megabyte on a small board:
        every other agent on the node waits for that, once per write.

        A warning rather than a limit: what an agent should remember is its
        author's business, and an agent that has quietly grown a megabyte of
        history is the one case where nobody has considered the question.
        """
        if self._warned_large or len(encoded) < LARGE_STATE_BYTES:
            return
        self._warned_large = True
        logger.warning(
            "[%s] Persisted state is %.1fMB. Every persist rewrites all of it, which on a "
            "node's storage takes long enough to hold up every other agent there. Keep what "
            "the agent remembers bounded -- a recent slice rather than the whole history.",
            self._name,
            len(encoded) / 1_048_576,
        )

    def load(self) -> dict[str, Any]:
        """Read the agent's state, or return empty if it cannot be read.

        A file that will not parse is moved aside rather than left in place: the
        next save would write straight over it, so the only copy of whatever the
        agent remembered would be gone.
        """
        # An agent started again under this name reads here what the one before
        # it saved, which may still be waiting to be written.
        _WRITER.flush()
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            kept = f"{self.path}.corrupt.{int(time.time())}"
            try:
                self.path.replace(kept)
            except Exception:
                kept = ""
            preserved = f"kept at {kept}" if kept else "the file could not be preserved"
            logger.exception("[%s] State load failed — %s", self._name, preserved)
            return {}
        logger.info("[%s] Loaded persistent state.", self._name)
        if not isinstance(loaded, dict):
            return {}
        if self._blobs is None:
            return loaded
        return self._unpack(self._blobs, loaded)

    def _unpack(self, blobs: Blobs, loaded: dict[str, Any]) -> dict[str, Any]:
        """``loaded`` with each blob's value where its marker was."""
        unpacked = blobs.unpack(loaded)
        self._blob_keys = {key for key, value in loaded.items() if is_marker(value)}
        self._unreadable = dict(unpacked.unreadable)
        if unpacked.unreadable:
            logger.warning(
                "[%s] Starting without %s: %s. Kept in %s as they were, and read again on "
                "the next start; persisting one of them replaces it.",
                self._name,
                ", ".join(sorted(unpacked.unreadable)),
                "; ".join(f"{k}: {r}" for k, r in sorted(unpacked.reasons.items())),
                blobs.directory,
            )
        return unpacked.values

    def delete(self) -> bool:
        """Remove the file for good. True when there was one to remove.

        This is what makes a delete (rather than a stop) irreversible: without
        it the next runner start would load the file back and the agent's whole
        memory would return after its registry entry was cleared.
        """
        # Before the file goes: a save still waiting, or on its way out, would
        # otherwise put it back.
        _WRITER.discard(self.path)
        self._values = {}
        self._written = None
        if self._blobs is not None:
            self._blobs.delete()
            self._blob_keys.clear()
            self._unreadable.clear()
        try:
            if self.path.exists():
                self.path.unlink()
                logger.info("[%s] Deleted persistent state file: %s", self._name, self.path)
                return True
        except Exception as e:
            logger.warning("[%s] Failed to delete state file %s: %s", self._name, self.path, e)
        return False
