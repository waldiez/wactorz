"""wactorz-state  —  export a state directory to one file, and import it back.

What a server or a node remembers lives in its state directory: the database,
each agent's state file and blobs, uploads, the install's identity and, unless
left out, its keys. An export is that directory in one archive, to move an
install to another machine or keep a copy of it; an import puts one back.

Usage
-----
    wactorz-state export [ARCHIVE] [--state-dir DIR] [--no-secrets]
    wactorz-state import ARCHIVE [--state-dir DIR] [--replace]

**An export can be taken while the system runs.** The database is copied with
SQLite's own backup, which is consistent however busy it is; every other file
is replaced in one step whenever it is written, so a copy is never half of one.
What was persisted in the last second may be missing, as after a power cut.

**An import cannot.** A running process keeps much of its state in memory and
writes it back, over whatever was imported. Import refuses while a process
holds the directory's lock (see `wactorz.core.state_lock`), and refuses a
directory that is not empty unless told to replace it, which keeps the old one
beside it rather than deleting it.

**An archive is code.** The agents' state files are pickles, unpickled when the
system starts, and some blobs load by unpickling too: importing an archive runs
whatever its maker put in it. Import only archives you made. What import does
check is that the archive is whole and holds nothing but the files it lists,
each matching its SHA-256, at a path inside the directory -- which catches a
damaged or truncated archive, not a forged one.

**An export holds the install's keys** unless ``--no-secrets`` is given: the key
main signs what it sends nodes with, and the broker's TLS keys. With them, a
restored install carries on as before -- nodes accept it, the broker's
certificate still verifies. Without them, it mints new ones, and every node has
to be deployed again. An archive with keys is written readable by its owner
only, and should be kept like the keys themselves.

Left out always: logs, the MQTT outbox (what it holds would be stale on the way
back), dashboard sign-in sessions, files part way through being written or
received, and the lock.
"""

import argparse
import contextlib
import datetime
import enum
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

from wactorz import __version__
from wactorz.core.blob_transfer import INBOX_DIRNAME
from wactorz.core.paths import resolve_state_dir
from wactorz.core.state_lock import LOCK_FILE, in_use

logger = logging.getLogger(__name__)

#: The archive layout this module writes, and the only one it reads.
FORMAT = 1

#: Where the archive lists what it holds, and the directory the files sit under.
MANIFEST = "manifest.json"
PREFIX = "state"

#: Never exported: regenerated, stale on the way back, or not state at all.
_LEFT_OUT_NAMES = frozenset({LOCK_FILE, "sessions.json", INBOX_DIRNAME})
_LEFT_OUT_PREFIXES = ("mqtt_outbox.db",)
_LEFT_OUT_SUFFIXES = (".log", ".part", "-wal", "-shm", "-journal")

#: The install's keys: left out by ``--no-secrets``. Matched on the path.
_SECRET_FILES = frozenset({"node_signing.key"})
_SECRET_DIR_SUFFIXES = {"mqtt_tls": ".key"}

#: The databases, copied through SQLite rather than read as files.
_DATABASE_SUFFIX = ".db"

_HASH_CHUNK = 1 << 20


class ArchiveError(Exception):
    """An archive that cannot be imported, or a state directory it cannot go into."""


class Why(enum.Enum):
    """What makes a file not a state archive, as the message says it."""

    NO_MANIFEST = f"it has no {MANIFEST}"
    MANIFEST_NOT_A_FILE = f"its {MANIFEST} is not a file"
    MANIFEST_UNREADABLE = f"its {MANIFEST} does not parse"
    OTHER_FORMAT = f"it is not in format {FORMAT}"
    NO_FILES = f"its {MANIFEST} lists no files"


class NotAnArchive(ArchiveError):
    def __init__(self, why: Why) -> None:
        super().__init__(f"not a Wactorz state archive: {why.value}")


class NoStateDirectory(ArchiveError):
    def __init__(self, directory: Path) -> None:
        super().__init__(f"there is no state directory at {directory}")


class DamagedArchive(ArchiveError):
    def __init__(self, path: str, why: str) -> None:
        super().__init__(f"the archive is damaged or altered at {path!r}: {why}")


