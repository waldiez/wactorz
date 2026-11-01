"""One session with a server: its address, its key, timeouts, and what a refusal means."""

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .errors import (
    ClientError,
    NotFoundError,
    NotOfferedError,
    RefusedError,
    RejectedError,
    TimedOutError,
    UnauthorizedError,
    UnavailableError,
)

logger = logging.getLogger(__name__)

#: Seconds a request may take. Generous: the bug prevented is a wait with no end,
#: and a call that is slow when healthy passes its own.
DEFAULT_TIMEOUT_S = 60.0
#: Seconds between pings on the socket, which is how a dead link is noticed.
HEARTBEAT_S = 30.0

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _exposed(url: str) -> bool:
    """Whether a key sent to ``url`` would cross a network unencrypted."""
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname not in _LOOPBACK


def _said(body: Any, fallback: str) -> str:
    """The server's own words from an error body, or ``fallback``."""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return fallback


class Transport:
    """Requests to one server. Nothing connects until `open`."""

    def __init__(self, url: str, api_key: str = "", *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.url = url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:
        return f"Transport({self.url!r})"

    async def open(self) -> None:
        """Start the session. Says once if the key would travel unencrypted."""
        if self._session is not None:
            return
        if self._api_key and _exposed(self.url):
            logger.warning(
                "[client] Sending the key to %s over plain http; anyone on the path can "
                "read it. Put TLS in front of that server.",
                self.url,
            )
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        self._session = aiohttp.ClientSession(headers=headers)

    async def close(self) -> None:
        """End the session."""
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def _in_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise UnavailableError(self.url, "the client is not open")
        return self._session

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> Any:
        """Call a route and return its JSON body, or raise what its refusal means."""
        return await self._call(method, path, params, json, data, timeout, _read_json)

    async def request_bytes(self, path: str, *, timeout: float | None = None) -> bytes:
        """``GET`` a route and return its body as it is."""
        return await self._call("GET", path, None, None, None, timeout, _read_bytes)

    async def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: Any,
        data: Any,
        timeout: float | None,
        read: Any,
    ) -> Any:
        limit = self.timeout if timeout is None else timeout
        try:
            async with self._in_session().request(
                method,
                f"{self.url}{path}",
                params=params,
                json=json,
                data=data,
                timeout=aiohttp.ClientTimeout(total=limit),
            ) as response:
                return await read(self, response, path)
        except asyncio.TimeoutError as exc:
            if isinstance(exc, ClientError):
                raise
            raise TimedOutError(method, path, limit) from exc
        except aiohttp.ClientError as exc:
            raise UnavailableError(self.url, exc) from exc

    def refusal(self, status: int, path: str, body: Any) -> ClientError:
        """The exception for a response of ``status``."""
        said = _said(body, f"status {status}")
        if status == 401:
            return UnauthorizedError(self.url)
        if status == 403:
            return RefusedError(self.url, said)
        if status == 404:
            return NotFoundError(path, said) if isinstance(body, dict) else NotOfferedError(path)
        if status >= 500:
            return UnavailableError(self.url, said)
        return RejectedError(status, said)

    def websocket_url(self) -> str:
        """The address of this server's ``/ws``."""
        parts = urlsplit(self.url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((scheme, parts.netloc, f"{parts.path}/ws", "", ""))

    async def connect_websocket(self) -> aiohttp.ClientWebSocketResponse[bool]:
        """Open ``/ws``. A refused handshake raises what the status means."""
        try:
            return await self._in_session().ws_connect(
                self.websocket_url(),
                heartbeat=HEARTBEAT_S,
                max_msg_size=0,
            )
        except aiohttp.WSServerHandshakeError as exc:
            raise self.refusal(exc.status, "/ws", None) from exc


async def _body_of(response: aiohttp.ClientResponse) -> Any:
    """``response``'s JSON, or None when it is not JSON at all."""
    if response.content_type != "application/json":
        return None
    try:
        return await response.json()
    except ValueError:
        return None


async def _read_json(transport: Transport, response: aiohttp.ClientResponse, path: str) -> Any:
    body = await _body_of(response)
    if response.status >= 400:
        raise transport.refusal(response.status, path, body)
    if response.content_type != "application/json":
        # The dashboard's catch-all answers any GET it does not know with the page.
        raise NotOfferedError(path)
    return body


async def _read_bytes(transport: Transport, response: aiohttp.ClientResponse, path: str) -> bytes:
    if response.status >= 400:
        raise transport.refusal(response.status, path, await _body_of(response))
    return await response.read()
