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

Two pointers may sit on a name, beside its versions: ``active``, the version
agents serve, and ``shadow``, a version under trial that sees the same data and
is acted on by nobody. A bare name resolves to ``active`` once it is set, and to
the newest version until then; ``name:latest`` is always the newest, and
``name:active`` and ``name:shadow`` name the pointers themselves. Storing a
version never moves a pointer: a move is its own act, with who made it and why.
A version a pointer names cannot be removed until the pointer is moved off it.
``docs/design/model-pointers.md`` records the decision.

The index is changed under a lock on the store's directory, so a command adding
a model while the server runs does not lose what either of them wrote. Under the
same lock, each pointer change is appended to a journal beside the index, with
who made it and why: whoever made it, in whichever process, main reads the
journal into the history and trims what it read (`pointer_changes`). The
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

#: The layout of the index this module writes. Format 1 is the same without
#: pointers: read as having none, and written back in this format. Any other is
#: refused outright, since a release that did not know the pointers and
#: rewrote the index would drop them, and a model silently served wrong is the
#: worse failure.
INDEX_FORMAT = 2
_READ_FORMATS = (1, INDEX_FORMAT)

#: The pointers a name may carry, and the only ones: a pointer is a role a
#: version plays, and these are the roles. Anything else said of a version is
#: metadata.
POINTERS = ("active", "shadow")

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
    """An artifact could not be stored, found or read, and why.

    Each reason is a classmethod, so the words for one are written once and
    a caller can find every place a reason is given.
    """

    # ── References and names ──────────────────────────────────────────────

    @classmethod
    def bad_version(cls, reference: str) -> "ArtifactError":
        return cls(f"{reference!r}: a version is 'latest', 'active', 'shadow' or a number from 1")

    @classmethod
    def not_a_reference(cls, wanted: Any) -> "ArtifactError":
        return cls(f"{wanted!r} is not a reference to an artifact")

    @classmethod
    def names_the_pointer(cls, reference: str, which: str, name: str) -> "ArtifactError":
        return cls(
            f"{reference!r} would mean the {which} pointer itself: name the version "
            f"to move it to, as {name}:3 or {name}:latest"
        )

    @classmethod
    def not_a_pointer(cls, which: str) -> "ArtifactError":
        return cls(f"{which!r} is not a pointer: one of {', '.join(POINTERS)}")

    @classmethod
    def names_one_version(cls, reference: str) -> "ArtifactError":
        return cls(f"{reference!r} names one version; every version is removed by the bare name")

    @classmethod
    def names_every_version(cls, reference: str) -> "ArtifactError":
        return cls(f"{reference!r} would remove every version: name one, or ask for every version")

    # ── What a spawn config names ─────────────────────────────────────────

    @classmethod
    def artifacts_not_a_mapping(cls) -> "ArtifactError":
        return cls("artifacts are named as a mapping, like {'model': 'imu-detector:3'}")

    @classmethod
    def pinned_not_a_mapping(cls) -> "ArtifactError":
        return cls("its artifacts are not a mapping")

    @classmethod
    def bad_key(cls, error: str) -> "ArtifactError":
        return cls(f"the key {error}")

    @classmethod
    def not_pinned(cls, entry: Any) -> "ArtifactError":
        return cls(f"{entry!r} is not a pinned artifact")

    @classmethod
    def no_sha256(cls, entry: Any) -> "ArtifactError":
        return cls(f"{entry!r} does not name a SHA-256")

    @classmethod
    def no_size(cls, entry: Any) -> "ArtifactError":
        return cls(f"{entry!r} does not say its size")

    @classmethod
    def no_version(cls, entry: Any) -> "ArtifactError":
        return cls(f"{entry!r} does not say its version")

    @classmethod
    def unknown_entry_format(cls, entry: Any) -> "ArtifactError":
        return cls(f"{entry!r} is not in a format this release knows")

    @classmethod
    def follows_no_pointer(cls, entry: Any, follows: Any) -> "ArtifactError":
        return cls(f"{entry!r} follows {follows!r}, which is not a pointer")

    @classmethod
    def not_held(cls, entry: Mapping[str, Any]) -> "ArtifactError":
        return cls(f"{entry.get('name')}:{entry.get('version')} is not held here")

    @classmethod
    def bytes_changed(cls, reference: str) -> "ArtifactError":
        return cls(f"{reference} no longer holds the bytes it was pinned to")

    # ── What is offered to the store ──────────────────────────────────────

    @classmethod
    def missing(cls, path: Path) -> "ArtifactError":
        return cls(f"{path} does not exist")

    @classmethod
    def metadata_not_json(cls, exc: Exception) -> "ArtifactError":
        return cls(f"its metadata is not JSON: {exc}")

    @classmethod
    def directory_with_format(cls, path: Path) -> "ArtifactError":
        return cls(f"{path} is a directory, which is kept as {DIRECTORY_FORMAT!r}")

    @classmethod
    def format_unsaid(cls, path: Path) -> "ArtifactError":
        return cls(
            f"the ending of {path.name} does not say its format; name one of "
            f"{', '.join(sorted(FORMATS))}"
        )

    @classmethod
    def file_not_directory(cls, path: Path) -> "ArtifactError":
        return cls(f"{path} is a file, not a directory")

    @classmethod
    def unknown_format(cls, named: str) -> "ArtifactError":
        return cls(f"{named!r} is not a format: one of {', '.join(sorted(FORMATS))}")

    @classmethod
    def link(cls, path: Path) -> "ArtifactError":
        return cls(f"{path} is a link; only what it points at could be kept")

    @classmethod
    def not_file_or_directory(cls, path: Path) -> "ArtifactError":
        return cls(f"{path} is neither a file nor a directory")

    # ── The archive a directory is kept as ────────────────────────────────

    @classmethod
    def archive_outside(cls, member: str) -> "ArtifactError":
        return cls(f"the archive names {member!r}, outside where it unpacks")

    @classmethod
    def archive_not_a_file(cls, member: str) -> "ArtifactError":
        return cls(f"the archive holds {member!r}, which is not a file")

    @classmethod
    def archive_unreadable(cls, member: str) -> "ArtifactError":
        return cls(f"the archive cannot give {member!r}")

    # ── The store itself ──────────────────────────────────────────────────

    @classmethod
    def unreadable_index(cls, path: Path, exc: Exception) -> "ArtifactError":
        return cls(f"the index at {path} cannot be read: {exc}")

    @classmethod
    def unknown_index(cls, path: Path) -> "ArtifactError":
        return cls(f"the index at {path} is not one this release reads")

    @classmethod
    def busy(cls, root: Path, exc: Exception) -> "ArtifactError":
        return cls(f"the store at {root} stayed busy: {exc}")

    @classmethod
    def pointer_bytes_changed(cls, name: str, which: str, reference: str) -> "ArtifactError":
        return cls(
            f"{name}'s {which} pointer names {reference}, which no "
            "longer holds the bytes it was set to; move the pointer"
        )

    @classmethod
    def pointer_dangles(cls, name: str, which: str, version: int) -> "ArtifactError":
        return cls(
            f"{name}'s {which} pointer names {name}:{version}, which is not stored; "
            "move the pointer"
        )


