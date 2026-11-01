"""Which installed packages may add to the system.

A package installed beside Wactorz can register extensions, agents and pipelines
as entry points, and loading one runs its code in the server, outside every check
a spawned agent passes. Being installed is not enough to be trusted with that: an
agent's own requirements install packages too. So a package is loaded only when
its distribution is named in ``WACTORZ_EXTENSIONS``, and ``*`` there loads them
all. One setting covers the three groups, because what is trusted is the package.

The check reads metadata and comes before ``load()``, so an unlisted package's
code is never imported. What Wactorz ships itself, and targets a deployment names
in ``WACTORZ_AGENTS`` or ``WACTORZ_PIPELINES``, are not entry points and do not
pass through here.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from typing import Any

from .config import CONFIG

logger = logging.getLogger(__name__)

#: The environment variable naming the distributions to load.
ENV_VAR = "WACTORZ_EXTENSIONS"
#: In that variable: every installed package, as an explicit choice.
EVERY_PACKAGE = "*"

#: The packages left out so far, each warned about once.
_blocked: set[str] = set()


def _normalised(name: str) -> str:
    """``name`` as packaging compares it: ``Wactorz_MLOps`` is ``wactorz-mlops``."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def listed(value: str) -> set[str]:
    """The distributions named in a ``WACTORZ_EXTENSIONS`` value, comma or space separated."""
    return {_normalised(raw) for raw in value.replace(",", " ").split()}


def _distribution_of(entry_point: Any) -> str:
    """The name of the distribution that registered ``entry_point``, or ``""`` if unknown."""
    return str(getattr(getattr(entry_point, "dist", None), "name", "") or "")


def _leave_out(package: str, value: str) -> None:
    """Remember ``package`` as left out, and say how to load it the first time."""
    if package in _blocked:
        return
    _blocked.add(package)
    named = ",".join([*value.replace(",", " ").split(), package])
    logger.warning(
        "[installed] %s is installed and registers agents, extensions or pipelines, "
        "but is not loaded. To load it, set %s=%s",
        package,
        ENV_VAR,
        named,
    )


def entry_points(group: str, *, value: str | None = None) -> list[Any]:
    """The entry points of ``group`` from the packages this deployment allows.

    A package that does not say which distribution it is cannot be matched
    against the list, so it is left out and named by the entry point's value.
    """
    if value is None:
        value = CONFIG.extensions_env
    allowed = listed(value)
    found = list(importlib.metadata.entry_points(group=group))
    if EVERY_PACKAGE in allowed:
        return found
    kept: list[Any] = []
    for entry_point in found:
        package = _distribution_of(entry_point)
        if package and _normalised(package) in allowed:
            kept.append(entry_point)
        else:
            _leave_out(package or str(entry_point.value), value)
    return kept


def blocked() -> list[str]:
    """The packages left out so far, for the dashboard to name."""
    return sorted(_blocked)


def forget() -> None:
    """Forget what was left out. For tests, and for a fresh run."""
    _blocked.clear()
