"""Catalogue programs compiled the way a DynamicAgent compiles them.

A catalogue program runs as source exec'd into a namespace of its own rather
than as an imported module, so each test file gets a namespace of its own too
and can patch it without reaching another file's. Compiling under the file's
real path keeps tracebacks and coverage on the program's own lines.
"""

from pathlib import Path
from typing import Any

import wactorz.catalogue_agents

PROGRAMS = Path(wactorz.catalogue_agents.__file__).parent


def program_namespace(relative: str) -> dict[str, Any]:
    """Exec the program at `relative` under the catalogue package; return its globals."""
    path = PROGRAMS / relative
    namespace: dict[str, Any] = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace
