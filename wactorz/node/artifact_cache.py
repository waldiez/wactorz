"""The artifacts a node has fetched, kept for the next agent that names them.

An artifact is a model main keeps (see `wactorz.core.artifacts`). A node fetches
one the first time an agent here names it, and keeps it under its SHA-256 in
``<state>/..artifact-cache/``, so a restart, a reboot or another agent naming the
same model fetches nothing. A file is checked against its hash before an agent
starts with it: one damaged on the card is fetched again rather than loaded.

The cache is bounded by ``WACTORZ_NODE_ARTIFACT_CACHE_BYTES``. Past it, files no
agent here names are deleted, the least recently used first; one an agent names
stays whatever the total, since deleting it would only fetch it again.

Every method reads or writes the disk, and a model can take a while to hash:
the runner calls them through ``asyncio.to_thread``.
"""

import contextlib
import hashlib
import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..core.artifacts import UNPACKED_DIRNAME, readable_path

logger = logging.getLogger(__name__)

#: The directory, inside the state directory, the cache is kept in. Starts with
#: `..`, which no agent's name may, so it never collides with an agent's own.
CACHE_DIRNAME = "..artifact-cache"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20


class ArtifactCache:
    """The fetched artifacts under one directory, by hash."""

    def __init__(self, directory: Path, max_bytes: int, unpacked: Path | None = None) -> None:
        self.directory = directory
        #: The most the cache keeps of files no agent names; 0 for no limit.
        self.max_bytes = max_bytes
        #: Where a directory artifact is unpacked for an agent to read.
        self.unpacked = unpacked or directory.parent / UNPACKED_DIRNAME

    def path(self, sha256: str) -> Path:
        return self.directory / sha256

    def readable_path(self, entry: Mapping[str, Any]) -> Path:
        """Where an agent reads the pinned ``entry``: its file, or the directory it unpacks to.

        Unpacks a directory the first time.
        """
        return readable_path(self.path(str(entry["sha256"])), entry, self.unpacked)

    def usable(self, wanted: Iterable[str]) -> set[str]:
        """Those of ``wanted`` held here and still matching their hash.

        One that no longer matches is deleted, so it is fetched again.
        """
        usable: set[str] = set()
        for sha in wanted:
            path = self.path(sha)
            if not path.is_file():
                continue
            if _hash_of(path) == sha:
                usable.add(sha)
                continue
            logger.warning(
                "[artifacts] %s… no longer matches its hash; fetching it again", sha[:12]
            )
            path.unlink(missing_ok=True)
            shutil.rmtree(self.unpacked / sha, ignore_errors=True)
        return usable

    def take(self, sha256: str, arrived: Path) -> Path:
        """Keep the file that arrived as ``sha256``, already checked against it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        kept = self.path(sha256)
        arrived.replace(kept)
        return kept

    def used(self, shas: Iterable[str]) -> None:
        """Mark ``shas`` as just used, so they are the last to go."""
        for sha in shas:
            with contextlib.suppress(FileNotFoundError):
                os.utime(self.path(sha))

    def trim(self, named: set[str]) -> list[str]:
        """Delete what no agent names, oldest use first, until the cache fits; what went."""
        if not self.max_bytes or not self.directory.is_dir():
            return []
        held = []
        for path in self.directory.iterdir():
            if _SHA256.match(path.name) and path.is_file():
                stat = path.stat()
                held.append((stat.st_mtime, path, stat.st_size))
        total = sum(size for _, _, size in held)
        removed: list[str] = []
        for _, path, size in sorted(held):
            if total <= self.max_bytes:
                break
            if path.name in named:
                continue
            path.unlink(missing_ok=True)
            shutil.rmtree(self.unpacked / path.name, ignore_errors=True)
            total -= size
            removed.append(path.name)
        return removed


def _hash_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
