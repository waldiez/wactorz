"""Models and other large files that main keeps once, by content, and names by version.

An agent's own blobs (see `wactorz.core.blobs`) belong to that agent: written by
it, moved with it, deleted with it. An artifact belongs to no agent. It is a
model, a set of weights or a tokenizer that someone put here on purpose, that
any number of agents may load, and that changes by being replaced with a new
version rather than by being written to.

So each is kept as a file named by its SHA-256, under ``<state>/..artifacts/``,
and an index gives it a name and a version: ``imu-detector:3`` is the third file
stored as ``imu-detector``. The same bytes stored twice, under any names, are
kept once. A reference to an artifact names the version, or ``latest``, and
resolves to the hash, which is what a machine that fetches it checks it against.

A directory -- a model in several files, or one with its labels beside it -- is
kept as one tar of it, written the same way every time: entries in order, and
no times, owners or permissions of the machine that wrote it. The same
directory gives the same hash wherever it is stored. Links are refused, since
what they point at would not be in the tar.

**Some formats run code when they load**: a pickle, a joblib file, a whole
torch module. They are refused unless the caller allows them, and recorded as
such, so that whatever loads one later can refuse it in turn. Every other
format here is data: loading it runs nothing from the file.

The index is changed under a lock on the store's directory, so a command adding
a model while the server runs does not lose what either of them wrote. The
directory's name starts with ``..``, which no agent's name may, so it never
collides with an agent's own; and it is part of what ``wactorz-state export``
writes.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast

from .atomic_io import write_text
from .model_contents import stored_contents
from .state_lock import StateInUseError, StateLock

#: The directory, inside the state directory, that artifacts are kept in.
STORE_DIRNAME = "..artifacts"

#: The directory, inside the state directory, a directory artifact is unpacked
#: into for an agent to read, one directory per hash. Rebuilt from the archive
#: whenever it is missing, so `wactorz-state export` leaves it out.
UNPACKED_DIRNAME = "..unpacked-artifacts"

#: The layout of the index this module writes, and the only one it reads.
INDEX_FORMAT = 1

#: A SHA-256, as a file in the store is named.
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: A name: what a topic, a file name and a reference can all carry. `:` and `@`
#: are left out because a reference uses them to name a version and a hash.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: How much of a file is read at a time while it is copied in and hashed.
_CHUNK = 1 << 20

#: How long to wait for another process to finish changing the index.
LOCK_WAIT_S = 30.0
_LOCK_POLL_S = 0.05


@dataclass(frozen=True)
class Format:
    """A kind of file an artifact can be, and whether loading it runs code."""

    name: str
    #: The endings that say a file is of this format, for guessing when none is named.
    suffixes: tuple[str, ...]
    runs_code: bool
    description: str


FORMATS: dict[str, Format] = {
    f.name: f
    for f in (
        Format("onnx", (".onnx",), False, "an ONNX model"),
        Format("safetensors", (".safetensors",), False, "tensors in safetensors"),
        Format("npz", (".npz",), False, "numpy arrays, loaded with allow_pickle=False"),
        Format("npy", (".npy",), False, "a numpy array, loaded with allow_pickle=False"),
        Format("state_dict", (), False, "a torch state_dict, loaded with weights_only=True"),
        Format("gguf", (".gguf",), False, "a GGUF model, as llama.cpp loads"),
        Format("tflite", (".tflite",), False, "a TensorFlow Lite model"),
        Format("skops", (".skops",), False, "a scikit-learn model saved with skops"),
        Format("raw", (), False, "bytes, read as they are"),
        Format("dir", (), False, "a directory, kept as a tar of it"),
        Format("pickle", (".pkl", ".pickle"), True, "a pickle"),
        Format("joblib", (".joblib",), True, "a joblib file"),
        Format("torch", (), True, "a whole torch module"),
    )
}

#: The format a directory is kept in.
DIRECTORY_FORMAT = "dir"


class ArtifactError(Exception):
    """An artifact could not be stored, found or read, and why."""


class CodeRefusedError(ArtifactError):
    """A format that runs code when it loads, offered where code is not allowed."""


class ArtifactNotFoundError(ArtifactError):
    def __init__(self, reference: str) -> None:
        super().__init__(f"no artifact {reference!r}")


@dataclass(frozen=True)
class Artifact:
    """One version of one named artifact."""

    name: str
    version: int
    sha256: str
    format: str
    size: int
    runs_code: bool
    added_at: float
    #: Where it came from and what it is: the source, the dataset, the code
    #: revision, its metrics. Whatever the one who added it said.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        """``name:version``."""
        return f"{self.name}:{self.version}"

    @property
    def pinned(self) -> str:
        """``name:version@sha256``: the reference a machine checks what it fetched against."""
        return f"{self.reference}@{self.sha256}"


def name_error(name: str) -> str | None:
    """Why ``name`` cannot name an artifact, or None if it can."""
    if not _NAME.match(name):
        return (
            f"{name!r} is not a usable name: letters, digits, '.', '_' and '-', "
            "starting with a letter or digit, at most 128 characters"
        )
    return None


def parse_reference(reference: str) -> tuple[str, int | None]:
    """The name and version in ``name``, ``name:latest`` or ``name:3``; None for the latest."""
    name, _, version = reference.partition(":")
    error = name_error(name)
    if error:
        raise ArtifactError(error)
    if version in ("", "latest"):
        return name, None
    if not version.isdigit() or int(version) < 1:
        raise ArtifactError(f"{reference!r}: a version is 'latest' or a number from 1")
    return name, int(version)


def guess_format(path: Path) -> str | None:
    """The format ``path`` is in, from its ending; None when the ending does not say."""
    if path.is_dir():
        return DIRECTORY_FORMAT
    suffix = path.suffix.lower()
    for fmt in FORMATS.values():
        if suffix in fmt.suffixes:
            return fmt.name
    return None


class ArtifactStore:
    """The artifacts kept under one directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    @classmethod
    def in_state_dir(cls, state_dir: str | os.PathLike[str]) -> "ArtifactStore":
        """The store inside ``state_dir``."""
        return cls(Path(state_dir) / STORE_DIRNAME)

    @property
    def objects(self) -> Path:
        """Where the files are, each named by its SHA-256."""
        return self.root / "objects"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def path_of(self, artifact: Artifact) -> Path:
        """The file holding ``artifact``."""
        return self.objects / artifact.sha256

    @property
    def unpacked(self) -> Path:
        """Where directory artifacts are unpacked, beside the store."""
        return self.root.parent / UNPACKED_DIRNAME

    def readable_path(self, entry: Mapping[str, Any]) -> Path:
        """Where an agent reads the pinned artifact ``entry``: its file, or the directory it unpacks to.

        Unpacks a directory the first time it is asked for. Raises
        `ArtifactError` when the file is not held.
        """
        path = self.object_path(str(entry.get("sha256", "")))
        if path is None:
            raise ArtifactError(f"{entry.get('name')}:{entry.get('version')} is not held here")
        return readable_path(path, entry, self.unpacked)

    def object_path(self, sha256: str) -> Path | None:
        """The file holding the bytes that hash to ``sha256``, or None when none does."""
        if not _SHA256.match(sha256):
            return None
        path = self.objects / sha256
        return path if path.is_file() else None

    def pin(self, wanted: Any) -> dict[str, Any]:
        """What a spawn config names, as the exact version it is to run.

        ``wanted`` is a reference -- ``name``, ``name:latest``, ``name:3`` --
        or an entry pinned before, which must still name the same bytes: an
        agent restarted from its record runs what it ran, or does not start.
        Raises `ArtifactError` saying why it cannot be pinned.
        """
        if isinstance(wanted, str):
            return pinned(self.resolve(wanted))
        if isinstance(wanted, Mapping) and "sha256" in wanted:
            entry = check_pinned(wanted)
            held = self.resolve(f"{entry['name']}:{entry['version']}")
            if held.sha256 != entry["sha256"]:
                raise ArtifactError(f"{held.reference} no longer holds the bytes it was pinned to")
            return pinned(held)
        raise ArtifactError(f"{wanted!r} is not a reference to an artifact")

    def pin_all(self, wanted: Any) -> dict[str, dict[str, Any]]:
        """Each artifact a spawn config's ``artifacts`` names, pinned, by the name the agent uses."""
        if not isinstance(wanted, Mapping):
            raise ArtifactError(
                "artifacts are named as a mapping, like {'model': 'imu-detector:3'}"
            )
        result: dict[str, dict[str, Any]] = {}
        for key, value in wanted.items():
            error = name_error(str(key))
            if error:
                raise ArtifactError(f"the key {error}")
            result[str(key)] = self.pin(value)
        return result

    # ── Reading ───────────────────────────────────────────────────────────

    def names(self) -> list[str]:
        """Every name with at least one version, in order."""
        return sorted(self._read_index())

    def versions(self, name: str) -> list[Artifact]:
        """Every version of ``name``, oldest first; empty when there is none."""
        return [_artifact(name, entry) for entry in self._read_index().get(name, [])]

    def resolve(self, reference: str) -> Artifact:
        """The artifact ``name``, ``name:latest`` or ``name:3`` names.

        Raises `ArtifactNotFoundError` when there is none, `ArtifactError` when the
        reference is not one.
        """
        name, version = parse_reference(reference)
        versions = self.versions(name)
        if not versions:
            raise ArtifactNotFoundError(reference)
        if version is None:
            return versions[-1]
        for artifact in versions:
            if artifact.version == version:
                return artifact
        raise ArtifactNotFoundError(reference)

    def verify(self, artifact: Artifact) -> str | None:
        """Why the file holding ``artifact`` is not what was stored, or None when it is."""
        path = self.path_of(artifact)
        try:
            digest = _hash_file(path)
        except OSError as exc:
            return f"{artifact.reference}: {exc.strerror or exc}"
        if digest != artifact.sha256:
            return f"{artifact.reference}: its file no longer matches its SHA-256"
        return None

    # ── Changing ──────────────────────────────────────────────────────────

    def add(
        self,
        source: str | os.PathLike[str],
        name: str,
        *,
        format_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        allow_code: bool = False,
    ) -> Artifact:
        """Store the file or directory at ``source`` as the next version of ``name``.

        ``format_name`` is guessed from the ending when not given; a file whose
        ending does not say must name it. A format that runs code is refused
        unless ``allow_code``. Raises `ArtifactError` saying why it was refused.
        """
        path = Path(source)
        error = name_error(name)
        if error:
            raise ArtifactError(error)
        if not path.exists():
            raise ArtifactError(f"{path} does not exist")
        fmt = _format_for(path, format_name)
        if fmt.runs_code and not allow_code:
            raise CodeRefusedError(
                f"{path.name} is {fmt.description}, which runs code when it loads: refused"
            )
        try:
            json.dumps(dict(metadata or {}))
        except (TypeError, ValueError) as exc:
            raise ArtifactError(f"its metadata is not JSON: {exc}") from exc

        self.objects.mkdir(parents=True, exist_ok=True)
        # Copied in and hashed before the lock is taken: a model takes as long to
        # read as it does, and nobody else needs to wait for that.
        sha256, size = self._take_in(path, fmt)
        with self._locked():
            index = self._read_index()
            versions = index.setdefault(name, [])
            entry = {
                "version": max((v["version"] for v in versions), default=0) + 1,
                "sha256": sha256,
                "format": fmt.name,
                "size": size,
                "runs_code": fmt.runs_code,
                "added_at": time.time(),
                "metadata": dict(metadata or {}),
            }
            versions.append(entry)
            self._write_index(index)
        return _artifact(name, entry)

    def remove(self, reference: str, *, every_version: bool = False) -> list[Artifact]:
        """Remove the version ``reference`` names, or with ``every_version`` all of a name's.

        A bare name is refused without ``every_version``: it reads like one
        thing and would remove several. A file is deleted once no version of
        any name still holds it. Returns what was removed; raises
        `ArtifactNotFoundError` when that is nothing.
        """
        name, version = parse_reference(reference)
        everything = version is None and ":" not in reference
        if everything != every_version:
            raise ArtifactError(
                f"{reference!r} names one version; every version is removed by the bare name"
                if every_version
                else f"{reference!r} would remove every version: name one, or ask for every version"
            )
        with self._locked():
            index = self._read_index()
            versions = index.get(name, [])
            if everything:
                gone, kept = versions, []
            elif version is None:
                gone, kept = versions[-1:], versions[:-1]
            else:
                gone = [v for v in versions if v["version"] == version]
                kept = [v for v in versions if v["version"] != version]
            if not gone:
                raise ArtifactNotFoundError(reference)
            if kept:
                index[name] = kept
            else:
                index.pop(name, None)
            self._write_index(index)
            still_held = {v["sha256"] for entries in index.values() for v in entries}
            for entry in gone:
                if entry["sha256"] not in still_held:
                    with contextlib.suppress(FileNotFoundError):
                        (self.objects / entry["sha256"]).unlink()
                    shutil.rmtree(self.unpacked / entry["sha256"], ignore_errors=True)
        return [_artifact(name, entry) for entry in gone]

    # ── Inside ────────────────────────────────────────────────────────────

    def _take_in(self, path: Path, fmt: Format) -> tuple[str, int]:
        """Copy ``path`` into the store under its hash; its hash and size.

        Written under a temporary name and renamed, so a copy that fails part
        way leaves nothing that looks stored. A file already held is not
        written again.
        """
        part = self.objects / f".{uuid.uuid4().hex}.part"
        try:
            with part.open("wb") as out:
                hashing = _Hashing(out)
                if fmt.name == DIRECTORY_FORMAT:
                    # A writable binary stream, which is all tarfile asks of it.
                    _write_tar(path, cast("IO[bytes]", hashing))
                else:
                    with path.open("rb") as source:
                        while chunk := source.read(_CHUNK):
                            hashing.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha256 = hashing.hexdigest()
            final = self.objects / sha256
            if final.exists():
                part.unlink()
            else:
                part.replace(final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return sha256, hashing.size

    def _read_index(self) -> dict[str, list[dict[str, Any]]]:
        if not self.index_path.exists():
            return {}
        try:
            stored = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"the index at {self.index_path} cannot be read: {exc}") from exc
        if not isinstance(stored, dict) or stored.get("format") != INDEX_FORMAT:
            raise ArtifactError(f"the index at {self.index_path} is not one this release reads")
        return stored.get("artifacts", {})

    def _write_index(self, index: Mapping[str, list[dict[str, Any]]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = {"format": INDEX_FORMAT, "artifacts": {name: index[name] for name in sorted(index)}}
        write_text(self.index_path, json.dumps(body, indent=2))

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the store's lock, waiting for another process to let go of it."""
        lock = StateLock(self.root)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                lock.acquire()
                break
            except StateInUseError as exc:
                if time.monotonic() > deadline:
                    raise ArtifactError(f"the store at {self.root} stayed busy: {exc}") from exc
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            lock.release()


def _artifact(name: str, entry: Mapping[str, Any]) -> Artifact:
    return Artifact(
        name=name,
        version=int(entry["version"]),
        sha256=str(entry["sha256"]),
        format=str(entry["format"]),
        size=int(entry["size"]),
        runs_code=bool(entry["runs_code"]),
        added_at=float(entry["added_at"]),
        metadata=dict(entry.get("metadata") or {}),
    )


def pinned(artifact: Artifact) -> dict[str, Any]:
    """``artifact`` as a spawn config carries it: enough to fetch it and check it."""
    return {
        "name": artifact.name,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "format": artifact.format,
        "size": artifact.size,
        "runs_code": artifact.runs_code,
    }


def check_pinned(entry: Any) -> dict[str, Any]:
    """``entry`` if it is a well-formed pinned artifact; `ArtifactError` saying why it is not.

    Asked by a node of what main sent, before it fetches anything.
    """
    if not isinstance(entry, Mapping):
        raise ArtifactError(f"{entry!r} is not a pinned artifact")
    sha256, size, version = entry.get("sha256"), entry.get("size"), entry.get("version")
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        raise ArtifactError(f"{entry!r} does not name a SHA-256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ArtifactError(f"{entry!r} does not say its size")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArtifactError(f"{entry!r} does not say its version")
    error = name_error(str(entry.get("name", "")))
    if error:
        raise ArtifactError(error)
    if entry.get("format") not in FORMATS:
        raise ArtifactError(f"{entry!r} is not in a format this release knows")
    return dict(entry)


def pinned_in(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The pinned artifacts a spawn config names, checked, by the name the agent uses."""
    wanted = config.get("artifacts") or {}
    if not isinstance(wanted, Mapping):
        raise ArtifactError("its artifacts are not a mapping")
    return {str(key): check_pinned(entry) for key, entry in wanted.items()}


def readable_path(file: Path, entry: Mapping[str, Any], unpacked: Path) -> Path:
    """Where an agent reads ``entry``, held in ``file``: the file, or for a directory, its unpacked copy."""
    if entry.get("format") != DIRECTORY_FORMAT:
        return file
    target = unpacked / str(entry["sha256"])
    unpack_directory(file, target)
    return target


def unpack_directory(archive: Path, target: Path) -> None:
    """Unpack the directory archive ``archive`` into ``target``, unless it is there already.

    Only files and directories are taken, each at a path inside ``target``: a
    link, a device, an absolute path or one that climbs out is refused, whoever
    wrote the archive. Unpacked beside ``target`` and renamed into place, so a
    directory that is there is a whole one.
    """
    if target.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    work = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        work.mkdir()
        with tarfile.open(archive) as tar:
            for member in tar:
                _unpack_member(tar, member, work)
        try:
            work.replace(target)
        except OSError:
            # Unpacked at the same moment by another caller, who got there first.
            if not target.is_dir():
                raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _unpack_member(tar: tarfile.TarFile, member: tarfile.TarInfo, into: Path) -> None:
    relative = PurePosixPath(member.name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ArtifactError(f"the archive names {member.name!r}, outside where it unpacks")
    if not relative.parts:
        return
    destination = into.joinpath(*relative.parts)
    if member.isdir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    if not member.isfile():
        raise ArtifactError(f"the archive holds {member.name!r}, which is not a file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = tar.extractfile(member)
    if source is None:
        raise ArtifactError(f"the archive cannot give {member.name!r}")
    with source, destination.open("wb") as out:
        shutil.copyfileobj(source, out, _CHUNK)


def summary(store: ArtifactStore, *, limit: int = 50) -> list[str]:
    """One line per name in ``store``: its newest version, format, size, arrays and what was said of it.

    For a person reading a list and for a model choosing one, so it says what
    tells models apart and nothing that does not: no hashes, no times. The arrays
    are the names code reads the model by, where the format has named arrays;
    see `wactorz.core.model_contents`.
    """
    lines: list[str] = []
    for name in store.names()[:limit]:
        versions = store.versions(name)
        if not versions:
            continue
        newest = versions[-1]
        line = f"{name} — newest {newest.reference} ({newest.format}, {human_size(newest.size)})"
        if len(versions) > 1:
            line += f", {len(versions)} versions"
        if newest.runs_code:
            line += ", runs code when it loads"
        if held := _held_by(store, newest):
            line += f"; arrays {held}"
        said = [
            f"{key}={value}"
            for key, value in newest.metadata.items()
            if key != "source" and isinstance(value, (str, int, float))
        ]
        if said:
            line += "; " + ", ".join(said)[:160]
        lines.append(line)
    return lines


def _held_by(store: ArtifactStore, artifact: Artifact) -> str | None:
    path = store.object_path(artifact.sha256)
    return None if path is None else stored_contents(artifact.sha256, path, artifact.format)


def human_size(size: int) -> str:
    """``size`` bytes in the unit that reads best."""
    if size < 1024:
        return f"{size} B"
    # Whole powers of 1024, counted exactly: 1 MiB is 1.0 MB, not 1024.0 KB.
    power = min((size.bit_length() - 1) // 10, len(_UNITS))
    return f"{size / 1024**power:.1f} {_UNITS[power - 1]}"


_UNITS = ("KB", "MB", "GB")


def describe(artifact: Artifact) -> dict[str, Any]:
    """``artifact`` as plain JSON, with its references."""
    return {**asdict(artifact), "reference": artifact.reference, "pinned": artifact.pinned}


def _format_for(path: Path, named: str | None) -> Format:
    if path.is_dir():
        if named not in (None, DIRECTORY_FORMAT):
            raise ArtifactError(f"{path} is a directory, which is kept as {DIRECTORY_FORMAT!r}")
        return FORMATS[DIRECTORY_FORMAT]
    if named is None:
        guessed = guess_format(path)
        if guessed is None:
            raise ArtifactError(
                f"the ending of {path.name} does not say its format; name one of "
                f"{', '.join(sorted(FORMATS))}"
            )
        named = guessed
    if named == DIRECTORY_FORMAT:
        raise ArtifactError(f"{path} is a file, not a directory")
    fmt = FORMATS.get(named)
    if fmt is None:
        raise ArtifactError(f"{named!r} is not a format: one of {', '.join(sorted(FORMATS))}")
    return fmt


class _Hashing(io.RawIOBase):
    """A writable file that hashes and counts what passes through it on the way to another."""

    def __init__(self, out: IO[bytes]) -> None:
        super().__init__()
        self._out = out
        self._hash = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self._hash.update(chunk)
        self.size += len(chunk)
        self._out.write(chunk)
        return len(chunk)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _write_tar(directory: Path, out: IO[bytes]) -> None:
    """``directory`` as a tar written the same way wherever it is written."""
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for path in _entries(directory):
            relative = path.relative_to(directory).as_posix()
            info = tarfile.TarInfo(relative)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
                continue
            info.mode = 0o644
            info.size = path.stat().st_size
            with path.open("rb") as source:
                tar.addfile(info, source)


def _entries(directory: Path) -> list[Path]:
    """Everything under ``directory``, in an order that does not depend on the disk."""
    found: list[Path] = []
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        if path.is_symlink():
            raise ArtifactError(f"{path} is a link; only what it points at could be kept")
        if not (path.is_dir() or path.is_file()):
            raise ArtifactError(f"{path} is neither a file nor a directory")
        found.append(path)
    return found


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
