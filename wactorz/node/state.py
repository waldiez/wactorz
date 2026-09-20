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
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..core.atomic_io import write_text

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


class JsonState:
    """One agent's state file: read it, write it, and remove it for good."""

    def __init__(self, path: Path, agent_name: str) -> None:
        self.path = path
        self._name = agent_name

    def save(self, values: dict[str, Any]) -> None:
        """Write what the agent remembers, keeping what can be kept.

        Encoded in full before anything is written, and written through
        :func:`write_text`, for two failures that both ended with the agent's
        memory gone rather than stale. Streaming into an opened file truncated
        it first and then stopped at the first value that would not serialise,
        leaving invalid JSON where a good file had been — so one
        ``agent.persist('when', datetime.now())`` cost the agent every other key
        it held. And an interrupted write did the same on a board that lost
        power mid-save.

        A value that cannot travel is dropped and named, rather than taking the
        rest with it: the same call :meth:`json_safe` makes for a migration, and
        for the same reason — a counter or a calibration is worth keeping, and a
        capture object would not survive a restart either way.
        """
        keepable, dropped = json_safe(values)
        if dropped:
            logger.warning(
                "[%s] Not persisting %s: nothing there can be written as JSON, which is "
                "what a node keeps. The rest of this agent's state was saved.",
                self._name,
                ", ".join(dropped),
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_text(self.path, json.dumps(keepable))
        except Exception as e:
            logger.warning("[%s] State save failed: %s", self._name, e)

    def load(self) -> dict[str, Any]:
        """Read the agent's state, or return empty if it cannot be read.

        A file that will not parse is moved aside rather than left in place: the
        next save would write straight over it, so the only copy of whatever the
        agent remembered would be gone.
        """
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
        return loaded if isinstance(loaded, dict) else {}

    def delete(self) -> bool:
        """Remove the file for good. True when there was one to remove.

        This is what makes a delete (rather than a stop) irreversible: without
        it the next runner start would load the file back and the agent's whole
        memory would return after its registry entry was cleared.
        """
        try:
            if self.path.exists():
                self.path.unlink()
                logger.info("[%s] Deleted persistent state file: %s", self._name, self.path)
                return True
        except Exception as e:
            logger.warning("[%s] Failed to delete state file %s: %s", self._name, self.path, e)
        return False


def json_safe(values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The part of ``values`` that can travel over MQTT, and the keys that cannot."""
    safe: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in values.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            dropped.append(key)
        else:
            safe[key] = value
    return safe, dropped