class CodeRefusedError(ArtifactError):
    """A format that runs code when it loads, offered where code is not allowed."""

    @classmethod
    def runs_code(cls, path: Path, description: str) -> "CodeRefusedError":
        return cls(f"{path.name} is {description}, which runs code when it loads: refused")


class ArtifactNotFoundError(ArtifactError):
    def __init__(self, reference: str) -> None:
        super().__init__(f"no artifact {reference!r}")


class ArtifactConflictError(ArtifactError):
    """A change the pointers as they stand do not allow: asked again later, it may succeed.

    Told apart from a reference that is wrong, or an artifact that is not
    there, so that a caller can say "not now" rather than "no such thing".
    """

    @classmethod
    def already(cls, reference: str, which: str) -> "ArtifactConflictError":
        return cls(f"{reference} is already {which}")

    @classmethod
    def shadow_is_active(cls, reference: str) -> "ArtifactConflictError":
        return cls(f"{reference} is active; a shadow is a different version")

    @classmethod
    def unset(cls, name: str, which: str) -> "ArtifactConflictError":
        return cls(f"{name} has no {which} version")

    @classmethod
    def no_shadow_to_promote(cls, name: str) -> "ArtifactConflictError":
        return cls(f"{name} has no shadow version to promote")

    @classmethod
    def nothing_to_roll_back(cls, name: str) -> "ArtifactConflictError":
        return cls(f"{name}'s active version has no earlier one to go back to")

    @classmethod
    def in_use(cls, name: str, version: int, which: str) -> "ArtifactConflictError":
        return cls(f"{name}:{version} is the {which} version; move the pointer first")


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


