"""Sweeping old hours out of main's data directory, on a cadence, when a retention is set.

``WACTORZ_DATA_RETENTION_DAYS`` says how long an hour of readings is kept on
main; unset, everything is kept, which is what an install that has not thought
about it should get. With it set, main sweeps a few times a day
(`wactorz.data.retention.sweep`), keeping the hours a stored model says it was
trained on, and says on the feed what went. A node keeps its own copies only
until main has taken them and a week has passed (`wactorz.node.data_shipping`),
so this is the one place a reading is finally let go.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ...config import DATA_RETENTION_DAYS
from ...core.artifacts import ArtifactStore
from ...core.paths import resolve_state_dir
from ...data.files import data_root
from ...data.retention import Sweep, protected_windows, sweep

if TYPE_CHECKING:
    from .hosts import ArtifactHost

logger = logging.getLogger(__name__)

#: How often main looks for hours past the retention. A sweep that finds
#: nothing costs a directory walk, so a few times a day is plenty.
SWEEP_EVERY_S = 6 * 3600.0

#: How long main waits after starting before its first sweep, so a restart
#: does not begin with a directory walk.
FIRST_SWEEP_AFTER_S = 300.0


class DataRetention:
    """Runs the sweep on a cadence and says what it removed."""

    def __init__(self, host: ArtifactHost | None = None, *, keep_days: float | None = None) -> None:
        self.host = host
        #: Read once: a retention is a deployment setting, not something that
        #: changes while main runs.
        self.keep_days = DATA_RETENTION_DAYS if keep_days is None else keep_days
        self.last: Sweep | None = None

    def _sweep_now(self) -> Sweep:
        state = resolve_state_dir()
        protected = protected_windows(ArtifactStore.in_state_dir(state))
        return sweep(data_root(state), keep_days=self.keep_days, protected=protected)

    async def once(self) -> Sweep:
        """One sweep, off the loop; the report."""
        report = await asyncio.to_thread(self._sweep_now)
        self.last = report
        if report.removed:
            await self._say(
                f"let go of {len(report.removed)} hour(s) of readings older than "
                f"{self.keep_days:g} day(s), {report.bytes} bytes"
                + (f"; kept {report.protected} a model was trained on" if report.protected else "")
            )
        return report

    async def run(self) -> None:
        """Sweep on a cadence, until the actor stops; nothing at all without a retention."""
        host = self.host
        if host is None or self.keep_days <= 0:
            return
        await asyncio.sleep(FIRST_SWEEP_AFTER_S)
        while host.state.value not in ("stopped", "failed"):
            try:
                await self.once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[%s] data retention sweep: %s", host.name, exc)
            await asyncio.sleep(SWEEP_EVERY_S)

    async def _say(self, message: str) -> None:
        host = self.host
        if host is None:
            return
        logger.info("[%s] %s", host.name, message)
        await host._mqtt_publish(
            f"agents/{host.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )
