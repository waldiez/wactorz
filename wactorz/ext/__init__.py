"""Extension discovery.

An extension is a module or package in wactorz/ext/, or one any installed
package registers under the ``wactorz.extensions`` entry-point group,
exposing setup(app: web.Application) -> None, called once at app construction.
Extensions add routes via app.add_routes(...) and startup work via
app.on_startup.append(...), and read their OWN env vars (no CONFIG
entanglement).

From another package
--------------------
A package declares one in its ``pyproject.toml``::

    [project.entry-points."wactorz.extensions"]
    mlops = "wactorz_mlops.ext"

The value names a module, or ``module:object``, with the same hooks as a
module under ``wactorz/ext/``. The entry point's name is the extension's short
name: what namespaces its ``public_config`` on ``/api/config`` and what another
extension's ``__deps__`` refers to. A name a built-in extension already uses is
refused, with a log line, so a package cannot stand in for one.

An extension MAY also expose an optional
``public_config(app) -> dict[str, Any]`` returning **non-secret** values for the
browser (never tokens/passwords/keys). Core's /api/config merges these,
namespaced by extension, so the frontend can seed feature defaults (e.g. the
Graph tab's Fuseki dataset) without core knowing extension internals.

Cross-extension wiring (advanced)
----------------------------------
setup() runs first for every extension — self-contained init only (routes,
own env, own startup hooks). Extensions that need to read another extension's
app-state can declare an optional ``async def on_ready(app)`` hook. It is
registered as an on_startup handler in a second phase after every setup() has
completed — so it runs after all of setup()'s own on_startup hooks.

If on_ready's order matters, declare ``__deps__ = ["other_ext", ...]`` (list
of extension short names). setup_all() topo-sorts the on_ready phase by these
declared dependencies.  Extensions without __deps__ or with unmet deps are
skipped in the sort and run in discovery order at the end.

Teardown is each extension's own responsibility: register app.on_shutdown /
app.on_cleanup handlers inside setup (aiohttp awaits them at server stop —
on_shutdown fires while connections are still open, on_cleanup after; both
take async callables).

Failures are logged, never fatal — a broken extension must not take the
server down.

FUTURE: if an extension might need a teardown ordering or prerequisite like
``on_ready``, we can add a similar ``on_stop`` phase with LIFO ordering

"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from .. import installed

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

#: The entry-point group an installed package registers an extension under.
ENTRY_POINT_GROUP = "wactorz.extensions"

#: The short name of each extension loaded from an entry point, by the name of
#: the module or object loaded: an entry point names the extension, where a
#: module under ``wactorz/ext/`` is named by its file.
_entry_point_names: dict[str, str] = {}

#: What the entry points gave, once read; None until then.
_external: list[Extension] | None = None


class Extension(Protocol):  # pylint: disable=too-few-public-methods
    """Extension typing-only contract.

    Plain modules satisfy it structurally.
    Runtime discovery still duck-types with hasattr; never isinstance.
    """

    __name__: str

    def setup(self, app: Application) -> None:
        """Set up the extension against the aiohttp app.

        Called once at app construction. Register routes, on_startup hooks,
        and any on_shutdown / on_cleanup teardown here.

        Parameters:
            app: Application. The aiohttp web app.
        """

    # Optional, checked with hasattr:
    #   def public_config(self, app: Application) -> dict[str, Any]: ...
    #     Non-secret config for the browser; merged into /api/config.
    #
    #   __deps__: list[str] = ["swid"]
    #     Other extension short names that must finish setup() before on_ready().
    #
    #   async def on_ready(self, app: Application) -> None:
    #     Cross-extension wiring — all other setup() calls have run.


def discover(*, refresh: bool = False) -> list[Extension]:
    """Every available extension: the modules under this package, then the entry points.

    The entry points are read once and kept, since this runs more than once
    while the server starts; ``refresh`` reads them again.
    """
    mods: list[Extension] = []
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] %s failed to import: %s", m.name, exc)
            continue
        if hasattr(mod, "setup"):
            mods.append(mod)  # pyright: ignore[reportArgumentType]
    mods.extend(_from_entry_points({_short_name(m) for m in mods}, refresh=refresh))
    return mods


def _from_entry_points(taken: set[str], *, refresh: bool = False) -> list[Extension]:
    """The extensions installed packages register, less any whose name is ``taken``.

    A broken entry point is logged and left out, as a broken module under
    ``wactorz/ext/`` is: one bad package must not keep the server from
    starting. One whose name a built-in extension already uses is refused,
    so a package cannot quietly replace the built-in.
    """
    global _external
    if _external is not None and not refresh:
        return list(_external)
    found: list[Extension] = []
    names: dict[str, str] = {}
    for entry_point in _entry_points():
        name = str(entry_point.name)
        if name in taken or name in names.values():
            log.warning(
                "[ext] Skipping entry point %s = %s: an extension called %r is already loaded",
                name,
                entry_point.value,
                name,
            )
            continue
        try:
            loaded = entry_point.load()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] Skipping entry point %s = %s: %s", name, entry_point.value, exc)
            continue
        if not callable(getattr(loaded, "setup", None)):
            log.warning(
                "[ext] Skipping entry point %s = %s: it has no setup(app)", name, entry_point.value
            )
            continue
        names[_module_name(loaded)] = name
        found.append(loaded)
        log.info("[ext] %s found in entry point %s", name, entry_point.value)
    _entry_point_names.clear()
    _entry_point_names.update(names)
    _external = found
    return list(found)


def _entry_points() -> list[Any]:
    try:
        return installed.entry_points(ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - a broken installation
        log.warning("[ext] Could not read %s entry points: %s", ENTRY_POINT_GROUP, exc)
        return []


def _module_name(loaded: Any) -> str:
    """What identifies ``loaded`` for naming: its module name, or its own."""
    return str(
        getattr(loaded, "__name__", "") or f"{type(loaded).__module__}.{type(loaded).__name__}"
    )


def setup_all(app: Application) -> None:
    """Setup all discovered extensions (two-phase).

    Phase 1 — setup(): self-contained init, no cross-extension reads.
    Phase 2 — on_ready(): registered as on_startup hooks, topo-sorted by
    __deps__ so they run after every setup()'s own on_startup hooks.
    """
    mods = discover()

    # Phase 1
    for mod in mods:
        try:
            mod.setup(app)
            log.info("[ext] %s loaded", getattr(mod, "__name__", mod))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] %s setup failed: %s", getattr(mod, "__name__", mod), exc)

    # Phase 2  — register, don't call; aiohttp runs them at server start
    for mod in _topo_sort_on_ready(mods):
        name = _short_name(mod)
        if hasattr(mod, "on_ready"):
            app.on_startup.append(mod.on_ready)  # pyright: ignore[reportAttributeAccessIssue]
            log.info("[ext] %s on_ready registered", name)


def collect_public_config(app: Application) -> dict[str, Any]:
    """Merge each extension's non-secret browser config, namespaced by extension.

    Extensions expose an optional ``public_config(app) -> dict``; the result is
    placed under the extension's short name (e.g. ``{"fuseki": {...}}``) and
    served by core's /api/config. Extensions are responsible for returning ONLY
    values safe for the browser — never tokens, passwords, or keystore data.
    Best-effort: a failing or absent hook is skipped, never fatal.
    """
    out: dict[str, Any] = {}
    for mod in discover():
        provider = getattr(mod, "public_config", None)
        if provider is None:
            continue
        name = _short_name(mod)
        try:
            out[name] = provider(app)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] %s public_config() failed: %s", name, exc)
    return out


def collect_actor_decorators(app: Application) -> list[Callable[[Any, dict], None]]:
    """Gather each extension's per-actor payload decorator.

    An extension may expose ``actor_decorator(app) -> Callable[[actor, payload], None]``
    — typically a closure that fetches its index once, then enriches each payload
    in place. The web layer's actors_handler calls every returned decorator on each
    actor payload it builds, so extensions can add fields (e.g. identity did/handle)
    without the monitor knowing about them. Best-effort: a failing or absent hook,
    or a factory that returns None, is skipped.
    """
    decorators: list[Callable[[Any, dict], None]] = []
    for mod in discover():
        factory = getattr(mod, "actor_decorator", None)
        if factory is None:
            continue
        name = _short_name(mod)
        try:
            dec = factory(app)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] %s actor_decorator() failed: %s", name, exc)
            continue
        if dec is not None:
            decorators.append(dec)
    return decorators


def _short_name(mod: Extension) -> str:
    """What an extension is called: its entry point's name, or its module's last part."""
    module_name = _module_name(mod)
    return _entry_point_names.get(module_name) or module_name.rsplit(".", 1)[-1]


def _topo_sort_on_ready(mods: list[Extension]) -> list[Extension]:
    """Topo-sort extensions with on_ready by their __deps__ declarations.

    Extensions without __deps__ or with an empty __deps__ list are appended
    at the end in discovery order.  Unmet deps are silently ignored (extension
    missing / no on_ready) so a stale dep doesn't block startup.
    """
    with_on_ready = [m for m in mods if hasattr(m, "on_ready")]
    if not with_on_ready:
        return []

    name_to_mod: dict[str, Extension] = {_short_name(m): m for m in with_on_ready}
    deps_graph: dict[str, set[str]] = {}
    for name, mod in name_to_mod.items():
        declared = set(getattr(mod, "__deps__", [])) & name_to_mod.keys()
        deps_graph[name] = declared

    in_degree: dict[str, int] = dict.fromkeys(name_to_mod, 0)
    for deps in deps_graph.values():
        for dep in deps:
            in_degree[dep] = in_degree.get(dep, 0) + 1

    queue: deque[str] = deque(n for n, d in in_degree.items() if d == 0)
    sorted_names: list[str] = []
    while queue:
        name = queue.popleft()
        sorted_names.append(name)
        for dep in deps_graph.get(name, set()):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Anything not sorted → cycle.  Log and skip cyclic extensions.
    if len(sorted_names) != len(name_to_mod):
        missing = set(name_to_mod) - set(sorted_names)
        log.warning(
            "[ext] on_ready cycle detected among %s — skipping their on_ready",
            sorted(missing),
        )

    # Build result: sorted first, then any remaining (no deps / unmet deps /
    # cycle survivors) in discovery order.
    sorted_set = set(sorted_names)
    result = [name_to_mod[n] for n in sorted_names if n in name_to_mod]
    for mod in with_on_ready:
        if _short_name(mod) not in sorted_set:
            result.append(mod)
    return result