class DirectoryInUse(ArchiveError):
    def __init__(self, directory: Path) -> None:
        super().__init__(
            f"a Wactorz process is running on {directory}; stop it before importing into it"
        )


class DirectoryNotEmpty(ArchiveError):
    def __init__(self, directory: Path) -> None:
        super().__init__(
            f"{directory} is not empty. Import with --replace to keep it beside the import "
            "as a copy and put the archive in its place."
        )


class ArchiveExists(ArchiveError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists already; name another file")


class ArchiveInsideState(ArchiveError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} is inside the state directory it would hold; put it elsewhere")


@dataclass
class Exported:
    """What an export wrote."""

    path: Path
    files: int = 0
    size: int = 0
    secrets: bool = True
    #: The keys it holds, by path, so the caller can say so.
    secret_files: list[str] = field(default_factory=list)


@dataclass
class Imported:
    """What an import put where."""

    directory: Path
    files: int = 0
    #: Where the directory that was there went, when it was replaced.
    previous: Path | None = None
    secrets: bool = False


# ── Choosing what goes ────────────────────────────────────────────────────────


def _left_out(relative: PurePosixPath) -> bool:
    name = relative.name
    if any(part in _LEFT_OUT_NAMES for part in relative.parts):
        return True
    if name.startswith(_LEFT_OUT_PREFIXES) or name.endswith(_LEFT_OUT_SUFFIXES):
        return True
    # A write in progress (`.name.pid.uuid.tmp`) and a quarantined unreadable
    # file (`name.corrupt.<time>`): neither is what the agent remembers.
    return (name.startswith(".") and name.endswith(".tmp")) or ".corrupt." in name


def is_secret(relative: PurePosixPath) -> bool:
    """Whether the file at ``relative`` in a state directory is one of the install's keys."""
    if str(relative) in _SECRET_FILES:
        return True
    parent = relative.parent.name if len(relative.parts) == 2 else ""
    suffix = _SECRET_DIR_SUFFIXES.get(parent)
    return suffix is not None and relative.name.endswith(suffix)


def _files(state_dir: Path, *, secrets: bool) -> Iterator[tuple[Path, PurePosixPath]]:
    """Each file to export, with its path inside the directory, in a stable order."""
    for root, dirs, names in os.walk(state_dir):
        here = Path(root)
        dirs.sort()
        # Never into a directory left out, nor along a link out of the tree.
        dirs[:] = [d for d in dirs if d not in _LEFT_OUT_NAMES and not (here / d).is_symlink()]
        for name in sorted(names):
            path = here / name
            relative = PurePosixPath(path.relative_to(state_dir).as_posix())
            if path.is_symlink() or not path.is_file() or _left_out(relative):
                continue
            if not secrets and is_secret(relative):
                continue
            yield path, relative


# ── Export ────────────────────────────────────────────────────────────────────


def default_archive_name() -> str:
    return f"wactorz-state-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def export_state(
    archive: str | os.PathLike[str],
    state_dir: str | os.PathLike[str] | None = None,
    *,
    secrets: bool = True,
) -> Exported:
    """Write the state directory to ``archive``; see the module docstring."""
    source = Path(state_dir or resolve_state_dir()).resolve()
    target = Path(archive).resolve()
    if not source.is_dir():
        raise NoStateDirectory(source)
    if target.exists():
        raise ArchiveExists(target)
    if source == target.parent or source in target.parents:
        raise ArchiveInsideState(target)
    result = Exported(target, secrets=secrets)
    listed: dict[str, dict[str, Any]] = {}
    partial = target.with_name(f".{target.name}.part")
    # Owner-only from the first byte: it may hold the install's keys.
    descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for path, relative in _files(source, secrets=secrets):
                entry = _add(tar, path, relative)
                listed[str(relative)] = entry
                result.files += 1
                result.size += entry["size"]
                if is_secret(relative):
                    result.secret_files.append(str(relative))
            _add_bytes(tar, MANIFEST, _manifest(listed, secrets=secrets))
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return result


