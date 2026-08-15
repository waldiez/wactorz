"""State-directory resolution.

Stdlib-only by design — imported at module scope
from core, agents, app and reset, so it must pull in nothing from wactorz.
"""

import os
from pathlib import Path

_DEFAULT = "./state"


def resolve_state_dir(explicit: str | None = None) -> str:
    """Explicit argument, else ``WACTORZ_STATE_DIR``, else ``./state``. No side effects.

    An empty or whitespace-only env var counts as unset, matching how
    ``config.py`` reads its own numeric vars — otherwise a blank
    ``WACTORZ_STATE_DIR=`` in a ``.env`` file resolves to ``""`` and every
    store silently lands in the process's working directory.
    """
    if explicit:
        return explicit
    return os.environ.get("WACTORZ_STATE_DIR", "").strip() or _DEFAULT


def ensure_state_dir(explicit: str | None = None) -> str:
    """:func:`resolve_state_dir`, plus creating the directory.

    Deployments pin an absolute, durable location through
    ``WACTORZ_STATE_DIR`` — the HA add-on sets ``/data/state`` so chat,
    pickle and SQLite state survive add-on updates instead of landing in the
    container's ephemeral layer. Callers that only need the path (module-level
    constants, fallbacks) want :func:`resolve_state_dir` instead: creating a
    directory as an import side effect makes ``./state`` appear from nothing
    more than ``wactorz-reset --help``.
    """
    base = resolve_state_dir(explicit)
    # mkdir on the resolved path (normalises `..`, and creates the target of a
    # dangling symlink instead of failing) — but return `base` unchanged, so both
    # functions in this module always hand back the same form of the same path.
    Path(base).resolve().mkdir(parents=True, exist_ok=True)
    return base


def agent_state_dir(base: str | Path, agent_name: str) -> Path:
    """The directory `agent_name` may keep state in, inside `base`.

    Replacing separators is not enough on its own: `..` contains none, so it
    survives the substitution and walks up a level — an agent called `..` wrote
    its state over the state directory itself, and `POST /api/reset` with that
    name reached the same path. The containment check below is what actually
    holds, because it does not depend on having predicted every way a name can
    climb out. That matters here more than most places: these files are
    unpickled, and unpickling a file someone else placed is code execution.

    Raises rather than sanitising silently. A name that cannot be made into a
    directory is a name the caller should not be storing anything under, and an
    agent quietly writing somewhere other than where it was told is the failure
    this exists to prevent.

    Does not create the directory — callers differ on when that should happen.
    """
    safe = agent_name.replace("/", "_").replace("\\", "_").strip()
    if safe in {"", ".", ".."} or safe.startswith(".."):
        raise ValueError(f"unsafe agent name for a state path: {agent_name!r}")

    root = Path(base).resolve()
    candidate = (root / safe).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"agent name escapes the state directory: {agent_name!r}")
    return candidate
