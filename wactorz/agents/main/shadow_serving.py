"""Starting and ending a model's shadow trial in the agents that serve it.

A trial begins when a model's ``shadow`` pointer is set and ends when it is
cleared or promoted. The pointer watcher hands each such change here, as it
hands every change to the hot swap. For a set pointer, every function agent on
main serving the model by its bare name is given the version to try beside the
one it serves (`FunctionAgent.swap_shadow`); for a cleared one, it drops it.
Then the trial's pairs, kept under ``shadow/<model>`` in the data directory,
are read back and compared (`wactorz.core.shadow_trial`), and the result is
written as an ``outcome`` node about the model, whichever way the trial ended,
with one line on the feed saying what it showed.

Agents on nodes run no shadow yet: their pairs would be kept where main
cannot read them, until the data package ships files to main. The hot swap
already moves the served version when ``active`` moves, promotion included;
this only ever touches the shadow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...core.artifacts import ArtifactError, ArtifactStore, Pointer
from ...core.paths import resolve_state_dir
from ...core.persistence.stores import get_db
from ...core.provenance import Provenance
from ...core.shadow_trial import CLEARED, PROMOTED, compare, record_outcome
from ...data.files import data_root

if TYPE_CHECKING:
    from .artifact_service import Change
    from .hosts import SwapHost

logger = logging.getLogger(__name__)

#: The pointer this follows.
SHADOW = "shadow"


def _following_active(actor: Any, name: str) -> list[str]:
    """The keys under which ``actor`` serves ``name`` by following its ``active`` pointer."""
    artifacts = getattr(actor, "artifacts", None)
    if not isinstance(artifacts, dict):
        return []
    return [
        key
        for key, entry in artifacts.items()
        if entry.get("name") == name and entry.get("follows") == "active"
    ]


class ShadowServing:
    """Gives the agents serving a model its shadow version, and records what the trial showed."""

    def __init__(self, host: SwapHost | None = None) -> None:
        self.host = host

    @property
    def store(self) -> ArtifactStore:
        """The store, read where the state directory is at the time of asking."""
        return ArtifactStore.in_state_dir(resolve_state_dir())

    async def follow(self, changes: list[Change]) -> None:
        """Start or end a trial in the serving agents, for each ``shadow`` pointer that changed."""
        host = self.host
        if host is None:
            return
        for name, which, now, was in changes:
            if which != SHADOW:
                continue
            try:
                entry = await self._shadow_entry(name) if now is not None else None
            except ArtifactError as exc:
                await self._say(f"{name}:shadow cannot be tried: {exc}")
                continue
            for actor in self._serving(name):
                for key in _following_active(actor, name):
                    try:
                        await actor.swap_shadow(key, entry)
                    except Exception as exc:
                        await self._say(f"{actor.name} cannot try {name}:shadow as {key}: {exc}")
            if now is None and was is not None:
                await self._ended(name, was)

    def _serving(self, name: str) -> list[Any]:
        """The agents here that serve ``name`` and can run a shadow beside it."""
        host = self.host
        if host is None or not host._registry:
            return []
        return [
            actor
            for actor in host._registry.all_actors()
            if callable(getattr(actor, "swap_shadow", None)) and _following_active(actor, name)
        ]

    async def _shadow_entry(self, name: str) -> dict[str, Any]:
        """``name:shadow`` pinned, with the path it is read at on main."""

        def resolve() -> dict[str, Any]:
            store = self.store
            pinned = store.pin(f"{name}:shadow")
            return {**pinned, "path": str(store.readable_path(pinned))}

        return await asyncio.to_thread(resolve)

    # ── The end of a trial ────────────────────────────────────────────────

    async def _ended(self, name: str, was: Pointer) -> None:
        """Compare the trial's pairs and write its outcome; say what it showed."""
        until = time.time()
        active = await asyncio.to_thread(self.store.pointer, name, "active")
        promoted = active is not None and active.version == was.version
        ended = PROMOTED if promoted else CLEARED
        active_version = None
        if active is not None:
            active_version = active.previous if promoted else active.version
        by = active.by if promoted and active is not None else ""
        summary = await self._compare(name, was.moved_at, until)
        pairs = summary.get("pairs", 0)
        agreement = summary.get("agreement")
        shown = f"{pairs} pair(s)" + (
            f", agreed on {agreement:.0%}" if isinstance(agreement, float) else ""
        )
        await self._say(f"the trial of {was.reference} ended ({ended}): {shown}")
        db = get_db()
        if db is None:
            return
        try:
            await asyncio.to_thread(
                record_outcome,
                Provenance(db),
                name=name,
                shadow_version=was.version,
                active_version=active_version,
                since=was.moved_at,
                until=until,
                ended=ended,
                summary=summary,
                by=by,
            )
        except Exception as exc:
            logger.warning("cannot record the outcome of %s's trial: %s", name, exc)

    async def _compare(self, name: str, since: float, until: float) -> dict[str, Any]:
        """What the pairs of ``name``'s trial over ``[since, until)`` show; counts alone without pyarrow."""
        try:
            from ...data import parquet  # optional dependency: the `data` extra
        except ModuleNotFoundError:
            return {**compare([]), "pairs_unavailable": "install wactorz[data]"}
        root = data_root(resolve_state_dir())

        def read() -> list[dict[str, Any]]:
            return parquet.frame(root, f"shadow/{name}", since, until).to_pylist()

        try:
            rows = await asyncio.to_thread(read)
        except Exception as exc:
            logger.warning("cannot read the pairs of %s's trial: %s", name, exc)
            return {**compare([]), "pairs_unavailable": str(exc)}
        return compare(rows)

    async def _say(self, message: str) -> None:
        """One line on the feed, as a pointer's move is one."""
        host = self.host
        if host is None:
            return
        logger.info("[%s] %s", host.name, message)
        await host._mqtt_publish(
            f"agents/{host.actor_id}/logs",
            {"type": "model", "message": message, "timestamp": time.time()},
        )