@dataclass(frozen=True)
class Pointer:
    """Where one of a name's pointers stands: the version it names, and the move that put it there."""

    name: str
    #: ``active`` or ``shadow``.
    pointer: str
    version: int
    #: The bytes the version held when the pointer was set. A version's bytes
    #: never change, but its number can be reused after a remove, so the
    #: pointer carries the hash as a pinned spawn does, and one whose version
    #: no longer holds these bytes is reported rather than followed.
    sha256: str
    #: The version it named before this move, or None: enough for one step back.
    previous: int | None
    moved_at: float
    #: Who moved it -- ``cli``, ``chat``, ``agent:<name>``, an extension's name.
    #: Recorded, never checked.
    by: str
    #: Why, in the mover's words, or None.
    reason: str | None = None

    @property
    def reference(self) -> str:
        """``name:version``."""
        return f"{self.name}:{self.version}"

    def payload(self) -> dict[str, Any]:
        """The pointer as the retained ``models/<name>/<pointer>`` topic carries it."""
        return asdict(self)


#: What a journalled change did: a pointer set to a version, a pointer unset,
#: or the shadow unset because its version became active.
MOVED = "moved"
CLEARED = "cleared"
PROMOTED = "promoted"

#: What follows the name in a reference: None for a bare name, ``"latest"``, one
#: of `POINTERS`, or a version number.
Selector = int | str | None


def name_error(name: str) -> str | None:
    """Why ``name`` cannot name an artifact, or None if it can."""
    if not _NAME.match(name):
        return (
            f"{name!r} is not a usable name: letters, digits, '.', '_' and '-', "
            "starting with a letter or digit, at most 128 characters"
        )
    return None


