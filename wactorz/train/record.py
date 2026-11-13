# SPDX-License-Identifier: Apache-2.0
"""A run and the version it made, in the provenance graph.

Each run is an ``action`` node about the model, written ``running`` when the
child starts and set to ``done`` or ``failed`` when it ends; the version stored
is a ``model`` node, joined ``produced`` from the run, ``trained`` from every
label the run's window held, and ``led_to`` the move the run made. A run that
failed has an ``outcome`` saying why, joined ``evaluated_by`` from it, since a
node's payload is never rewritten. ``docs/design/labels-and-provenance.md``
says what each kind means; ``docs/design/train-step.md`` why these.

Calls block on SQLite; from the event loop, go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..core.artifacts import Artifact, Pointer
from ..core.pointer_moves import record_move, source_of
from ..core.provenance import (
    ACTION,
    DONE,
    EVALUATED_BY,
    FAILED,
    LED_TO,
    MODEL,
    OUTCOME,
    PRODUCED,
    RUNNING,
    TRAINED,
    Provenance,
    ProvenanceError,
    ProvNode,
    stable_id,
)
from .spec import TrainSpec

#: What the action did, in its payload's ``what``, and what a failed run's outcome is about.
TRAIN = "train"
TRAIN_FAILED = "train_failed"


def run_node_id(run_id: str) -> str:
    """The id of the action node for the run ``run_id``."""
    return stable_id("train-run", run_id)


def model_node_id(name: str, version: int, sha256: str) -> str:
    """The id of the model node for ``name:version`` holding ``sha256``.

    The hash is part of it because a version's number can be reused after a
    remove, and the node is about the bytes that were stored.
    """
    return stable_id("model", name, version, sha256)


def record_start(prov: Provenance, *, run_id: str, spec: TrainSpec, by: str, at: float) -> ProvNode:
    """Write that the run ``run_id`` started, about the window it reads."""
    return prov.add_node(
        ACTION,
        ts=spec.since,
        ts_end=spec.until,
        status=RUNNING,
        source=source_of(spec.model),
        actor=by,
        payload={
            "what": TRAIN,
            "run_id": run_id,
            "model": spec.model,
            "recipe": spec.recipe,
            "source": spec.source,
            "window": {"source": spec.source, "since": spec.since, "until": spec.until},
            "split": spec.split.to_dict(),
            "settings": dict(spec.settings),
            "seed": spec.seed,
            "metric": spec.metric,
            "margin": spec.margin,
            "pointer": spec.pointer,
            "reason": spec.reason,
        },
        node_id=run_node_id(run_id),
        recorded=at,
    )


def record_model(
    prov: Provenance,
    *,
    run_id: str,
    artifact: Artifact,
    metrics: Mapping[str, Any],
    label_ids: Iterable[str],
    moved: Pointer | None,
    at: float,
) -> ProvNode:
    """Write the version ``artifact`` as a model node, joined from the run, the labels and to the move.

    The action is set ``done``. ``moved`` is the pointer the run set, written
    as the move main would record from the journal, under the same id, so the
    two records are one node; the run leads to it.
    """
    node = prov.add_node(
        MODEL,
        ts=at,
        ts_end=at,
        source=source_of(artifact.name),
        payload={
            "name": artifact.name,
            "version": artifact.version,
            "sha256": artifact.sha256,
            "format": artifact.format,
            "run_id": run_id,
            "metrics": dict(metrics),
        },
        node_id=model_node_id(artifact.name, artifact.version, artifact.sha256),
        recorded=at,
    )
    run = run_node_id(run_id)
    if prov.get(run) is not None:
        prov.add_edge(run, node.id, PRODUCED, recorded=at)
        prov.set_status(run, DONE)
    for label_id in dict.fromkeys(label_ids):
        if label_id and prov.get(label_id) is not None:
            try:
                prov.add_edge(label_id, node.id, TRAINED, recorded=at)
            except ProvenanceError:
                # Not a label after all: the id came from the loader's join.
                continue
    if moved is not None:
        move = record_move(prov, moved)
        if prov.get(run) is not None:
            prov.add_edge(run, move.id, LED_TO, recorded=at)
    return node


def record_failure(prov: Provenance, *, run_id: str, error: str, at: float) -> ProvNode | None:
    """Write why the run ``run_id`` made nothing, and set it ``failed``; None when the run is not in the graph."""
    run = run_node_id(run_id)
    action = prov.get(run)
    if action is None:
        return None
    outcome = prov.add_node(
        OUTCOME,
        ts=at,
        ts_end=at,
        source=action.source,
        payload={"what": TRAIN_FAILED, "run_id": run_id, "error": error},
        node_id=stable_id("train-failed", run_id),
        recorded=at,
    )
    prov.add_edge(run, outcome.id, EVALUATED_BY, recorded=at)
    prov.set_status(run, FAILED)
    return outcome


def runs_of(prov: Provenance, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """The runs that trained ``name``, newest first, as the routes and the agent report them."""
    nodes = [
        node
        for node in prov.nodes(ACTION, source=source_of(name), limit=None)
        if node.payload.get("what") == TRAIN
    ]
    newest_first = sorted(nodes, key=lambda node: (node.recorded, node.seq), reverse=True)
    return [
        {
            "id": node.id,
            "status": node.status,
            "by": node.actor,
            "at": node.recorded,
            **node.payload,
        }
        for node in newest_first[: max(0, limit)]
    ]
