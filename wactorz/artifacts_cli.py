"""wactorz-artifacts  —  keep models on main, by name and version.

An artifact is a model, a set of weights or any large file that agents load and
nobody writes to: it changes by gaining a new version. See
`wactorz.core.artifacts` for how they are kept.

Usage
-----
    wactorz-artifacts add PATH NAME [--format F] [--meta KEY=VALUE]... [--allow-code]
    wactorz-artifacts list [NAME]
    wactorz-artifacts show REFERENCE
    wactorz-artifacts remove REFERENCE [--every-version]
    wactorz-artifacts verify [REFERENCE]

A reference is ``NAME`` or ``NAME:latest`` for the newest version, or
``NAME:3`` for the third. Every command takes ``--state-dir``; the default is
``WACTORZ_STATE_DIR``, else ``./state``, as for the server.

The format is guessed from the file's ending; ``--format`` names it when the
ending does not say, as for a ``.pt`` file, which may hold a ``state_dict`` or
a whole module. A directory is kept as one archive of it. Formats that run code
when they load -- a pickle, a joblib file, a whole torch module -- are refused
unless ``--allow-code`` is given.

Adding and removing work while the server runs.
"""

import argparse
import datetime
import json
import logging
import sys
from typing import Any

from wactorz.core.artifacts import (
    FORMATS,
    Artifact,
    ArtifactError,
    ArtifactStore,
    CodeRefusedError,
    describe,
    human_size,
    parse_reference,
)
from wactorz.core.paths import resolve_state_dir

logger = logging.getLogger(__name__)


def _store(args: argparse.Namespace) -> ArtifactStore:
    return ArtifactStore.in_state_dir(resolve_state_dir(args.state_dir))


def _metadata(pairs: list[str]) -> dict[str, Any]:
    """``KEY=VALUE`` pairs as metadata, each value read as JSON where it is JSON."""
    metadata: dict[str, Any] = {"source": "cli"}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ArtifactError(f"{pair!r} is not KEY=VALUE")
        try:
            metadata[key] = json.loads(value)
        except ValueError:
            metadata[key] = value
    return metadata


def _line(artifact: Artifact) -> str:
    # In this machine's time, as whoever reads the list expects.
    added = (
        datetime.datetime.fromtimestamp(artifact.added_at, tz=datetime.timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )
    code = "  runs code" if artifact.runs_code else ""
    return (
        f"{artifact.reference:<32} {artifact.format:<12} {_size(artifact.size):>10}  "
        f"{added}  {artifact.sha256[:12]}{code}"
    )


_size = human_size


def _add(args: argparse.Namespace) -> None:
    store = _store(args)
    # Asked before adding: the add makes it. A state directory that is not
    # there yet is usually one the server never reads, from a command run in
    # another folder, and the model would land where nothing looks for it.
    new_state_dir = not store.root.parent.is_dir()
    try:
        artifact = store.add(
            args.path,
            args.name,
            format_name=args.format,
            metadata=_metadata(args.meta),
            allow_code=args.allow_code,
        )
    except CodeRefusedError as exc:
        raise ArtifactError(f"{exc}. Add --allow-code to store it anyway.") from exc
    logger.info("Stored %s (%s, %s)", artifact.pinned, artifact.format, _size(artifact.size))
    logger.info("in %s", store.root.resolve())
    if new_state_dir:
        logger.warning(
            "There was no state directory at %s, so a new one was made. Wactorz reads its "
            "models from its own state directory: run this where Wactorz runs, or pass "
            "--state-dir, or set WACTORZ_STATE_DIR for both.",
            store.root.parent.resolve(),
        )
    if artifact.runs_code:
        logger.warning(
            "It runs code when it loads: load it only where you would run its maker's code."
        )


def _list(args: argparse.Namespace) -> None:
    store = _store(args)
    names = [parse_reference(args.name)[0]] if args.name else store.names()
    found = [artifact for name in names for artifact in store.versions(name)]
    logger.info("Models in %s:", store.root.resolve())
    if not found:
        logger.info("  none%s.", f" named {args.name!r}" if args.name else "")
        return
    for artifact in found:
        logger.info("%s", _line(artifact))


def _show(args: argparse.Namespace) -> None:
    artifact = _store(args).resolve(args.reference)
    sys.stdout.write(json.dumps(describe(artifact), indent=2) + "\n")


def _remove(args: argparse.Namespace) -> None:
    for artifact in _store(args).remove(args.reference, every_version=args.every_version):
        logger.info("Removed %s", artifact.reference)


def _verify(args: argparse.Namespace) -> None:
    store = _store(args)
    if args.reference:
        checked = [store.resolve(args.reference)]
    else:
        checked = [artifact for name in store.names() for artifact in store.versions(name)]
    problems = [problem for artifact in checked if (problem := store.verify(artifact))]
    for problem in problems:
        logger.error("%s", problem)
    if problems:
        raise ArtifactError(f"{len(problems)} of {len(checked)} artifact(s) do not match")
    logger.info("%s artifact(s) match what was stored.", len(checked))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wactorz-artifacts",
        description="Keep models and other large files on main, by name and version.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    state_dir = argparse.ArgumentParser(add_help=False)
    state_dir.add_argument(
        "--state-dir", metavar="DIR", help="default: WACTORZ_STATE_DIR or ./state"
    )

    add = commands.add_parser("add", parents=[state_dir], help="store a file or directory")
    add.add_argument("path", help="the file or directory to store")
    add.add_argument("name", help="the name to store it under; each add is a new version")
    add.add_argument("--format", choices=sorted(FORMATS), help="default: guessed from the ending")
    add.add_argument(
        "--meta",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="say where it came from or how good it is: dataset=..., metric=0.93; repeatable",
    )
    add.add_argument(
        "--allow-code",
        action="store_true",
        help="store a format that runs code when it loads (pickle, joblib, torch)",
    )
    listing = commands.add_parser("list", parents=[state_dir], help="list what is stored")
    listing.add_argument("name", nargs="?", help="only this name's versions")
    show = commands.add_parser("show", parents=[state_dir], help="one version, as JSON")
    show.add_argument("reference", help="NAME, NAME:latest or NAME:3")
    remove = commands.add_parser("remove", parents=[state_dir], help="remove a version")
    remove.add_argument("reference", help="NAME:3 or NAME:latest; NAME with --every-version")
    remove.add_argument("--every-version", action="store_true", help="remove every version of NAME")
    verify = commands.add_parser(
        "verify", parents=[state_dir], help="check stored files still match their hashes"
    )
    verify.add_argument("reference", nargs="?", help="default: every version of every name")
    return parser


_COMMANDS = {"add": _add, "list": _list, "show": _show, "remove": _remove, "verify": _verify}


def _run(args: argparse.Namespace) -> Exception | None:
    """Do what ``args`` asks; the reason it could not, or None."""
    try:
        _COMMANDS[args.command](args)
    except (ArtifactError, OSError) as exc:
        return exc
    return None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    failure = _run(_build_parser().parse_args(argv))
    if failure is not None:
        logger.error("wactorz-artifacts: %s", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
