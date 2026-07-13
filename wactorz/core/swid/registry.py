"""File-backed CEL registry implementing swid's ``AsyncSWIDRegistry`` protocol.

One JSON file per DID under ``<data_dir>/logs/``, so registered DIDs survive a
server restart. Semantics mirror the library's ``AsyncInMemoryRegistry``
exactly: ``register`` raises ``ValueError`` on an empty log or a duplicate DID;
``get_log``/``append`` raise ``KeyError`` for unknown DIDs (the resolver maps
that to ``notFound``). No chain verification happens here — the minter is the
only writer, and ``resolve_async`` verifies the chain on every read.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from swid.cel import derive_scid_from_event
from swid.keystore.util import atomic_write, encode_did


class FileSWIDRegistry:
    """``AsyncSWIDRegistry`` over a directory of per-DID JSON event logs."""

    def __init__(self, data_dir: Path) -> None:
        self._logs_dir = Path(data_dir) / "logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        # Single-process server: one lock serialises writers (matches the
        # in-memory reference; readers go through it too for simplicity).
        self._lock = asyncio.Lock()

    def _path(self, did: str) -> Path:
        return self._logs_dir / f"{encode_did(did)}.json"

    async def register(self, log: list[dict[str, Any]]) -> str:
        """Persist a genesis CEL and return the ``did:swid`` derived from it.

        Raises ValueError if ``log`` is empty or the DID is already registered.
        """
        if not log:
            raise ValueError("cannot register an empty CEL")
        scid = derive_scid_from_event(log[0])
        did = f"did:swid:{scid}"
        async with self._lock:
            path = self._path(did)
            if await asyncio.to_thread(path.exists):
                raise ValueError(f"DID {did} is already registered")
            data = json.dumps(list(log)).encode("utf-8")
            await asyncio.to_thread(atomic_write, path, data)
        return did

    async def get_log(self, did: str) -> list[dict[str, Any]]:
        """Return the full CEL for ``did``; raise KeyError if unknown."""
        async with self._lock:
            return await asyncio.to_thread(self._read, did)

    async def append(self, did: str, event: dict[str, Any]) -> None:
        """Append a signed update/deactivation event to ``did``'s CEL.

        Raises KeyError if the DID is not registered.
        """
        async with self._lock:
            log = await asyncio.to_thread(self._read, did)
            log.append(event)
            data = json.dumps(log).encode("utf-8")
            await asyncio.to_thread(atomic_write, self._path(did), data)

    async def close(self) -> None:
        """Nothing to release — files are closed after every read/write."""

    def _read(self, did: str) -> list[dict[str, Any]]:
        path = self._path(did)
        if not path.exists():
            raise KeyError(f"DID {did} is not registered")
        return json.loads(path.read_text(encoding="utf-8"))