def _add(tar: tarfile.TarFile, path: Path, relative: PurePosixPath) -> dict[str, Any]:
    """Add one file under the archive's prefix, and return its manifest entry."""
    mode = path.stat().st_mode
    if relative.suffix != _DATABASE_SUFFIX:
        return _add_file(tar, path, relative, mode=mode)
    with tempfile.TemporaryDirectory(prefix="wactorz-export-") as scratch:
        copy = _database_copy(path, Path(scratch))
        # A file named like a database that is not one is kept as it is.
        return _add_file(tar, copy or path, relative, mode=mode)


def _add_file(
    tar: tarfile.TarFile, path: Path, relative: PurePosixPath, *, mode: int
) -> dict[str, Any]:
    digest = _sha256(path)
    info = tar.gettarinfo(str(path), arcname=f"{PREFIX}/{relative}")
    info.mode = mode & 0o777
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    with path.open("rb") as file:
        tar.addfile(info, file)
    return {"sha256": digest, "size": info.size}


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _database_copy(path: Path, scratch: Path) -> Path | None:
    """A consistent copy in ``scratch`` of the SQLite database at ``path``, however busy.

    None when ``path`` is not a SQLite database.
    """
    copy = scratch / path.name
    source = sqlite3.connect(path)
    try:
        destination = sqlite3.connect(copy)
        try:
            source.backup(destination)
        except sqlite3.DatabaseError:
            return None
        finally:
            destination.close()
    finally:
        source.close()
    return copy


