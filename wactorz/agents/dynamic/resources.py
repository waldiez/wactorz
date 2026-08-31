"""Releasing what generated code left open when the agent stops.

`cleanup()` is optional and often missing or incomplete, so anything holding a
camera or a file is closed here as well. Both places generated code keeps such
things are scanned: `agent.state`, which is the documented pattern, and the
module globals of the compiled namespace, which is what models actually write
(`_cap = None` at module level).
"""

import logging
import types
from typing import Any

logger = logging.getLogger(__name__)

#: Values that cannot hold an OS resource, so they are never probed.
SKIP_TYPES = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    type,
    types.ModuleType,
    types.FunctionType,
    types.CoroutineType,
)

#: Namespace entries that are the agent's own entry points, not its resources.
ENTRY_POINTS = ("setup", "process", "cleanup", "handle_task")


def release_one(key: str, obj: Any, log_name: str) -> None:
    """Close a single object if it looks like a camera or a file."""
    if obj is None or isinstance(obj, SKIP_TYPES):
        return
    if hasattr(obj, "release") and hasattr(obj, "isOpened"):
        try:
            if obj.isOpened():
                obj.release()
                logger.info("[%s] Released camera handle '%s'", log_name, key)
        except Exception as exc:
            logger.debug("[%s] Camera '%s' would not release: %s", log_name, key, exc)
    elif hasattr(obj, "close") and hasattr(obj, "closed"):
        try:
            if not obj.closed:
                obj.close()
                logger.debug("[%s] Closed file handle '%s'", log_name, key)
        except Exception as exc:
            logger.debug("[%s] File '%s' would not close: %s", log_name, key, exc)


def release_open_resources(api: Any, namespace: dict[str, Any], log_name: str) -> None:
    """Release everything the agent left open, in state and in module globals."""
    state = getattr(api, "state", {}) if api else {}
    for key in list(state.keys()):
        release_one(key, state.get(key), log_name)

    for key, obj in list(namespace.items()):
        if key.startswith("__") or key in ENTRY_POINTS:
            continue
        release_one(key, obj, log_name)
