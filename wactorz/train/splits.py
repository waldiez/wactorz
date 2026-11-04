# SPDX-License-Identifier: Apache-2.0
"""The folds a run trains and scores on, cut by the loader from the window's frame.

A `SplitSpec` names one of the loader's splits (`wactorz.data.frames`), none of
which shuffles rows, and this turns it into folds over a frame: a hold-out of
the last rows, a cut at one moment, forward folds, or segments held out. Each
fold carries its manifest, which is stored with the version so the split can
be made again. Needs pyarrow, through the loader.
"""

from __future__ import annotations

from typing import Any

from ..data.frames import (
    Fold,
    leave_k_out,
    rolling_origin,
    segments,
    time_split,
)
from ..data.parquet import TS
from .spec import HOLDOUT, LEAVE_K_OUT, ROLLING_ORIGIN, TIME_SPLIT, SplitSpec, TrainError


def folds_for(frame: Any, split: SplitSpec) -> list[Fold]:
    """The folds ``split`` makes of ``frame``, each with rows on both sides. Raises `TrainError`."""
    if frame.num_rows == 0:
        raise TrainError.nothing_to_fit()
    if split.kind == HOLDOUT:
        folds = [_holdout(frame, split.fraction)]
    elif split.kind == TIME_SPLIT:
        folds = [time_split(frame, float(split.at or 0.0))]
    elif split.kind == ROLLING_ORIGIN:
        folds = rolling_origin(frame, split.k)
    elif split.kind == LEAVE_K_OUT:
        segs = segments(
            frame,
            split.by,
            gap_s=split.gap_s,
            length_s=split.length_s,
            column=split.column,
        )
        folds = leave_k_out(segs, split.k, seed=split.seed)
    else:
        raise TrainError.unknown_split(split.kind)
    for fold in folds:
        if not _rows(fold.train):
            raise TrainError.nothing_to_fit()
        if not _rows(fold.test):
            raise TrainError.nothing_to_score()
    return folds


def _holdout(frame: Any, fraction: float) -> Fold:
    """The last ``fraction`` of the rows scored on: a time split at the row where they begin."""
    rows = frame.num_rows
    cut = min(rows - 1, max(1, round(rows * (1.0 - fraction))))
    if rows < 2:
        raise TrainError.nothing_to_score()
    at = float(frame.column(TS)[cut].as_py())
    fold = time_split(frame, at)
    return Fold(
        train=fold.train,
        test=fold.test,
        kind=HOLDOUT,
        params={**fold.params, "fraction": fraction},
    )


def _rows(ranges: tuple[tuple[int, int], ...]) -> int:
    return sum(max(0, end - start) for start, end in ranges)
