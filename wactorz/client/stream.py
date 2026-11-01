"""The one socket of a client, kept connected and shared by everyone listening.

The stream is live state with no replay. A listener is told when the socket went
and came back, so it can reread what may have changed, and one that falls behind
loses its oldest events and is told how many.
"""

import asyncio
import json
import logging
import random
from collections import deque
from collections.abc import Callable
from typing import Any

import aiohttp

from .errors import ClientError, UnavailableError
from .frames import Disconnected, Dropped, Event, Frame, Reconnected
from .transport import Transport

logger = logging.getLogger(__name__)

#: Seconds before the first attempt to reconnect, and the most it grows to.
RECONNECT_FIRST_S = 1.0
RECONNECT_MOST_S = 30.0
#: Events a listener may hold before it starts to lose the oldest.
LISTENER_LIMIT = 1024


def _next_delay(delay: float) -> float:
    return min(delay * 2, RECONNECT_MOST_S)


def _jittered(delay: float) -> float:
    """``delay``, spread out so clients of one restarted server do not return as one."""
    return delay * random.uniform(0.75, 1.25)  # noqa: S311


def _parsed(text: str) -> Frame | None:
    """The frame in ``text``, or None when it is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    return Frame(kind if isinstance(kind, str) else "", data)


class Listener:
    """One consumer's events. ``limit`` None keeps everything: for a bounded turn."""

    def __init__(
        self, limit: int | None = LISTENER_LIMIT, accepts: Callable[[Event], bool] | None = None
    ) -> None:
        self._events: deque[Event] = deque()
        self._limit = limit
        self._accepts = accepts
        self._dropped = 0
        self._changed = asyncio.Event()
        self._closed = False
        self._failure: ClientError | None = None

    def put(self, event: Event) -> None:
        """Keep ``event`` if this listener wants it, losing the oldest when full."""
        if self._accepts is not None and not self._accepts(event):
            return
        if self._limit is not None and len(self._events) >= self._limit:
            self._events.popleft()
            self._dropped += 1
        self._events.append(event)
        self._changed.set()

    def end(self, failure: ClientError | None = None) -> None:
        """No more events are coming. ``failure`` is why, when it is not a plain close."""
        self._closed = True
        self._failure = failure
        self._changed.set()

    async def get(self) -> Event | None:
        """The next event, or None once the stream has closed."""
        while True:
            if self._dropped:
                count, self._dropped = self._dropped, 0
                return Dropped(count)
            if self._events:
                return self._events.popleft()
            if self._closed:
                if self._failure is not None:
                    raise self._failure
                return None
            self._changed.clear()
            await self._changed.wait()


class Stream:
    """``/ws`` for one client. Connects when first needed; reconnects until closed."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._listeners: set[Listener] = set()
        self._socket: aiohttp.ClientWebSocketResponse[bool] | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._failure: ClientError | None = None
        #: The newest full state, so a listener who joins late starts from it.
        self._snapshot: Frame | None = None

    def listen(self, listener: Listener) -> Listener:
        """Hand ``listener`` everything from now on, the current state first."""
        if self._failure is not None:
            listener.end(self._failure)
            return listener
        self._start()
        if self._snapshot is not None:
            listener.put(self._snapshot)
        self._listeners.add(listener)
        return listener

    def forget(self, listener: Listener) -> None:
        """Stop handing ``listener`` events."""
        self._listeners.discard(listener)

    async def send(self, frame: dict[str, Any], *, timeout: float) -> None:
        """Send ``frame``, waiting up to ``timeout`` for the socket to be up."""
        self._start()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise self._failure or UnavailableError(self._transport.url, "no socket") from exc
        if self._failure is not None:
            raise self._failure
        socket = self._socket
        if socket is None:
            raise UnavailableError(self._transport.url, "the socket has just gone")
        try:
            await socket.send_json(frame)
        except (ConnectionError, aiohttp.ClientError) as exc:
            raise UnavailableError(self._transport.url, exc) from exc

    async def close(self) -> None:
        """Close the socket and end every listener."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._drop_socket()
        for listener in self._listeners:
            listener.end()
        self._listeners.clear()

    def _start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def _publish(self, event: Event) -> None:
        for listener in list(self._listeners):
            listener.put(event)

    async def _drop_socket(self) -> None:
        self._connected.clear()
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()

    def _give_up(self, failure: ClientError) -> None:
        """Stop for good: a refused key does not get better by trying again."""
        logger.error("[client] Giving up on the socket: %s", failure)
        self._failure = failure
        for listener in self._listeners:
            listener.end(failure)
        self._listeners.clear()
        # Wakes anyone in `send`, who then finds the failure.
        self._connected.set()

    async def _run(self) -> None:
        delay = RECONNECT_FIRST_S
        ever_connected = False
        while True:
            try:
                self._socket = await self._transport.connect_websocket()
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                logger.debug("[client] Could not connect: %s", exc)
            except ClientError as exc:
                if not isinstance(exc, UnavailableError):
                    self._give_up(exc)
                    return
                logger.debug("[client] Could not connect: %s", exc)
            else:
                delay = RECONNECT_FIRST_S
                self._connected.set()
                if ever_connected:
                    self._publish(Reconnected())
                ever_connected = True
                await self._read(self._socket)
                await self._drop_socket()
                self._publish(Disconnected())
            await asyncio.sleep(_jittered(delay))
            delay = _next_delay(delay)

    async def _read(self, socket: aiohttp.ClientWebSocketResponse[bool]) -> None:
        async for message in socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = _parsed(message.data)
            if frame is None:
                continue
            if frame.type == "full_snapshot":
                self._snapshot = frame
            self._publish(frame)
