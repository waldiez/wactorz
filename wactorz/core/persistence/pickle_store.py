"""Pickle store — agent.state objects only."""

import logging
import pickle
from pathlib import Path
from typing import Any

from ..atomic_io import quarantine_unreadable, write_pickle
from ..paths import agent_state_dir, resolve_state_dir

logger = logging.getLogger(__name__)

# ── Pickle Store (for agent.state only) ───────────────────────────────────


class PickleStore:
    """Pickle-based persistence for arbitrary Python objects.
    Used ONLY for agent.state dicts (ML models, numpy arrays, cv2 captures).
    """

    def __init__(self, base_dir: str | None = None) -> None:
        self._base = Path(base_dir or resolve_state_dir()).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

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
        """Write an agent's state, replacing any previous one. True if it landed.

        The agent keeps running either way — an unpicklable object in the state
        dict should not take it down — but a caller that cares whether its state
        will survive a restart can now find out, and a lost save is reported at
        warning rather than left for someone reading debug logs.
        """
        path = self._path(agent_name)
        try:
            write_pickle(path, state)
        except Exception as e:
            logger.warning("[Persistence] Pickle save failed for %s: %s", agent_name, e)
            return False
        return True

    def load(self, agent_name: str) -> dict[str, Any]:
        """An agent's stored state, or an empty dict if there is none to read.

        A file that exists but cannot be read is treated as absent, so a corrupt
        state file degrades to a fresh start rather than a crash loop — but it is
        moved aside first. Left in place it would be overwritten by this agent's
        next save, destroying the only copy of whatever it held.
        """
        path = self._path(agent_name)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
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
