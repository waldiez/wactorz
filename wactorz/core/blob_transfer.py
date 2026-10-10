"""Moving an agent's blobs to another machine, with the rest of its state.

A migration ships an agent's state as JSON. A blob -- a model, an array, raw
bytes -- has no JSON form, so in the snapshot it becomes a reference naming its
format, its SHA-256 and its size, and its bytes follow on a topic of their own,
in chunks:

- main to a node on ``nodes/<node>/blob/<sha256>``;
- a node to main on ``nodes/<node>/blob_return/<sha256>``.

Each chunk carries its index and the number of chunks in a short header, and
the receiver writes them to a file in order, hashing as it goes. One topic per
blob keeps its chunks in order on the way, since the broker delivers the
messages of one topic in the order they were published.

**The hash is what is trusted, not the chunks.** Nothing about a chunk says who
sent it. The reference that names the hash arrives in a message that is
trusted already -- a spawn main signed, a state return quoting a token main
minted -- and a blob is used only when its bytes hash to what that names. So a
chunk from anyone else is at worst a file nobody asks for, cleared after a
while; and only main and the node itself may write to a node's topics at all.

**A node is not trusted to run code on main.** Some formats load by running
code (`Encoder.runs_code`). Main sends those to a node, which already runs
whatever code main sends it; a node does not send them back. Such a value stays
on the node, and the migration is refused unless forced, as for any other value
that cannot travel. That holds for a move between two nodes as well, which goes
through main: one node is not trusted to run code on another.
"""

import asyncio
import hashlib
import json
import logging
import re
import struct
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .blobs import BLOB_MARK, encoder_for, encoder_named

logger = logging.getLogger(__name__)

#: How much of a blob one message carries: well under any broker's limit, and
#: small enough that a board holds a few in memory without noticing.
CHUNK_BYTES = 256 * 1024

#: Each chunk's header: its index, and how many chunks the blob has.
_HEADER = struct.Struct(">II")

#: A SHA-256 as a reference names it, and as the receiver names its file.
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: How long to wait for a blob's chunks at the least, and the slowest rate they
#: are expected at: a board on a weak wireless link still finishes inside it.
WAIT_AT_LEAST_S = 60.0
SLOWEST_BYTES_PER_S = 100_000

#: The directory, inside the state directory, that blobs are received into
#: before they are used. No agent can be given this name -- one that starts
#: with `..` is refused as a state path -- so it never collides with an
#: agent's own directory.
INBOX_DIRNAME = "..incoming-blobs"


def to_node_topic(node: str, sha256: str) -> str:
    """Where main sends ``node`` a blob."""
    return f"nodes/{node}/blob/{sha256}"


def sha256_of_topic(topic: str) -> str | None:
    """The blob a chunk's topic names, or None if it names none properly."""
    sha = topic.rsplit("/", 1)[-1]
    return sha if _SHA256.match(sha) else None


# ── References ────────────────────────────────────────────────────────────────


