"""Live recognition against a sherpa-onnx streaming server.

The protocol is small: connect, send raw 32-bit float PCM as binary frames, read
JSON back, send ``Done!`` to finish. Each reply carries the *whole current text*
of its segment rather than an addition to it, because a transducer revises an
open segment as more audio arrives -- a hypothesis formed from room noise is
replaced outright once speech gives the decoder enough to go on.

That is why a caller keeps text per segment and replaces it. Appending would keep
every discarded guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

#: What the models are trained at, and what the server assumes it is being sent.
SAMPLE_RATE = 16000

#: How long to wait for the server to accept a connection. A recogniser that is
#: not there fails at once; this bounds one that accepts slowly.
CONNECT_TIMEOUT = 10.0

#: How long a session may sit with no reply before it is abandoned. Generous:
#: silence is normal while someone gathers their thoughts, and the caller stops
#: the session itself when the turn ends.
IDLE_TIMEOUT = 300.0

#: The word the server waits for before it flushes and closes.
DONE = "Done!"

#: How much audio may wait to be sent before the oldest is dropped. A client
#: that streams faster than the recogniser consumes -- or a recogniser that
#: stalls -- would otherwise grow this without limit. At 16 kHz float32 this is
#: roughly ten seconds, which is far more slack than a live turn needs.
MAX_PENDING_FRAMES = 100


@dataclass(frozen=True)
class Partial:
    """One reading of a segment, superseding whatever came before it.

    `final` marks the reading the server settled on: the segment number changes
    after an endpoint, so a caller sees the previous segment stop moving rather
    than being told twice.
    """

    text: str
    segment: int
    final: bool


def is_streaming_uri(uri: str) -> bool:
    """Whether this address names a streaming recogniser rather than a batch one.

    The scheme decides. A deployment sets one address, and what the client does
    follows from what it connected to instead of from a second setting that can
    disagree with it.
    """
    return urlparse(uri).scheme in ("ws", "wss")


class StreamingSession:
    """One live recognition, from the first frame of audio to the last word."""

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse[bool] | None = None
        self._segment = 0

    async def __aenter__(self) -> StreamingSession:
        # No total timeout: the session lives as long as someone is speaking.
        # sock_read bounds the silence instead, so a server that stops answering
        # is noticed without cutting off a pause mid-sentence.
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=CONNECT_TIMEOUT, sock_read=IDLE_TIMEOUT
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            self._ws = await self._session.ws_connect(self._uri)
        except Exception:
            await self._session.close()
            self._session = None
            raise
        return self

    async def __aexit__(self, *_: object) -> bool:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._ws = None
        self._session = None
        return False

    async def feed(self, pcm: bytes) -> None:
        """Send one frame of 32-bit float PCM at `SAMPLE_RATE`."""
        if self._ws is None:
            raise RuntimeError("no open session")
        await self._ws.send_bytes(pcm)

    async def finish(self) -> None:
        """Tell the server no more audio is coming, so it flushes what it has."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str(DONE)

    async def readings(self) -> AsyncIterator[Partial]:
        """Every reading the server sends, in order, until it closes.

        A reading is marked final when the segment number moves past it: the
        server reports an endpoint by starting to number the next one.
        """
        if self._ws is None:
            raise RuntimeError("no open session")
        latest: Partial | None = None
        async for message in self._ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except (ValueError, TypeError):
                logger.warning("[stt] unreadable reply from %s: %r", self._uri, message.data)
                continue
            if not isinstance(payload, dict):
                # One malformed reply is not a reason to end a turn that is
                # otherwise going fine.
                logger.warning("[stt] unexpected reply shape from %s: %r", self._uri, payload)
                continue
            segment = int(payload.get("segment", 0))
            text = str(payload.get("text", ""))
            # Two servers ship with sherpa-onnx and they answer differently: one
            # says `is_final` outright, the other reports an endpoint only by
            # starting to number the next segment. Take the flag where it is
            # given and infer it where it is not, so either server drives this.
            stated_final = bool(payload.get("is_final", False))
            if latest is not None and segment != latest.segment:
                yield Partial(text=latest.text, segment=latest.segment, final=True)
            latest = Partial(text=text, segment=segment, final=stated_final)
            yield latest
            if stated_final:
                latest = None
        if latest is not None and latest.text:
            yield Partial(text=latest.text, segment=latest.segment, final=True)


async def transcribe_stream(
    uri: str,
    audio: AsyncIterator[bytes],
    on_reading: Callable[[Partial], None],
) -> None:
    """Drive one session: feed it audio, hand every reading to `on_reading`."""
    async with StreamingSession(uri) as session:

        async def pump() -> None:
            async for frame in audio:
                await session.feed(frame)
            await session.finish()

        pumping = asyncio.create_task(pump())
        try:
            async for reading in session.readings():
                on_reading(reading)
        finally:
            pumping.cancel()
            (outcome,) = await asyncio.gather(pumping, return_exceptions=True)
        # Raised, not discarded: a server that closes while audio is still going
        # up ends the readings too, and swallowing this would make that
        # indistinguishable from a turn that simply finished.
        if isinstance(outcome, Exception):
            raise outcome


class LiveTranscription:
    """A session fed one frame at a time, with readings collected for a caller.

    `transcribe_stream` wants audio as an iterator, but a websocket delivers
    frames as they land. This turns one into the other: audio goes in through
    `feed`, readings come out through `readings`, and the session runs between
    them.
    """

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=MAX_PENDING_FRAMES)
        self._out: asyncio.Queue[Partial | BaseException | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LiveTranscription:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_: object) -> bool:
        await self.close()
        return False

    def _offer(self, item: bytes | None) -> None:
        """Queue one item, making room by dropping the oldest if it is full.

        Never waits, and that matters for both callers. Whoever is capturing
        cannot be made to pause -- a microphone keeps producing whether or not
        the recogniser is keeping up -- and the end-of-audio marker travels the
        same path, so a caller that says "stop" is not left holding a backlog
        that has nowhere to go.
        """
        try:
            self._frames.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._frames.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - raced to empty
            pass
        logger.warning("[stt] recogniser is behind; dropped a frame of audio")
        self._frames.put_nowait(item)

    async def feed(self, pcm: bytes) -> None:
        """Offer one frame of audio to the recogniser."""
        self._offer(pcm)

    async def finish(self) -> None:
        """Say that no more audio is coming; readings continue until it settles."""
        self._offer(None)

    async def close(self) -> None:
        """Abandon the session, whether or not it finished."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def readings(self) -> AsyncIterator[Partial]:
        """Every reading, in order, until the session ends or fails.

        A failure is re-raised here rather than logged and swallowed: the caller
        is the one that has to tell someone recognition stopped working.
        """
        while True:
            item = await self._out.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _audio(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def _run(self) -> None:
        try:
            await transcribe_stream(self._uri, self._audio(), self._out.put_nowait)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._out.put_nowait(exc)
        else:
            self._out.put_nowait(None)
