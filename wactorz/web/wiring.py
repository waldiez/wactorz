"""``GET /api/wiring``: who publishes what, who reads it, and where the data stops.

The graph is `wactorz.core.wiring`'s, read from main's topic registry at the
time of asking, with where each served model's pointers stand and what each
agent has asked a person. The registry lives in main, so a monitor with no
actor system in its process has nothing to show and says so.
"""

import asyncio
import json
from collections.abc import Iterable
from typing import Any

from aiohttp import web

from ..core import topic_bus
from ..core.artifacts import ArtifactError, ArtifactStore
from ..core.paths import resolve_state_dir
from ..core.wiring import wiring
from . import runtime
from .questions import asked_by

#: Why there is no wiring to show.
NO_REGISTRY = (
    "the wiring is read from main's topic registry, and no actor system runs in this process"
)


def pointers_of(names: Iterable[str]) -> dict[str, dict[str, int | None]]:
    """Where each model's ``active`` and ``shadow`` pointers stand, by name; None for one unset.

    A model the store cannot answer for -- a bad name, a store that cannot be
    read -- has both unset rather than failing the whole graph.
    """
    store = ArtifactStore.in_state_dir(resolve_state_dir())
    stands: dict[str, dict[str, int | None]] = {}
    for name in names:
        try:
            pointers = store.pointers(name)
        except (ArtifactError, OSError):
            pointers = {}
        stands[name] = {
            which: (pointers[which].version if which in pointers else None)
            for which in ("active", "shadow")
        }
    return stands


async def wiring_handler(_request: web.Request) -> web.Response:
    """The agents, the topics between them, the sources outside, and the warnings."""
    bus = topic_bus.get_topic_bus()
    if runtime.system is None or bus is None:
        return web.json_response({"error": NO_REGISTRY}, status=503)
    contracts = bus.registry.all_contracts()
    named = {name for contract in contracts for name in contract.models.values()}
    # The store is files: read off the event loop. The registry is not, and is
    # read here, on the loop that changes it.
    stands = await asyncio.to_thread(pointers_of, sorted(named))
    graph: dict[str, Any] = wiring(
        contracts,
        bus.registry.outside_samples(),
        pointers=lambda name: stands.get(name, {}),
        asked=asked_by(),
    )
    return web.json_response(graph, dumps=_dumps)


def _dumps(value: Any) -> str:
    """JSON for the graph: a payload seen on the wire may hold what JSON does not, said as text."""
    return json.dumps(value, default=str)
