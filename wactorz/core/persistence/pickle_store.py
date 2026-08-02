"""Pickle store — agent.state objects only."""

import logging
import pickle
from pathlib import Path
from typing import Any

from ..paths import resolve_state_dir

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
        safe = agent_name.replace("/", "_").replace("\\", "_").strip()
        if safe in {"", ".", ".."} or safe.startswith(".."):
            raise ValueError(f"unsafe agent name for a state path: {agent_name!r}")

        p = (self._base / safe).resolve()
        if p != self._base and self._base not in p.parents:
            raise ValueError(f"agent name escapes the state directory: {agent_name!r}")

        p.mkdir(parents=True, exist_ok=True)
        return p / "state.pkl"

    def save(self, agent_name: str, state: dict[str, Any]) -> None:
        """Write an agent's state, or log and carry on if it cannot be written.

        **A failed save is not reported to the caller** — an unpicklable object
        in the state dict, or an unwritable directory, is logged at debug and
        the agent keeps running with state that will not survive a restart.
        """
        path = self._path(agent_name)
        try:
            with open(path, "wb") as f:
                pickle.dump(state, f)
        except Exception as e:
            logger.debug(f"[Persistence] Pickle save failed for {agent_name}: {e}")

    def load(self, agent_name: str) -> dict[str, Any]:
        """An agent's stored state, or an empty dict if there is none to read.

        A file that exists but cannot be read is logged and treated as absent,
        so a corrupt state file degrades to a fresh start rather than a crash
        loop.
        """
        path = self._path(agent_name)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception as e:
                logger.warning(f"[Persistence] Pickle load failed for {agent_name}: {e}")
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
                logger.warning(f"[Persistence] Pickle unlink failed for {agent_name}: {e}")
                return
        # Try to drop the parent directory too. rmdir only succeeds if empty,
        # which is what we want — never remove a folder a user populated.
        parent = path.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as e:
            logger.debug(f"[Persistence] Pickle rmdir skipped for {parent}: {e}")
