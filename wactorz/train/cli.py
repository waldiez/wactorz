# SPDX-License-Identifier: Apache-2.0
r"""``wactorz-train``: train a version of a model from recorded readings, from a terminal.

    wactorz-train check examples.imu_anomaly.recipe
    wactorz-train run recipe.py --model imu-detector --source sensors/imu --metric f1 --margin 0.02
    wactorz-train run recipe.py --model imu-detector --source sensors/imu --metric f1 \\
        --since 1760000000 --until 1760086400 --split time:1760050920 --set shadow

``check`` loads a recipe and says what it defines, fitting nothing. ``run``
does what the trainer agent does on a task (``docs/design/train-step.md``):
reads the window from the state directory's data, fits and scores in a child
process, stores the version with its metadata, moves the pointer it is for
when the margin is met, and writes the run into the provenance graph when the
state directory holds main's database. Needs pyarrow, the ``data`` extra.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.labels import Interval, Labels
from ..core.paths import resolve_state_dir
from ..core.persistence.db import WactorzDB
from ..core.provenance import Provenance
from .recipe import load_recipe
from .run import LabelsOf, run
from .spec import (
    DEFAULT_SEED,
    DEFAULT_TIMEOUT_S,
    DEFAULT_WINDOW_S,
    HOLDOUT,
    LEAVE_K_OUT,
    ROLLING_ORIGIN,
    TIME_SPLIT,
    TrainError,
    TrainSpec,
)

logger: logging.Logger = logging.getLogger(__name__)

#: Who the move and the graph say asked.
BY = "cli"

#: The database main keeps in the state directory; a run beside it writes the graph there.
DB_FILE = "wactorz.db"


class NotASplitError(argparse.ArgumentTypeError):
    """A ``--split`` that is not one of the forms the command takes."""

    def __init__(self, text: str) -> None:
        super().__init__(
            f"not a split: {text!r}; holdout[:FRACTION], time:AT, rolling:K or leave:K"
        )


class NotASettingError(argparse.ArgumentTypeError):
    """A ``--setting`` without its ``=``."""

    def __init__(self, text: str) -> None:
        super().__init__(f"not KEY=VALUE: {text!r}")


def _split(text: str) -> dict[str, Any]:
    """``holdout``, ``holdout:0.3``, ``time:AT``, ``rolling:K`` or ``leave:K`` as a split."""
    kind, _, value = text.partition(":")
    if kind == HOLDOUT:
        return {"kind": HOLDOUT, "fraction": float(value) if value else 0.2}
    if kind == "time":
        return {"kind": TIME_SPLIT, "at": float(value)}
    if kind == "rolling":
        return {"kind": ROLLING_ORIGIN, "k": int(value or 1)}
    if kind == "leave":
        return {"kind": LEAVE_K_OUT, "k": int(value or 1)}
    raise NotASplitError(text)


def _settings(pairs: list[str]) -> dict[str, Any]:
    """``KEY=VALUE`` pairs as settings, each value JSON where it parses and text where not."""
    found: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise NotASettingError(pair)
        try:
            found[key] = json.loads(value)
        except ValueError:
            found[key] = value
    return found


def _spec(args: argparse.Namespace, now: float) -> TrainSpec:
    until = float(args.until) if args.until is not None else now
    since = float(args.since) if args.since is not None else until - DEFAULT_WINDOW_S
    return TrainSpec.from_dict(
        {
            "recipe": args.recipe,
            "model": args.model,
            "source": args.source,
            "since": since,
            "until": until,
            "split": args.split,
            "seed": args.seed,
            "settings": _settings(args.setting or []),
            "metric": args.metric or "",
            "margin": args.margin,
            "higher_is_better": not args.lower_is_better,
            "pointer": args.set,
            "labels": True if args.labels else None,
            "timeout_s": args.timeout,
            "memory_mb": args.memory_mb,
            "reason": args.reason or "",
        }
    )


async def _run(args: argparse.Namespace) -> str | None:
    """Carry out one run; why it could not, or None."""
    state = Path(resolve_state_dir(args.state_dir))
    try:
        spec = _spec(args, time.time())
    except (TrainError, argparse.ArgumentTypeError) as exc:
        return str(exc)
    db_path = state / DB_FILE
    db = WactorzDB(db_path) if db_path.is_file() else None
    try:
        outcome = await run(
            spec,
            state_dir=state,
            by=BY,
            prov=Provenance(db) if db else None,
            labels_of=_labels_reader(db) if db else None,
        )
    finally:
        if db is not None:
            db.close()
    if args.json:
        logger.info(json.dumps(outcome.to_dict(), indent=2))
    else:
        logger.info("%s", outcome.summary())
        if outcome.ok:
            logger.info("metrics: %s", json.dumps(outcome.metrics.get("new", {})))
            if outcome.metrics.get("parent") is not None:
                logger.info("%s: %s", outcome.parent, json.dumps(outcome.metrics.get("parent", {})))
        logger.info("run %s: %s", outcome.run_id, outcome.run_dir)
    if db is None:
        logger.info("no %s in %s: the run is not in the provenance graph", DB_FILE, state)
    return None if outcome.ok else outcome.error


def _labels_reader(db: WactorzDB) -> LabelsOf:
    labels = Labels(db)

    def effective(source: str, since: float, until: float) -> Sequence[Interval]:
        return labels.effective(source, since=since, until=until)

    return effective


def _check(args: argparse.Namespace) -> str | None:
    try:
        recipe = load_recipe(args.recipe)
    except TrainError as exc:
        return str(exc)
    logger.info(
        "%s: train, score%s; source %s (%s)",
        recipe.name,
        ", save" if recipe.save else " (save: a mapping of numpy arrays as .npz)",
        recipe.path or "-",
        recipe.code_sha256[:12] or "no file",
    )
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wactorz-train", description="Train a version of a model from recorded readings."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="load a recipe and say what it defines")
    check.add_argument("recipe", help="a module name, or a .py file")
    running = commands.add_parser(
        "run", help="train, score, store, and move when the margin is met"
    )
    running.add_argument("recipe", help="a module name, or a .py file")
    running.add_argument("--model", required=True, help="the name the version is stored under")
    running.add_argument("--source", required=True, help="the recorded source to read")
    running.add_argument(
        "--since", type=float, metavar="SECONDS", help="default: a week before until"
    )
    running.add_argument("--until", type=float, metavar="SECONDS", help="default: now")
    running.add_argument(
        "--split",
        type=_split,
        default={"kind": HOLDOUT},
        help="holdout[:FRACTION], time:AT, rolling:K or leave:K (default: holdout, the last fifth)",
    )
    running.add_argument("--metric", help="the figure of score() the move depends on")
    running.add_argument("--margin", type=float, default=0.0, help="how much better it must be")
    running.add_argument("--lower-is-better", action="store_true", help="a lower figure is better")
    running.add_argument(
        "--set",
        choices=("shadow", "active", "none"),
        default="shadow",
        help="the pointer the run is for (default: shadow); none stores only",
    )
    running.add_argument("--seed", type=int, default=DEFAULT_SEED)
    running.add_argument(
        "--setting", action="append", metavar="KEY=VALUE", help="a setting for train()"
    )
    running.add_argument(
        "--labels", action="store_true", help="join the labels whatever the model says"
    )
    running.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, metavar="SECONDS")
    running.add_argument("--memory-mb", type=int, default=0, help="address-space cap; 0 for none")
    running.add_argument("--reason", help="why, for the move's record")
    running.add_argument("--state-dir", metavar="DIR", help="default: WACTORZ_STATE_DIR or ./state")
    running.add_argument("--json", action="store_true", help="the outcome as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``wactorz-train`` with ``argv`` (the process's arguments when None); 0 on success, 1
    after logging what failed.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = _build_parser().parse_args(argv)
    failure = _check(args) if args.command == "check" else asyncio.run(_run(args))
    if failure is not None:
        logger.error("wactorz-train: %s", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
