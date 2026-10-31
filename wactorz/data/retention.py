# SPDX-License-Identifier: Apache-2.0
"""Letting old hours go, except the ones a model was trained on.

The data directory keeps every hour of every source until something removes
it. `sweep` removes the hours older than a retention, source by source, and
keeps whatever a stored model's record says it learned from: a version's
metadata may carry ``data``, one window or a list of them, each
``{"source": ..., "since": ..., "until": ...}`` in event time, which the train
step writes and `protected_windows` reads back. An hour inside such a window
stays however old it is, so a model's training data outlives the retention
and the trial that chose it can be read again. Removed, an hour is gone: there
is no second copy, which is what the retention is for.

Nothing here needs pyarrow: it works on the files and their names.
``docs/design/data-storage.md`` is the decision.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.artifacts import ArtifactStore
from .files import HOUR_S, hours_of, sources

logger = logging.getLogger(__name__)

#: The metadata key a stored version names its training windows under.
DATA_KEY = "data"

#: One stretch of one source that must stay: ``(source, since, until)``, event time.
Window = tuple[str, float, float]


@dataclass
class Sweep:
    """What a sweep did, or would do: the hours removed, by source, and what stayed for a model."""

    removed: list[tuple[str, float]] = field(default_factory=list)
    bytes: int = 0
    protected: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed": [{"source": s, "hour": h} for s, h in self.removed],
            "hours": len(self.removed),
            "bytes": self.bytes,
            "protected": self.protected,
            "dry_run": self.dry_run,
        }


def _windows_in(value: Any) -> list[Window]:
    """The windows one metadata ``data`` entry names, however it is shaped."""
    entries = value if isinstance(value, list) else [value]
    found: list[Window] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        source, since, until = entry.get("source"), entry.get("since"), entry.get("until")
        if (
            isinstance(source, str)
            and isinstance(since, (int, float))
            and isinstance(until, (int, float))
            and not isinstance(since, bool)
            and not isinstance(until, bool)
        ):
            found.append((source, float(since), float(until)))
    return found


def protected_windows(store: ArtifactStore) -> list[Window]:
    """Every window a stored version says it was trained on, from every name's versions."""
    found: list[Window] = []
    for name in store.names():
        for version in store.versions(name):
            found.extend(_windows_in(version.metadata.get(DATA_KEY)))
    return found


def is_protected(source: str, hour: float, windows: Iterable[Window]) -> bool:
    """Whether the hour starting at ``hour`` of ``source`` overlaps a window that must stay."""
    end = hour + HOUR_S
    return any(s == source and since < end and until > hour for s, since, until in windows)


def sweep(
    root: Path,
    *,
    keep_days: float,
    protected: Sequence[Window] = (),
    now: float | None = None,
    dry_run: bool = False,
) -> Sweep:
    """Remove every hour that ended more than ``keep_days`` ago, unless a window protects it.

    ``keep_days`` of zero or less removes nothing: no retention is the default.
    With ``dry_run`` nothing is removed and the report says what would be.
    """
    report = Sweep(dry_run=dry_run)
    if keep_days <= 0:
        return report
    cutoff = (time.time() if now is None else now) - keep_days * 86400.0
    for source in sources(root):
        for hour, paths in hours_of(root, source):
            if hour + HOUR_S > cutoff:
                continue
            if is_protected(source, hour, protected):
                report.protected += 1
                continue
            size = sum(p.stat().st_size for p in paths if p.is_file())
            if not dry_run:
                for path in paths:
                    path.unlink(missing_ok=True)
                _prune_empty(paths[0].parent, root)
            report.removed.append((source, hour))
            report.bytes += size
    if report.removed:
        logger.info(
            "[data] %s %s hour(s), %s bytes, of %s source(s); %s kept for a model",
            "would remove" if dry_run else "removed",
            len(report.removed),
            report.bytes,
            len({s for s, _ in report.removed}),
            report.protected,
        )
    return report


def _prune_empty(directory: Path, root: Path) -> None:
    """Remove ``directory`` and its parents while they are empty, up to ``root``."""
    current = directory
    while current != root and current.is_dir():
        if any(current.iterdir()):
            return
        current.rmdir()
        current = current.parent
