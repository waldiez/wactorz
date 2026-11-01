# SPDX-License-Identifier: Apache-2.0
"""Sending main the readings a node recorded, one closed file at a time.

A recorder on a node files readings under the node's own data directory
(`wactorz.agents.recorder_agent`). Main's copy is the record, so each file is
sent there once it is closed, over the blob path that already carries
migration state and artifacts::

    node → nodes/<node>/data_file            {source, path, sha256, size}
    node → nodes/<node>/blob_return/<sha256> the file, in chunks
    main → nodes/<node>/data_ack             {sha256, ok, reason}   signed

The notice says where the file belongs; main checks the hash as it does for
every blob, files it in its own layout, and answers. A file main has not
answered for is sent again after a while, and one it has is kept here until a
retention passes, so a node away for a day loses nothing and a file is never
sent twice without need. What was sent and answered is kept in a small record
beside the files. ``docs/design/data-storage.md`` is the decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from ..core.blob_transfer import send_file
from ..data.files import DATA_DIRNAME, data_root

logger = logging.getLogger(__name__)

#: How often the node looks for closed files to send.
SHIP_EVERY_S = 60.0

#: How long a file waits for main's answer before it is sent again.
RESEND_AFTER_S = 600.0

#: How long an answered file is kept here before it is removed.
KEEP_AFTER_ACK_S = 7 * 24 * 3600.0

#: The most files sent in one look, so a backlog goes out at a pace the broker takes.
PER_LOOK = 20

#: Where the node keeps what it sent and what main answered.
RECORD_NAME = ".shipped.json"

Publish = Callable[[str, Any], Coroutine[Any, Any, None]]


def sha256_of(path: Path) -> str:
    """The hash of the file at ``path``, read in pieces."""
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class DataShipper:
    """Sends main the closed files under the node's data directory, and keeps track."""

    def __init__(
        self,
        node_name: str,
        state_dir: str | Path,
        publish: Publish,
        *,
        every: float = SHIP_EVERY_S,
    ) -> None:
        self.node_name = node_name
        self.root = data_root(state_dir)
        self._publish = publish
        self.every = every
        #: path (relative to the data directory) -> what was sent and when,
        #: and whether main answered.
        self._record: dict[str, dict[str, Any]] | None = None

    # ── The record ────────────────────────────────────────────────────────

    @property
    def record_path(self) -> Path:
        return self.root / RECORD_NAME

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._record is not None:
            return self._record
        try:
            loaded = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        self._record = {k: dict(v) for k, v in loaded.items() if isinstance(v, dict)}
        return self._record

    def _save(self) -> None:
        if self._record is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.record_path.with_name(RECORD_NAME + ".tmp")
        temp.write_text(json.dumps(self._record, sort_keys=True), encoding="utf-8")
        temp.replace(self.record_path)

    # ── Looking and sending ───────────────────────────────────────────────

    def closed_files(self) -> list[Path]:
        """Every Parquet file under the data directory: written whole, so closed."""
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.rglob("*.parquet") if p.is_file())

    def due(self, now: float | None = None) -> list[Path]:
        """The files to send now: never sent, or sent and unanswered for `RESEND_AFTER_S`."""
        moment = time.time() if now is None else now
        record = self._load()
        found: list[Path] = []
        for path in self.closed_files():
            key = path.relative_to(self.root).as_posix()
            sent = record.get(key)
            if sent is None or (
                not sent.get("acked") and moment - float(sent.get("sent_at", 0)) >= RESEND_AFTER_S
            ):
                found.append(path)
        return found[:PER_LOOK]

    async def look(self) -> list[str]:
        """Send what is due and drop what was answered long ago; the paths sent."""
        sent: list[str] = []
        for path in await asyncio.to_thread(self.due):
            try:
                sent.append(await self.send(path))
            except OSError as exc:
                logger.warning("[%s] cannot send %s to main: %s", self.node_name, path, exc)
        await asyncio.to_thread(self.trim)
        return sent

    async def send(self, path: Path) -> str:
        """Send one file: the notice, then the bytes; the path as main is told it."""
        key = path.relative_to(self.root).as_posix()
        sha = await asyncio.to_thread(sha256_of, path)
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        source = key.rsplit("/", 4)[0] if key.count("/") >= 4 else key
        await self._publish(
            f"nodes/{self.node_name}/data_file",
            {
                "source": source,
                "path": key,
                "sha256": sha,
                "size": size,
                "node": self.node_name,
                "timestamp": time.time(),
            },
        )
        await send_file(path, f"nodes/{self.node_name}/blob_return/{sha}", self._publish)
        record = self._load()
        record[key] = {"sha256": sha, "sent_at": time.time(), "acked": False}
        await asyncio.to_thread(self._save)
        return key

    def acknowledge(self, sha256: str, ok: bool, reason: str = "") -> str | None:
        """Main answered for the file hashing to ``sha256``; the path it was, or None."""
        record = self._load()
        for key, sent in record.items():
            if sent.get("sha256") != sha256:
                continue
            if ok:
                sent["acked"] = True
                sent["acked_at"] = time.time()
            else:
                # Sent again at the next look: main said why it could not keep it.
                sent["sent_at"] = 0.0
                sent["refused"] = reason
            self._save()
            return key
        return None

    def trim(self, now: float | None = None) -> list[str]:
        """Remove the files main answered for more than `KEEP_AFTER_ACK_S` ago; what went."""
        moment = time.time() if now is None else now
        record = self._load()
        gone: list[str] = []
        for key, sent in list(record.items()):
            path = self.root / key
            if not sent.get("acked"):
                continue
            if moment - float(sent.get("acked_at", moment)) < KEEP_AFTER_ACK_S:
                continue
            path.unlink(missing_ok=True)
            del record[key]
            gone.append(key)
        for key in [
            k for k in record if not (self.root / k).exists() and not record[k].get("acked")
        ]:
            # Gone before it was answered for: nothing left to send.
            del record[key]
        if gone or record is not None:
            self._save()
        return gone

    async def run(self) -> None:
        """Look on a cadence, until cancelled."""
        while True:
            await asyncio.sleep(self.every)
            try:
                await self.look()
            except Exception as exc:
                logger.warning("[%s] data shipping: %s", self.node_name, exc)


def data_dir_of(state_dir: str | Path) -> Path:
    """Where a node's recorder files its readings, for whoever asks."""
    return Path(state_dir) / DATA_DIRNAME
