"""Extension discovery.

An extension is a module or package in wactorz/ext/
exposing setup(app: web.Application) -> None, called once at app construction.
Extensions add routes via app.add_routes(...) and startup work via
app.on_startup.append(...), and read their OWN env vars (no CONFIG
entanglement).

An extension MAY also expose an optional
``public_config(app) -> dict[str, Any]`` returning **non-secret** values for the
browser (never tokens/passwords/keys). Core's /api/config merges these,
namespaced by extension, so the frontend can seed feature defaults (e.g. the
Graph tab's Fuseki dataset) without core knowing extension internals.

Teardown is each extension's own responsibility: register app.on_shutdown /
app.on_cleanup handlers inside setup (aiohttp awaits them at server stop —
on_shutdown fires while connections are still open, on_cleanup after; both
take async callables).

Failures are logged, never fatal — a broken extension must not take the
server down.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)


class Extension(Protocol):  # pylint: disable=too-few-public-methods
    """Extension typing-only contract.

    Plain modules satisfy it structurally.
    Runtime discovery still duck-types with hasattr; never isinstance.
    See below for usage with aiohttp.web.Application.
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


def discover() -> list[Extension]:
    """Discover available extensions."""
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
    return mods


def setup_all(app: Application) -> None:
    """Setup all discovered extensions."""
    for mod in discover():
        try:
            mod.setup(app)
            log.info("[ext] %s loaded", getattr(mod, "__name__", mod))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] %s setup failed: %s", getattr(mod, "__name__", mod), exc)


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
        name = mod.__name__.rsplit(".", 1)[-1]
        try:
            out[name] = provider(app)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("[ext] %s public_config() failed: %s", name, exc)
    return out
