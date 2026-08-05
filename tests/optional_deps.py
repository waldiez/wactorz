"""Stand-ins for optional dependencies, safe to use at module scope.

The suite must run on a machine (or CI job) without the optional extras
installed, so a test that only needs a name to exist may substitute an empty
module for it. The catch is that ``sys.modules`` is process-wide and never
restored, so a careless stub shadows the real dependency for every later test —
which is how `pytest.approx` ended up calling ``isscalar`` on an empty module.

``sys.modules.setdefault(name, ModuleType(name))`` is the wrong tool: it asks
whether the module has been *imported yet*, not whether it *can* be. Import it
for real first, and stub only what is genuinely absent.
"""

from __future__ import annotations

import importlib
import sys
import types


def ensure_importable(*names: str) -> None:
    """Make each name importable, without ever replacing a real module.

    Imports the real dependency when it is installed; inserts an empty
    placeholder only when the import genuinely fails.
    """
    for name in names:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except Exception:  # pylint: disable=broad-exception-caught
            # Not installed (or unimportable here) — a bare module is enough for
            # tests that only need the attribute lookup to resolve.
            sys.modules[name] = types.ModuleType(name)