def parse_reference(reference: str) -> tuple[str, Selector]:
    """The name in ``reference`` and what follows it; see `Selector`.

    A bare ``name`` and ``name:latest`` are told apart, since they resolve
    differently once a pointer is set.
    """
    name, _, version = reference.partition(":")
    error = name_error(name)
    if error:
        raise ArtifactError(error)
    if version == "":
        return name, None
    if version == "latest" or version in POINTERS:
        return name, version
    if not version.isdigit() or int(version) < 1:
        raise ArtifactError.bad_version(reference)
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

    @property
    def journal_path(self) -> Path:
        """Each pointer change not yet read into the history, one JSON object a line."""
        return self.root / "pointer-moves.jsonl"

    @property
    def rejected_journal_path(self) -> Path:
        """Lines of the journal that were not a change, kept for a person to read."""
        return self.root / "pointer-moves.rejected.jsonl"

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
            raise ArtifactError.not_held(entry)
        return readable_path(path, entry, self.unpacked)

    def object_path(self, sha256: str) -> Path | None:
        """The file holding the bytes that hash to ``sha256``, or None when none does."""
        if not _SHA256.match(sha256):
            return None
        path = self.objects / sha256
        return path if path.is_file() else None

    def pin(self, wanted: Any) -> dict[str, Any]:
        """What a spawn config names, as the exact version it is to run.

        ``wanted`` is a reference -- ``name``, ``name:latest``, ``name:3``,
        ``name:active``, ``name:shadow`` -- or an entry pinned before. A
        reference that followed a pointer (a bare name, or the pointer named)
        is pinned with ``follows`` saying which, and pinned again later goes
        to where that pointer is now, while it is set: following was what the
        spawn asked for, and an agent restarted on the version everyone else
        moved off would be the one serving the old model. With no pointer to
        follow, and for an entry that named a version, the entry must still
        name the same bytes: such an agent runs what it ran, or does not
        start. Raises `ArtifactError` saying why it cannot be pinned.
        """
        if isinstance(wanted, str):
            _, selector = parse_reference(wanted)
            entry = pinned(self.resolve(wanted))
            if selector is None or selector == "active":
                entry["follows"] = "active"
            elif selector == "shadow":
                entry["follows"] = "shadow"
            return entry
        if isinstance(wanted, Mapping) and "sha256" in wanted:
            entry = check_pinned(wanted)
            follows = entry.get("follows")
            name = str(entry["name"])
            if follows in POINTERS and self.pointer(name, follows) is not None:
                return self.pin(f"{name}:{follows}")
            # No pointer to follow: the version it ran, as a store without
            # pointers has always had it.
            held = self.resolve(f"{name}:{entry['version']}")
            if held.sha256 != entry["sha256"]:
                raise ArtifactError.bytes_changed(held.reference)
            repinned = pinned(held)
            if follows in POINTERS:
                repinned["follows"] = follows
            return repinned
        raise ArtifactError.not_a_reference(wanted)

    def pin_all(self, wanted: Any) -> dict[str, dict[str, Any]]:
        """Each artifact a spawn config's ``artifacts`` names, pinned, by the name the agent uses."""
        if not isinstance(wanted, Mapping):
            raise ArtifactError.artifacts_not_a_mapping()
        result: dict[str, dict[str, Any]] = {}
        for key, value in wanted.items():
            error = name_error(str(key))
            if error:
                raise ArtifactError.bad_key(error)
            result[str(key)] = self.pin(value)
        return result

    # ── Reading ───────────────────────────────────────────────────────────

    def names(self) -> list[str]:
        """Every name with at least one version, in order."""
        return sorted(self._read_all()[0])

    def versions(self, name: str) -> list[Artifact]:
        """Every version of ``name``, oldest first; empty when there is none."""
        return [_artifact(name, entry) for entry in self._read_all()[0].get(name, [])]

    def resolve(self, reference: str) -> Artifact:
        """The artifact a reference names: the one rule, for every caller.

        A bare ``name`` is ``active`` when that is set, else the newest;
        ``name:latest`` is the newest whatever the pointers say; ``name:active``
        and ``name:shadow`` are the pointers, an error when unset; ``name:3``
        is that version. Raises `ArtifactNotFoundError` when there is none,
        `ArtifactError` when the reference is not one or a pointer dangles.
        """
        name, selector = parse_reference(reference)
        artifacts, pointers = self._read_all()
        return _resolve_in(artifacts, pointers, name, selector, reference)

    def pointer(self, name: str, which: str) -> Pointer | None:
        """Where ``name``'s ``active`` or ``shadow`` pointer stands, or None when unset."""
        return self.pointers(name).get(_pointer_name(which))

    def pointers(self, name: str) -> dict[str, Pointer]:
        """``name``'s pointers that are set, by pointer name."""
        error = name_error(name)
        if error:
            raise ArtifactError(error)
        return _pointers_of(name, self._read_all()[1])

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
            raise ArtifactError.missing(path)
        fmt = _format_for(path, format_name)
        if fmt.runs_code and not allow_code:
            raise CodeRefusedError.runs_code(path, fmt.description)
        try:
            json.dumps(dict(metadata or {}))
        except (TypeError, ValueError) as exc:
            raise ArtifactError.metadata_not_json(exc) from exc

        self.objects.mkdir(parents=True, exist_ok=True)
        # Copied in and hashed before the lock is taken: a model takes as long to
        # read as it does, and nobody else needs to wait for that.
        sha256, size = self._take_in(path, fmt)
        with self._locked():
            index, pointers = self._read_all()
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
            self._write_all(index, pointers)
        return _artifact(name, entry)

    def remove(self, reference: str, *, every_version: bool = False) -> list[Artifact]:
        """Remove the version ``reference`` names, or with ``every_version`` all of a name's.

        A bare name is refused without ``every_version``: it reads like one
        thing and would remove several. A version a pointer names is refused
        until the pointer is moved off it; every version of a name takes the
        name's pointers with it. A file is deleted once no version of any name
        still holds it. Returns what was removed; raises `ArtifactNotFoundError`
        when that is nothing.
        """
        name, selector = parse_reference(reference)
        everything = selector is None
        if everything != every_version:
            raise (
                ArtifactError.names_one_version(reference)
                if every_version
                else ArtifactError.names_every_version(reference)
            )
        with self._locked():
            index, pointers = self._read_all()
            versions = index.get(name, [])
            dropped: dict[str, Pointer] = {}
            if everything:
                gone, kept = versions, []
                dropped = _pointers_of(name, pointers)
                pointers.pop(name, None)
            else:
                if not versions:
                    raise ArtifactNotFoundError(reference)
                version = _resolve_in(index, pointers, name, selector, reference).version
                gone = [v for v in versions if v["version"] == version]
                kept = [v for v in versions if v["version"] != version]
                for which, entry in pointers.get(name, {}).items():
                    if entry["version"] == version:
                        raise ArtifactConflictError.in_use(name, version, which)
            if not gone:
                raise ArtifactNotFoundError(reference)
            if kept:
                index[name] = kept
            else:
                index.pop(name, None)
            self._write_all(index, pointers)
            now = time.time()
            self._journal([_change(CLEARED, was, at=now) for was in dropped.values()])
            still_held = {v["sha256"] for entries in index.values() for v in entries}
            for entry in gone:
                if entry["sha256"] not in still_held:
                    with contextlib.suppress(FileNotFoundError):
                        (self.objects / entry["sha256"]).unlink()
                    shutil.rmtree(self.unpacked / entry["sha256"], ignore_errors=True)
        return [_artifact(name, entry) for entry in gone]

    def move(self, which: str, reference: str, *, by: str, reason: str | None = None) -> Pointer:
        """Point ``name``'s ``which`` at the version ``reference`` names, and say who and why.

        The reference names a version, ``latest`` included; a bare name is
        refused, since it would mean the pointer being moved. Moving
        ``active`` onto the version ``shadow`` names is a promotion and clears
        ``shadow``; pointing ``shadow`` at the active version is refused,
        since a trial of a model against itself measures nothing. A move to
        where the pointer already is is refused too, so every move is a
        change. Raises `ArtifactError` saying why.
        """
        which = _pointer_name(which)
        name, selector = parse_reference(reference)
        if selector is None or selector == which:
            raise ArtifactError.names_the_pointer(reference, which, name)
        with self._locked():
            artifacts, pointers = self._read_all()
            changes: list[dict[str, Any]] = []
            moved = _move_in(
                artifacts, pointers, which, name, selector, reference, by, reason, changes
            )
            self._write_all(artifacts, pointers)
            self._journal(changes)
        return moved

    def rollback(self, name: str, *, by: str, reason: str | None = None) -> Pointer:
        """Move ``name``'s ``active`` back to the version it named before its last move.

        One step back is all the store keeps: a pointer records the version it
        left, not the one before that. Done as one change under the lock, so a
        move made meanwhile is either stepped back from or seen first. The
        move follows `move`'s rules, so stepping back onto the shadow version
        promotes it. Raises `ArtifactConflictError` when ``active`` is unset or
        has nowhere to go back to.
        """
        error = name_error(name)
        if error:
            raise ArtifactError(error)
        with self._locked():
            artifacts, pointers = self._read_all()
            active = pointers.get(name, {}).get("active")
            if active is None:
                raise ArtifactConflictError.unset(name, "active")
            previous = active.get("previous")
            if previous is None:
                raise ArtifactConflictError.nothing_to_roll_back(name)
            reference = f"{name}:{previous}"
            changes: list[dict[str, Any]] = []
            moved = _move_in(
                artifacts, pointers, "active", name, int(previous), reference, by, reason, changes
            )
            self._write_all(artifacts, pointers)
            self._journal(changes)
        return moved

    def clear(self, which: str, name: str, *, by: str = "", reason: str | None = None) -> Pointer:
        """Unset ``name``'s ``which`` pointer; what it was. Raises `ArtifactError` when it was unset.

        Cleared, ``active`` leaves the bare name meaning the newest again. A
        cleared pointer has no entry left in the index to say who cleared it
        or why, so the journal says (`pointer_changes`).
        """
        which = _pointer_name(which)
        error = name_error(name)
        if error:
            raise ArtifactError(error)
        with self._locked():
            artifacts, pointers = self._read_all()
            standing = pointers.get(name, {})
            if which not in standing:
                raise ArtifactConflictError.unset(name, which)
            was = standing.pop(which)
            if standing:
                pointers[name] = standing
            else:
                pointers.pop(name, None)
            self._write_all(artifacts, pointers)
            cleared = _pointer(name, which, was)
            self._journal([_change(CLEARED, cleared, at=time.time(), by=by, reason=reason)])
        return cleared

    def pointer_changes(self) -> tuple[list[dict[str, Any]], bytes]:
        """The journalled changes, oldest first, and the lines they were read from.

        Each is ``change`` (`MOVED`, `CLEARED` or `PROMOTED`), ``at``, ``by``,
        ``reason`` and ``pointer``, the pointer as the change set it or, for a
        clear, as it stood before. Only whole lines are read: a last line with
        no end yet is left for later. The lines read go to
        `forget_pointer_changes` once what they say is recorded.
        """
        if not self.journal_path.exists():
            # Asked on every look of main's: most find nothing, and need no lock to say so.
            return [], b""
        with self._locked():
            try:
                body = self.journal_path.read_bytes()
            except FileNotFoundError:
                return [], b""
        read = body[: body.rfind(b"\n") + 1]
        return [change for line in read.splitlines() if (change := _journal_line(line))], read

    def forget_pointer_changes(self, read: bytes) -> list[bytes]:
        """Drop the lines `pointer_changes` read, keeping what came since; the lines set aside.

        Dropped only while the journal still starts with them: a reader that
        finds them gone, dropped by another that read them too, drops nothing,
        so a line journalled meanwhile is never lost. A line that is not a
        change -- one cut short when a process died writing it -- is moved to
        ``pointer-moves.rejected.jsonl`` beside the journal rather than lost,
        and returned so the caller can say so.
        """
        if not read:
            return []
        with self._locked():
            try:
                body = self.journal_path.read_bytes()
            except FileNotFoundError:
                return []
            if not body.startswith(read):
                return []
            rejected = [line for line in read.splitlines() if not _journal_line(line)]
            if rejected:
                with self.rejected_journal_path.open("ab") as aside:
                    aside.write(b"".join(line + b"\n" for line in rejected))
            rest = body[len(read) :]
            if rest:
                write_text(self.journal_path, rest.decode("utf-8", errors="replace"))
            else:
                self.journal_path.unlink()
        return rejected

    def promote(self, name: str, *, by: str, reason: str | None = None) -> Pointer:
        """Move ``name``'s ``active`` to where ``shadow`` points and clear ``shadow``, as one change.

        Raises `ArtifactError` when there is no shadow to promote.
        """
        error = name_error(name)
        if error:
            raise ArtifactError(error)
        with self._locked():
            artifacts, pointers = self._read_all()
            standing = pointers.get(name, {})
            if "shadow" not in standing:
                raise ArtifactConflictError.no_shadow_to_promote(name)
            target = _resolve_in(artifacts, pointers, name, "shadow", f"{name}:shadow")
            standing["active"] = _pointer_entry(target, standing.get("active"), by, reason)
            tried = _pointer(name, "shadow", standing.pop("shadow"))
            pointers[name] = standing
            self._write_all(artifacts, pointers)
            active = _pointer(name, "active", standing["active"])
            self._journal(
                [
                    _change(MOVED, active, at=active.moved_at, by=by, reason=reason),
                    _change(PROMOTED, tried, at=active.moved_at, by=by, reason=reason),
                ]
            )
        return active

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
            self._settle(part, self.objects / sha256)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return sha256, hashing.size

    @staticmethod
    def _settle(part: Path, final: Path) -> None:
        """Make ``part`` the stored file ``final``, or drop it when another already is.

        Two adds of the same bytes at once each write a part and reach here
        with the same ``final``. The first to rename wins; the second finds
        the file there and drops its own part. On Windows the rename itself
        is what fails for the second, with the file briefly held by the first,
        so the file being there afterwards counts as settled too: the bytes
        are the same, by the hash in the name.
        """
        if final.exists():
            part.unlink()
            return
        try:
            part.replace(final)
        except OSError:
            if not final.exists():
                raise
            part.unlink(missing_ok=True)

    def _read_all(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
        """The index: every name's versions, and every name's pointers."""
        if not self.index_path.exists():
            return {}, {}
        try:
            stored = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactError.unreadable_index(self.index_path, exc) from exc
        if not isinstance(stored, dict) or stored.get("format") not in _READ_FORMATS:
            raise ArtifactError.unknown_index(self.index_path)
        return stored.get("artifacts", {}), stored.get("pointers", {})

    def _write_all(
        self,
        index: Mapping[str, list[dict[str, Any]]],
        pointers: Mapping[str, dict[str, Any]],
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = {
            "format": INDEX_FORMAT,
            "artifacts": {name: index[name] for name in sorted(index)},
            "pointers": {name: pointers[name] for name in sorted(pointers) if pointers[name]},
        }
        write_text(self.index_path, json.dumps(body, indent=2))

    def _journal(self, changes: list[dict[str, Any]]) -> None:
        """Append ``changes`` to the journal; called under the lock, after the index is written."""
        if not changes:
            return
        lines = "".join(json.dumps(change, sort_keys=True) + "\n" for change in changes)
        with self.journal_path.open("a+b") as journal:
            journal.seek(0, os.SEEK_END)
            if journal.tell():
                journal.seek(-1, os.SEEK_END)
                if journal.read(1) != b"\n":
                    # A line cut short by a process that died writing it: end
                    # it, so it is read as the broken line it is, alone.
                    lines = "\n" + lines
            journal.write(lines.encode("utf-8"))

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
                    raise ArtifactError.busy(self.root, exc) from exc
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


def _pointer_name(which: str) -> str:
    if which not in POINTERS:
        raise ArtifactError.not_a_pointer(which)
    return which


def _pointer(name: str, which: str, entry: Mapping[str, Any]) -> Pointer:
    return Pointer(
        name=name,
        pointer=which,
        version=int(entry["version"]),
        sha256=str(entry["sha256"]),
        previous=None if entry.get("previous") is None else int(entry["previous"]),
        moved_at=float(entry["moved_at"]),
        by=str(entry.get("by", "")),
        reason=None if entry.get("reason") is None else str(entry["reason"]),
    )


def _change(
    change: str, pointer: Pointer, *, at: float, by: str = "", reason: str | None = None
) -> dict[str, Any]:
    """One line of the journal: what ``change`` did to ``pointer``, when, by whom and why."""
    return {"change": change, "at": at, "by": by, "reason": reason, "pointer": asdict(pointer)}


def _journal_line(line: bytes) -> dict[str, Any] | None:
    """One line of the journal as the change it holds, or None for one that holds none."""
    try:
        change = json.loads(line)
        if change["change"] not in (MOVED, CLEARED, PROMOTED):
            return None
        float(change["at"])
        Pointer(**change["pointer"])
    except (ValueError, TypeError, KeyError):
        return None
    return change


def _pointers_of(name: str, pointers: Mapping[str, dict[str, Any]]) -> dict[str, Pointer]:
    return {which: _pointer(name, which, entry) for which, entry in pointers.get(name, {}).items()}


def _move_in(
    artifacts: Mapping[str, list[dict[str, Any]]],
    pointers: dict[str, dict[str, Any]],
    which: str,
    name: str,
    selector: Selector,
    reference: str,
    by: str,
    reason: str | None,
    changes: list[dict[str, Any]],
) -> Pointer:
    """`ArtifactStore.move`, over an index already read and held: changes ``pointers``.

    What it changed is appended to ``changes``, for the journal.
    """
    target = _resolve_in(artifacts, pointers, name, selector, reference)
    standing = pointers.get(name, {})
    current = standing.get(which)
    if current is not None and current["version"] == target.version:
        raise ArtifactConflictError.already(target.reference, which)
    other = "shadow" if which == "active" else "active"
    tried = None
    if standing.get(other, {}).get("version") == target.version:
        if which == "shadow":
            raise ArtifactConflictError.shadow_is_active(target.reference)
        tried = _pointer(name, other, standing.pop(other))
    standing[which] = _pointer_entry(target, current, by, reason)
    pointers[name] = standing
    moved = _pointer(name, which, standing[which])
    changes.append(_change(MOVED, moved, at=moved.moved_at, by=by, reason=reason))
    if tried is not None:
        # Active moved onto the version on trial: that is a promotion.
        changes.append(_change(PROMOTED, tried, at=moved.moved_at, by=by, reason=reason))
    return moved


def _pointer_entry(
    target: Artifact, current: Mapping[str, Any] | None, by: str, reason: str | None
) -> dict[str, Any]:
    """A pointer at ``target``, as the index keeps it, moved from ``current``."""
    return {
        "version": target.version,
        "sha256": target.sha256,
        "previous": None if current is None else int(current["version"]),
        "moved_at": time.time(),
        "by": by,
        "reason": reason,
    }


def _resolve_in(
    artifacts: Mapping[str, list[dict[str, Any]]],
    pointers: Mapping[str, dict[str, Any]],
    name: str,
    selector: Selector,
    reference: str,
) -> Artifact:
    """`ArtifactStore.resolve`, over an index already read."""
    versions = [_artifact(name, entry) for entry in artifacts.get(name, [])]
    if not versions:
        raise ArtifactNotFoundError(reference)
    standing = pointers.get(name, {})
    if selector is None:
        selector = "active" if "active" in standing else "latest"
    if selector == "latest":
        return versions[-1]
    if selector in POINTERS:
        entry = standing.get(selector)
        if entry is None:
            raise ArtifactConflictError.unset(name, selector)
        for artifact in versions:
            if artifact.version == entry["version"]:
                if artifact.sha256 != entry["sha256"]:
                    raise ArtifactError.pointer_bytes_changed(name, selector, artifact.reference)
                return artifact
        raise ArtifactError.pointer_dangles(name, selector, entry["version"])
    for artifact in versions:
        if artifact.version == selector:
            return artifact
    raise ArtifactNotFoundError(reference)


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
        raise ArtifactError.not_pinned(entry)
    sha256, size, version = entry.get("sha256"), entry.get("size"), entry.get("version")
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        raise ArtifactError.no_sha256(entry)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ArtifactError.no_size(entry)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArtifactError.no_version(entry)
    error = name_error(str(entry.get("name", "")))
    if error:
        raise ArtifactError(error)
    if entry.get("format") not in FORMATS:
        raise ArtifactError.unknown_entry_format(entry)
    follows = entry.get("follows")
    if follows is not None and follows not in POINTERS:
        raise ArtifactError.follows_no_pointer(entry, follows)
    return dict(entry)


def pinned_in(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The pinned artifacts a spawn config names, checked, by the name the agent uses."""
    wanted = config.get("artifacts") or {}
    if not isinstance(wanted, Mapping):
        raise ArtifactError.pinned_not_a_mapping()
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
        raise ArtifactError.archive_outside(member.name)
    if not relative.parts:
        return
    destination = into.joinpath(*relative.parts)
    if member.isdir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    if not member.isfile():
        raise ArtifactError.archive_not_a_file(member.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = tar.extractfile(member)
    if source is None:
        raise ArtifactError.archive_unreadable(member.name)
    with source, destination.open("wb") as out:
        shutil.copyfileobj(source, out, _CHUNK)


def summary(store: ArtifactStore, *, limit: int = 50) -> list[str]:
    """One line per name in ``store``: its newest version, format, size, arrays and what was said of it.

    For a person reading a list and for a model choosing one, so it says what
    tells models apart and nothing that does not: no hashes, no times. Where a
    pointer is set, the line says which version is active or shadow. The arrays
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
        for which, pointer in store.pointers(name).items():
            line += f", {which} {pointer.reference}"
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
            raise ArtifactError.directory_with_format(path)
        return FORMATS[DIRECTORY_FORMAT]
    if named is None:
        guessed = guess_format(path)
        if guessed is None:
            raise ArtifactError.format_unsaid(path)
        named = guessed
    if named == DIRECTORY_FORMAT:
        raise ArtifactError.file_not_directory(path)
    fmt = FORMATS.get(named)
    if fmt is None:
        raise ArtifactError.unknown_format(named)
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
            raise ArtifactError.link(path)
        if not (path.is_dir() or path.is_file()):
            raise ArtifactError.not_file_or_directory(path)
        found.append(path)
    return found


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
