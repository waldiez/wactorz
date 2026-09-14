"""What an agent remembers across an in-place code repair.

A repair compiles the model's new code into a fresh namespace, so anything the
old program kept at module level (`count = 0` read through `global count`)
would start over. Plain values are copied across for the names the new
program also defines at module level, so a counter or a conversation history
survives the fix. Anything that is machinery rather than memory is left to the
new program, which builds its own in setup(): functions, classes, modules, and
objects holding a resource such as an open camera.
"""

import logging
import types
from typing import Any

from .resources import ENTRY_POINTS

logger = logging.getLogger(__name__)

#: Values that are memory rather than machinery, so they are copied across.
DATA_TYPES = (bool, int, float, str, bytes, list, dict, set, tuple)

#: What the new program defines under a name that must never be overwritten.
CODE_TYPES = (types.FunctionType, type, types.ModuleType)


def carry_over_globals(old_ns: dict[str, Any], new_ns: dict[str, Any], log_name: str) -> list[str]:
    """Copy the old program's plain module-level values into the new namespace.

    Only names the new code defines itself are touched, so a value the fix
    dropped is not resurrected and nothing the host injected is disturbed.
    Returns the names carried over.
    """
    carried: list[str] = []
    for key, value in old_ns.items():
        if key.startswith("__") or key in ENTRY_POINTS or key not in new_ns:
            continue
        if not isinstance(value, DATA_TYPES) or isinstance(new_ns[key], CODE_TYPES):
            continue
        new_ns[key] = value
        carried.append(key)
    if carried:
        logger.info("[%s] Carried over module state across repair: %s", log_name, carried)
    return carried