def reference(format_name: str, data: bytes) -> dict[str, Any]:
    """What a snapshot holds in place of a blob: its format, hash and size."""
    return {BLOB_MARK: format_name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def is_reference(value: Any) -> bool:
    """Whether ``value`` stands for a blob that travels separately."""
    return (
        isinstance(value, dict)
        and set(value) == {BLOB_MARK, "sha256", "size"}
        and isinstance(value[BLOB_MARK], str)
        and isinstance(value["sha256"], str)
        and bool(_SHA256.match(value["sha256"]))
        and isinstance(value["size"], int)
        and value["size"] >= 0
    )


def references(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each key of ``state`` that stands for a blob, with its reference."""
    return {key: value for key, value in state.items() if is_reference(value)}


def blob_bytes(state: Mapping[str, Any]) -> int:
    """How many bytes of blobs ``state`` refers to."""
    return sum(ref["size"] for ref in references(state).values())


def wait_for(total_bytes: int) -> float:
    """How long to give ``total_bytes`` of blobs to arrive."""
    return WAIT_AT_LEAST_S + total_bytes / SLOWEST_BYTES_PER_S


# ── Packing a snapshot ────────────────────────────────────────────────────────


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def cannot_travel(values: Mapping[str, Any], *, code_allowed: bool) -> list[str]:
    """The keys of ``values`` that cannot go with the agent, without encoding anything.

    A value travels as JSON, or as a blob in a format the receiving side will
    load. ``code_allowed`` says whether it will load one that runs code.
    """
    left: list[str] = []
    for key, value in values.items():
        if _is_json(value):
            continue
        encoder = encoder_for(value)
        if encoder is None or (encoder.runs_code and not code_allowed):
            left.append(key)
    return left


def json_part(values: Mapping[str, Any]) -> dict[str, Any]:
    """The values of ``values`` that travel as JSON, without the blobs and the rest."""
    return {key: value for key, value in values.items() if _is_json(value)}


@dataclass
class Packed:
    """An agent's state, ready to travel."""

    #: The state as JSON, with a reference where each blob was.
    state: dict[str, Any] = field(default_factory=dict)
    #: Each blob's bytes, by its SHA-256.
    blobs: dict[str, bytes] = field(default_factory=dict)
    #: The keys left behind, as `cannot_travel` names them.
    left_behind: list[str] = field(default_factory=list)

    @property
    def blob_bytes(self) -> int:
        return sum(len(data) for data in self.blobs.values())


def pack(values: Mapping[str, Any], *, code_allowed: bool) -> Packed:
    """``values`` as JSON and blobs, leaving behind what cannot travel.

    Encodes every blob, which takes as long as writing it would: call it off
    the event loop for an agent that keeps models.
    """
    packed = Packed()
    for key, value in values.items():
        if _is_json(value):
            packed.state[key] = value
            continue
        encoder = encoder_for(value)
        if encoder is None or (encoder.runs_code and not code_allowed):
            packed.left_behind.append(key)
            continue
        data = encoder.encode(value)
        ref = reference(encoder.name, data)
        packed.state[key] = ref
        packed.blobs[ref["sha256"]] = data
    return packed


class BlobsUnusable(Exception):
    """A snapshot's blobs could not all be put back."""


class BlobFormatUnknown(BlobsUnusable):
    def __init__(self, key: str) -> None:
        super().__init__(f"'{key}' is in a format this side does not know")


class BlobMissing(BlobsUnusable):
    def __init__(self, key: str) -> None:
        super().__init__(f"'{key}' never arrived")


class BlobDidNotLoad(BlobsUnusable):
    def __init__(self, key: str, exc: Exception) -> None:
        super().__init__(f"'{key}' did not load: {type(exc).__name__}: {exc}")


class BlobsLate(BlobsUnusable):
    def __init__(self, missing: int, wanted: int, timeout: float) -> None:
        super().__init__(f"{missing} of {wanted} blob(s) did not arrive within {timeout:.0f}s")


class BlobsTooLarge(BlobsUnusable):
    def __init__(self, total: int, limit: int) -> None:
        super().__init__(f"its blobs are {total} bytes, over the {limit}-byte limit")


def unpack(
    state: Mapping[str, Any], received: Mapping[str, Path], *, code_allowed: bool
) -> tuple[dict[str, Any], list[str]]:
    """``state`` with each reference replaced by its blob's value, and the keys refused.

    ``received`` maps each SHA-256 to the file it arrived in. A reference to a
    format that runs code is refused unless ``code_allowed``: the key is left
    out and named, as a sender that checked first would have done. Raises
    `BlobsUnusable` for a blob that is missing or does not load, since the
    agent would otherwise start without it and nothing would say so.
    """
    values: dict[str, Any] = {}
    refused: list[str] = []
    for key, value in state.items():
        if not is_reference(value):
            values[key] = value
            continue
        encoder = encoder_named(value[BLOB_MARK])
        if encoder is None:
            raise BlobFormatUnknown(key)
        if encoder.runs_code and not code_allowed:
            refused.append(key)
            continue
        path = received.get(value["sha256"])
        if path is None:
            raise BlobMissing(key)
        try:
            values[key] = encoder.decode(path.read_bytes())
        except Exception as exc:
            raise BlobDidNotLoad(key, exc) from exc
    return values, refused


# ── Sending ───────────────────────────────────────────────────────────────────


def chunks(data: bytes) -> Iterator[bytes]:
    """``data`` as the messages that carry it, each with its header."""
    count = max(1, -(-len(data) // CHUNK_BYTES))
    for index in range(count):
        part = data[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]
        yield _HEADER.pack(index, count) + part


#: Publishes one chunk: the topic and the bytes, at QoS 1.
Publish = Callable[[str, bytes], Awaitable[None]]


async def send(
    blobs: Mapping[str, bytes], topic_for: Callable[[str], str], publish: Publish
) -> None:
    """Publish each blob's chunks on its own topic."""
    for sha, data in blobs.items():
        topic = topic_for(sha)
        for chunk in chunks(data):
            await publish(topic, chunk)


# ── Receiving ─────────────────────────────────────────────────────────────────


@dataclass
class _Arriving:
    """A blob part way through arriving."""

    count: int
    next_index: int
    hasher: Any
    size: int
    started: float


class Inbox:
    """Where blobs arrive, are checked against their hash, and wait to be used.

    Chunks are appended to `<sha256>.part` as they come and the file is renamed
    to `<sha256>` once the last one is in and the hash matches. A blob that is
    asked for is waited on until it is complete; one that never is, or is never
    asked for, is removed by :meth:`sweep`.

    Every name in the directory is a hash this class checked, so nothing
    arriving can choose where it is written.
    """

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.directory = directory
        #: The most bytes one blob may have, and the most all blobs in progress
        #: may hold between them.
        self.max_bytes = max_bytes
        self._arriving: dict[str, _Arriving] = {}
        self._complete = asyncio.Condition()

    def path(self, sha256: str) -> Path:
        return self.directory / sha256

    def _part(self, sha256: str) -> Path:
        return self.directory / f"{sha256}.part"

    async def accept(self, sha256: str, message: bytes) -> None:
        """Take one chunk of ``sha256``'s blob."""
        if not _SHA256.match(sha256) or len(message) < _HEADER.size:
            return
        index, count = _HEADER.unpack_from(message)
        data = message[_HEADER.size :]
        arriving = self._arriving.get(sha256)
        if arriving is not None and index < arriving.next_index:
            # Already written. The broker sends a QoS 1 message again when it
            # did not hear it was received, across a reconnect say, and the
            # same blob sent again carries the same bytes in the same chunks:
            # either way this one adds nothing, and the rest still fit on.
            return
        if arriving is None:
            if index != 0 or not await self._begin(sha256, count):
                return
            arriving = self._arriving[sha256]
        if index != arriving.next_index or count != arriving.count:
            # A chunk missing in between: the file can no longer match its hash.
            logger.warning(
                "[blobs] %s… is missing chunk %s (got %s of %s); dropped",
                sha256[:12],
                arriving.next_index,
                index,
                count,
            )
            await self._abandon(sha256)
            return
        arriving.size += len(data)
        if arriving.size > self.max_bytes:
            logger.warning(
                "[blobs] %s… is larger than %s bytes; dropped", sha256[:12], self.max_bytes
            )
            await self._abandon(sha256)
            return
        arriving.hasher.update(data)
        arriving.next_index += 1
        await asyncio.to_thread(_append, self._part(sha256), data)
        if arriving.next_index == arriving.count:
            await self._finish(sha256, arriving)

    async def _begin(self, sha256: str, count: int) -> bool:
        """Start receiving ``sha256``. False when it is not worth receiving."""
        if count < 1 or (count - 1) * CHUNK_BYTES > self.max_bytes:
            logger.warning(
                "[blobs] %s… announces %s chunks; too large, dropped", sha256[:12], count
            )
            return False
        if await asyncio.to_thread(self.path(sha256).exists):
            # Here already: a blob is its hash, so another copy adds nothing.
            self._arriving.pop(sha256, None)
            return False
        in_progress = sum(a.size for a in self._arriving.values())
        if in_progress > self.max_bytes:
            logger.warning("[blobs] Too much arriving at once; %s… dropped", sha256[:12])
            return False
        await asyncio.to_thread(_start_part, self._part(sha256))
        self._arriving[sha256] = _Arriving(count, 0, hashlib.sha256(), 0, time.monotonic())
        return True

    async def _finish(self, sha256: str, arriving: _Arriving) -> None:
        del self._arriving[sha256]
        if arriving.hasher.hexdigest() != sha256:
            logger.warning("[blobs] %s… does not match its hash; dropped", sha256[:12])
            await asyncio.to_thread(self._part(sha256).unlink, missing_ok=True)
            return
        await asyncio.to_thread(self._part(sha256).replace, self.path(sha256))
        async with self._complete:
            self._complete.notify_all()

    async def _abandon(self, sha256: str) -> None:
        self._arriving.pop(sha256, None)
        await asyncio.to_thread(self._part(sha256).unlink, missing_ok=True)

    async def wait_for(self, shas: Iterable[str], timeout: float) -> dict[str, Path]:
        """The file of each blob in ``shas``, once all have arrived.

        Raises `BlobsUnusable` naming how many never did, after ``timeout``.
        """
        wanted = set(shas)
        if not wanted:
            return {}
        try:
            await asyncio.wait_for(self._all_here(wanted), timeout)
        except asyncio.TimeoutError:
            gone = await self._missing(wanted)
            raise BlobsLate(len(gone), len(wanted), timeout) from None
        return {sha: self.path(sha) for sha in wanted}

    async def _missing(self, wanted: set[str]) -> set[str]:
        paths = {sha: self.path(sha) for sha in wanted}
        return await asyncio.to_thread(_absent, paths)

    async def _all_here(self, wanted: set[str]) -> None:
        async with self._complete:
            while await self._missing(wanted):
                # Woken by each blob that completes. Checked under the same
                # lock `_finish` notifies under, so a blob completing between
                # the check and the wait still wakes it.
                await self._complete.wait()

    async def release(self, shas: Iterable[str]) -> None:
        """Remove blobs that have been used."""
        paths = [self.path(sha) for sha in shas if _SHA256.match(sha)]
        await asyncio.to_thread(_remove, paths)

    async def sweep(self, older_than_s: float) -> None:
        """Remove what has waited longer than ``older_than_s``: used, abandoned, or never asked for."""
        cutoff = time.monotonic() - older_than_s
        for sha in [s for s, a in self._arriving.items() if a.started < cutoff]:
            await self._abandon(sha)
        await asyncio.to_thread(_remove_older, self.directory, time.time() - older_than_s)


def _start_part(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as file:
        file.write(data)


def _absent(paths: Mapping[str, Path]) -> set[str]:
    return {sha for sha, path in paths.items() if not path.exists()}


def _remove(paths: Iterable[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _remove_older(directory: Path, cutoff: float) -> None:
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue
