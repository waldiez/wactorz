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