def _manifest(listed: dict[str, dict[str, Any]], *, secrets: bool) -> bytes:
    return json.dumps(
        {
            "format": FORMAT,
            "wactorz": __version__,
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "secrets": secrets,
            "files": listed,
        },
        indent=1,
        sort_keys=True,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── Import ────────────────────────────────────────────────────────────────────


def import_state(
    archive: str | os.PathLike[str],
    state_dir: str | os.PathLike[str] | None = None,
    *,
    replace: bool = False,
) -> Imported:
    """Put the state in ``archive`` in the state directory; see the module docstring.

    Everything is checked and written to a directory beside the target first,
    and only then moved into place, so an archive that fails a check leaves the
    target as it was.
    """
    target = Path(state_dir or resolve_state_dir()).resolve()
    if in_use(target):
        raise DirectoryInUse(target)
    occupied = target.is_dir() and any(p.name != LOCK_FILE for p in target.iterdir())
    if occupied and not replace:
        raise DirectoryNotEmpty(target)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    staging = target.with_name(f".{target.name}.import-{stamp}")
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            manifest = _read_manifest(tar)
            files = _extract(tar, manifest["files"], staging)
    except BaseException:
        _remove_tree(staging)
        raise
    result = Imported(target, files=files, secrets=bool(manifest.get("secrets")))
    if target.exists() and not occupied:
        # Nothing in it but, perhaps, a lock no process holds.
        (target / LOCK_FILE).unlink(missing_ok=True)
        target.rmdir()
    if target.exists():
        result.previous = target.with_name(f"{target.name}.before-import-{stamp}")
        target.rename(result.previous)
    staging.rename(target)
    return result


def _read_manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = tar.getmember(MANIFEST)
    except KeyError:
        raise NotAnArchive(Why.NO_MANIFEST) from None
    stream = tar.extractfile(member) if member.isfile() else None
    if stream is None:
        raise NotAnArchive(Why.MANIFEST_NOT_A_FILE)
    try:
        manifest = json.loads(stream.read().decode("utf-8"))
    except ValueError as exc:
        raise NotAnArchive(Why.MANIFEST_UNREADABLE) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise NotAnArchive(Why.OTHER_FORMAT)
    if not isinstance(manifest.get("files"), dict):
        raise NotAnArchive(Why.NO_FILES)
    return manifest


def _safe_relative(name: str) -> PurePosixPath | None:
    """The path under the prefix a member names, or None if it names anything else.

    Refused: an absolute path, one that climbs with `..`, a drive or a
    backslash (a separator on Windows), and anything outside the prefix.
    """
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or ":" in name:
        return None
    parts = path.parts
    if len(parts) < 2 or parts[0] != PREFIX or any(p in ("", ".", "..") for p in parts):
        return None
    return PurePosixPath(*parts[1:])


def _extract(tar: tarfile.TarFile, listed: dict[str, Any], staging: Path) -> int:
    """Write each listed file into ``staging``, checking it on the way. Returns how many."""
    seen: set[str] = set()
    for member in tar:
        if member.name == MANIFEST:
            continue
        relative = _safe_relative(member.name)
        if relative is None:
            raise DamagedArchive(member.name, "a path outside the state directory")
        if member.isdir():
            continue
        if not member.isfile():
            # A link or a device would reach past the directory, or be one.
            raise DamagedArchive(member.name, "not a plain file")
        entry = listed.get(str(relative))
        if not isinstance(entry, dict) or str(relative) in seen:
            raise DamagedArchive(member.name, "not listed in the manifest, or listed twice")
        stream = tar.extractfile(member)
        if stream is None:
            raise DamagedArchive(member.name, "unreadable")
        _write_checked(stream, staging / Path(*relative.parts), member, entry)
        seen.add(str(relative))
    missing = set(listed) - seen
    if missing:
        raise DamagedArchive(min(missing), f"listed but missing ({len(missing)} in all)")
    return len(seen)


def _write_checked(stream: IO[bytes], path: Path, member: tarfile.TarInfo, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    # Owner-only until the bytes are known to be the ones listed.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    if digest.hexdigest() != entry.get("sha256") or size != entry.get("size"):
        raise DamagedArchive(member.name, "its content does not match the manifest")
    # As it was exported, less anything beyond plain permissions.
    path.chmod(member.mode & 0o777)


def _remove_tree(path: Path) -> None:
    for root, dirs, names in os.walk(path, topdown=False):
        for name in names:
            Path(root, name).unlink(missing_ok=True)
        for name in dirs:
            with contextlib.suppress(OSError):
                Path(root, name).rmdir()
    with contextlib.suppress(OSError):
        path.rmdir()


# ── The command ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wactorz-state",
        description="Export a Wactorz state directory to one archive, or import one back.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write the state directory to an archive")
    export.add_argument("archive", nargs="?", help="where to write it (default: a dated name)")
    export.add_argument("--state-dir", metavar="DIR", help="default: WACTORZ_STATE_DIR or ./state")
    export.add_argument(
        "--no-secrets",
        action="store_true",
        help="leave out the install's keys; nodes must then be deployed again",
    )
    load = commands.add_parser("import", help="put an archive's state in the state directory")
    load.add_argument("archive", help="an archive written by `wactorz-state export`")
    load.add_argument("--state-dir", metavar="DIR", help="default: WACTORZ_STATE_DIR or ./state")
    load.add_argument(
        "--replace",
        action="store_true",
        help="when the directory is not empty, keep it beside the import and take its place",
    )
    return parser


def _report_export(done: Exported) -> None:
    logger.info("Exported %s file(s), %.1f MB, to %s", done.files, done.size / 1_048_576, done.path)
    if done.secret_files:
        logger.warning(
            "It holds this install's keys (%s): keep it as you would keep them. "
            "`--no-secrets` writes one without.",
            ", ".join(done.secret_files),
        )


def _report_import(done: Imported) -> None:
    logger.info("Imported %s file(s) into %s", done.files, done.directory)
    if done.previous is not None:
        logger.info("What was there is kept at %s", done.previous)
    if not done.secrets:
        logger.warning("The archive held no keys: deploy each node again once the server is up.")


def _run(args: argparse.Namespace) -> Exception | None:
    """Do what ``args`` asks; the reason it could not, or None."""
    try:
        if args.command == "export":
            _report_export(
                export_state(
                    args.archive or default_archive_name(),
                    args.state_dir,
                    secrets=not args.no_secrets,
                )
            )
        else:
            logger.warning(
                "An archive's state files are unpickled when the system starts: import only "
                "archives you made."
            )
            _report_import(import_state(args.archive, args.state_dir, replace=args.replace))
    except (ArchiveError, tarfile.TarError, OSError) as exc:
        return exc
    return None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = _build_parser().parse_args(argv)
    failure = _run(args)
    if failure is not None:
        logger.error("wactorz-state: %s", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
