# SPDX-License-Identifier: Apache-2.0
"""A drift alarm in the provenance graph: the ``observation`` node, and reading them back.

An alarm is an episode: raised once, at the first check that crosses the
threshold, keyed by the model, the test and the column, and written as one
``observation`` about the model with what was seen then. The id is the
episode's, so a check that would write the same episode again writes
nothing; the next episode of the same key is a new node. `alarms_of` is what
the route and the Models tab read; `raised_episodes` is what the agent reads
back after a restart, so a shift already recorded is not alarmed again.
``docs/design/drift.md`` is the decision.

Calls block on SQLite; from the event loop, go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any

from ..core.provenance import OBSERVATION, Provenance, ProvNode, model_source, stable_id
from .check import Reading, Window

#: What the observation is about, in its payload's ``what``.
DRIFT = "drift"


def observation_id(model: str, version: int, test: str, column: str, raised_at: float) -> str:
    """The id of the episode of ``test`` on ``column`` of ``model``'s ``version``, raised at ``raised_at``."""
    return stable_id(DRIFT, model, version, test, column, raised_at)


def record_alarm(
    prov: Provenance,
    *,
    model: str,
    version: int,
    reading: Reading,
    window: Window,
    reference: Window,
    by: str,
    at: float,
) -> ProvNode:
    """Write the episode ``reading`` opens as an observation about ``model``, over the window it was seen on."""
    return prov.add_node(
        OBSERVATION,
        ts=window.since,
        ts_end=window.until,
        source=model_source(model),
        actor=by,
        payload={
            "what": DRIFT,
            "model": model,
            "version": version,
            **reading.to_dict(),
            "window": window.to_dict(),
            "reference": reference.to_dict(),
        },
        node_id=observation_id(model, version, reading.test, reading.column, at),
        recorded=at,
    )


def alarms_of(prov: Provenance, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """The drift alarms about ``name``, newest first, as the route reports them."""
    nodes = [
        node
        for node in prov.nodes(OBSERVATION, source=model_source(name), limit=None)
        if node.payload.get("what") == DRIFT
    ]
    newest_first = sorted(nodes, key=lambda node: (node.recorded, node.seq), reverse=True)
    return [
        {"id": node.id, "at": node.recorded, "by": node.actor, **node.payload}
        for node in newest_first[: max(0, limit)]
    ]


def raised_episodes(
    prov: Provenance, name: str, *, version: int, since: float
) -> dict[tuple[str, str], str]:
    """The episodes of ``name``'s ``version`` whose window reaches past ``since``: the test and column of each, with its node.

    What the agent takes as still raised when it starts: an alarm on the
    version served, seen on a window that overlaps the one it will check
    next. One that settled meanwhile is raised again here and settles at the
    first check, which costs nothing but a line.
    """
    found: dict[tuple[str, str], str] = {}
    for node in prov.nodes(OBSERVATION, source=model_source(name), since=since, limit=None):
        payload = node.payload
        if payload.get("what") != DRIFT or payload.get("version") != version:
            continue
        test, column = str(payload.get("test") or ""), str(payload.get("column") or "")
        if test and column:
            found[(test, column)] = node.id
    return found
