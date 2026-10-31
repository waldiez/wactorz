"""Taking the files a node's recorder sends, and filing them where main keeps the record.

A node records readings where it is and sends each closed file to main
(`wactorz.node.data_shipping`): a notice saying where the file belongs, then
the bytes on the blob path a migration or an artifact uses, which main's
migration listener already receives into its inbox and checks against the
hash. This listener takes the notice, waits for the blob, puts the file in
main's data layout under the source and hour the notice names, and answers::

    node → nodes/<node>/data_file            {source, path, sha256, size}
    node → nodes/<node>/blob_return/<sha256>
    main → nodes/<node>/data_ack             {sha256, ok, reason}   signed

A notice is unauthenticated; what it can do is place a file, whose content is
checked against the hash, under a path this listener builds from a source and
a file name it has checked, inside main's data directory. A path that is not
of that shape is refused and said so. A file of the same name already there,
main's own hour of the same source, is kept: the arriving one becomes a further
part of that hour, as the writer numbers parts. ``docs/design/data-storage.md``
is the decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.blob_transfer import BlobsUnusable, wait_for
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from ...core.paths import resolve_state_dir
from ...data.files import DataError, check_source, data_root

if TYPE_CHECKING:
    from .hosts import ArtifactHost

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting a dropped subscription.
RECONNECT_DELAY_S = 5.0

#: What a file's name may be: an hour, or a part of one, as the writer names them.
_FILE = re.compile(r"^(?P<hour>\d{2})(?:-(?P<part>\d{4}))?\.parquet$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DataFileError(ValueError):
    """A notice that does not name a file main can take, and why."""

    @classmethod
    def not_a_notice(cls) -> DataFileError:
        return cls("a data file notice names source, path, sha256 and size")

    @classmethod
    def path_outside_source(cls, path: str, source: str) -> DataFileError:
        return cls(f"{path!r} is not an hour of {source!r}")

    @classmethod
    def too_large(cls, size: int, limit: int) -> DataFileError:
        return cls(f"{size} bytes is over the {limit}-byte limit for a file sent to main")


def placement(root: Path, source: str, path: str) -> Path:
    """Where the file a notice names goes under ``root``: a checked source, a checked hour's name.

    Raises `DataError` or `DataFileError` for a path that is not of that shape.
    """
    check_source(source)
    parts = path.split("/")
    if (
        len(parts) < 5
        or "/".join(parts[:-4]) != source
        or not all(p.isdigit() and len(p) in (2, 4) for p in parts[-4:-1])
        or not _FILE.match(parts[-1])
    ):
        raise DataFileError.path_outside_source(path, source)
    return root.joinpath(*source.split("/"), *parts[-4:])


def next_free(target: Path) -> Path:
    """``target``, or the next part name of its hour when a file is there already."""
    if not target.exists():
        return target
    hour = target.name[:2]
    part = 1
    while (target.parent / f"{hour}-{part:04d}.parquet").exists():
        part += 1
    return target.parent / f"{hour}-{part:04d}.parquet"


class DataFiles:
    """Receives the files nodes send, and answers."""

    def __init__(self, host: ArtifactHost | None = None) -> None:
        self.host = host
        self._taking: set[asyncio.Task[Any]] = set()
        #: What was filed, by the node it came from, for the log and tests.
        self.filed: list[tuple[str, Path]] = []

    @property
    def root(self) -> Path:
        return data_root(resolve_state_dir())

    async def receive(self, topic: str, payload: bytes | None) -> None:
        """A node's notice: take the file it names, off this listener's loop."""
        host = self.host
        if host is None or not payload:
            return
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "nodes" or parts[2] != "data_file":
            return
        try:
            data = json.loads(payload.decode())
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        task = asyncio.create_task(self.take(parts[1], data))
        self._taking.add(task)
        task.add_done_callback(self._taking.discard)

    async def take(self, node: str, data: dict[str, Any]) -> bool:
        """Wait for the file the notice names, file it, and answer; whether it was kept."""
        host = self.host
        if host is None:
            return False
        sha = str(data.get("sha256") or "")
        try:
            source, path, size = self._checked(data)
            target = placement(self.root, source, path)
        except (DataError, DataFileError) as exc:
            await self._answer(node, sha, False, str(exc))
            return False
        inbox = host.migration.inbox
        if inbox.max_bytes and size > inbox.max_bytes:
            await self._answer(
                node, sha, False, str(DataFileError.too_large(size, inbox.max_bytes))
            )
            return False
        try:
            arrived = (await inbox.wait_for({sha}, wait_for(size)))[sha]
        except BlobsUnusable as exc:
            await self._answer(node, sha, False, f"the file did not arrive: {exc}")
            return False
        try:
            placed = await asyncio.to_thread(self._place, arrived, target)
        except OSError as exc:
            await inbox.release({sha})
            await self._answer(node, sha, False, f"the file could not be kept: {exc}")
            return False
        await inbox.release({sha})
        self.filed.append((node, placed))
        logger.info("[%s] %s from %r filed as %s", host.name, path, node, placed)
        await self._answer(node, sha, True, "")
        return True

    @staticmethod
    def _checked(data: dict[str, Any]) -> tuple[str, str, int]:
        source, path, sha, size = (
            data.get("source"),
            data.get("path"),
            data.get("sha256"),
            data.get("size"),
        )
        if (
            not isinstance(source, str)
            or not isinstance(path, str)
            or not isinstance(sha, str)
            or not _SHA256.match(sha)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise DataFileError.not_a_notice()
        return source, path, size

    def _place(self, arrived: Path, target: Path) -> Path:
        final = next_free(target)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(arrived), str(final))
        return final

    async def _answer(self, node: str, sha: str, ok: bool, reason: str) -> None:
        host = self.host
        if host is None:
            return
        if not ok:
            logger.warning("[%s] a data file from %r was not kept: %s", host.name, node, reason)
        # Signed on the way out, because it is a node control topic: see
        # `MainActor._publish_properties`.
        await host._mqtt_publish(
            f"nodes/{node}/data_ack",
            {"sha256": sha, "ok": ok, "reason": reason, "timestamp": time.time()},
            qos=1,
        )

    async def listener(self) -> None:
        """Follow the nodes' notices, until the actor stops."""
        host = self.host
        if host is None:
            return
        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "datafiles"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe("nodes/+/data_file", qos=1)
                    last_error = None
                    async for message in client.messages:
                        await self.receive(str(message.topic), message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[%s] data file listener error: %s. Reconnecting in %ss…",
                        host.name,
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                await asyncio.sleep(RECONNECT_DELAY_S)
