# SPDX-License-Identifier: Apache-2.0
"""What the tests of the example programs under ``examples/`` share.

Each example is loaded from its folder by path, under a name of its own, since
several call their module ``agent.py``.
"""

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load(example: str, module: str = "agent") -> types.ModuleType:
    """``examples/<example>/<module>.py`` as a module named for both."""
    path = EXAMPLES / example / f"{module}.py"
    name = f"examples_{example}_{module}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def no_wait(real_sleep: Any) -> Any:
    """A sleep that yields once: the examples wait for a system that is stood in for."""

    async def _sleep(_delay: float, *args: Any) -> None:
        await real_sleep(0)

    return _sleep
