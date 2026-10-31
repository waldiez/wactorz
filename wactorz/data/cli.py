# SPDX-License-Identifier: Apache-2.0
"""``wactorz-data``: what is recorded, what a window of it holds, and a copy for a notebook.

    wactorz-data sources                                  # every source with files
    wactorz-data profile sensors/imu/left-wrist           # the last day's dataset card
    wactorz-data profile sensors/imu/left-wrist --since 1760000000 --until 1760086400 --json
    wactorz-data export sensors/imu/left-wrist out.parquet --since ... --until ...

Reads the data directory under the state directory (``--state-dir``, else
``WACTORZ_STATE_DIR``, else ``./state``), beside the server or on a node. Needs
pyarrow, the ``data`` extra, for anything but ``sources``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.paths import resolve_state_dir
from .files import DataError, check_source, data_root, sources

logger = logging.getLogger(__name__)

#: How far back a profile or an export looks when the window is unsaid.
DEFAULT_WINDOW_S = 24 * 3600.0


def _moment(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _window(args: argparse.Namespace) -> tuple[float, float]:
    until = float(args.until) if args.until is not None else time.time()
    since = float(args.since) if args.since is not None else until - DEFAULT_WINDOW_S
    return since, until


def _card_lines(card: dict[str, Any]) -> list[str]:
    """The dataset card as a person reads it."""
    lines = [f"{card.get('source') or 'frame'}: {card['rows']} rows"]
    if not card["rows"]:
        return lines
    lines.append(
        f"  from {_moment(card['from'])} to {_moment(card['to'])} ({card['span_s']:.0f} s)"
    )
    rate = card.get("rate_per_s")
    lines.append(
        f"  rate {rate:.3f}/s, median step {card['median_step_s']:.3f} s, "
        f"gaps {card['gaps']['count']} (longest {card['gaps']['longest_s']:.0f} s)"
        if rate is not None
        else f"  gaps {card['gaps']['count']}"
    )
    for name, column in card["columns"].items():
        if column["type"] == "number":
            q = column["quantiles"]
            lines.append(
                f"  {name}: number, {column['count']} values, {column['missing']} missing, "
                f"min {column['min']:.4g}, p50 {q['0.5']:.4g}, max {column['max']:.4g}, "
                f"mean {column['mean']:.4g}"
            )
        else:
            top = ", ".join(f"{t['value']} ({t['count']})" for t in column["top"])
            lines.append(
                f"  {name}: {column['type']}, {column['count']} values, {column['missing']} missing, "
                f"{column['distinct']} distinct: {top}"
            )
    return lines


def _run(args: argparse.Namespace) -> str | None:
    """Carry out one command; why it could not, or None."""
    root = data_root(resolve_state_dir(args.state_dir))
    if args.command == "sources":
        for source in sources(root):
            logger.info(source)
        return None
    try:
        check_source(args.source)
    except DataError as exc:
        return str(exc)
    try:
        from . import frames, parquet  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return "the data files cannot be read: install wactorz[data]"
    since, until = _window(args)
    table = parquet.frame(root, args.source, since, until)
    if args.command == "profile":
        card = frames.profile(table, source=args.source)
        if args.json:
            logger.info(json.dumps({**card, "since": since, "until": until}, indent=2))
        else:
            logger.info("\n".join(_card_lines(card)))
        return None
    written = frames.export(table, Path(args.path))
    logger.info("%s rows of %s written to %s", table.num_rows, args.source, written)
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wactorz-data", description="What is recorded, and what a window of it holds."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", metavar="DIR", help="default: WACTORZ_STATE_DIR or ./state")
    window = argparse.ArgumentParser(add_help=False)
    window.add_argument(
        "--since", type=float, metavar="SECONDS", help="Unix seconds; default: a day before until"
    )
    window.add_argument("--until", type=float, metavar="SECONDS", help="Unix seconds; default: now")

    commands.add_parser("sources", parents=[common], help="list every source with files")
    profile = commands.add_parser(
        "profile", parents=[common, window], help="describe a window of a source"
    )
    profile.add_argument("source", help="sensors/imu/left-wrist, shadow/imu-detector, ...")
    profile.add_argument("--json", action="store_true", help="the card as JSON")
    export = commands.add_parser(
        "export", parents=[common, window], help="write a window of a source as one Parquet file"
    )
    export.add_argument("source")
    export.add_argument("path", help="where to write it")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    failure = _run(_build_parser().parse_args(argv))
    if failure is not None:
        logger.error("wactorz-data: %s", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
