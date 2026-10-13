import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable
from typing import Any, TypeVar

import websockets

from ....monitoring import ha_metrics

T = TypeVar("T")

# How long a request/response exchange may take before it is treated as failed.
#
# `connect()` sets ping_interval/ping_timeout, which closes a socket that has
# gone away — but not one whose peer answers pings and never replies. Without a
# deadline that case blocks the caller for good.
#
# This bounds commands only. Waiting indefinitely for the *next event* is what a
# subscription is for, so `receive_event` is deliberately left unbounded.
#
# Deliberately generous. The point is to be finite, not tight: the slow-but-
# healthy case is real — a full area/device/entity registry dump plus
# `get_states` on a large installation running on modest hardware — and failing
# one of those would be a new bug in place of the old one. Callers that hold a
# connection open retry on a timeout, so overshooting costs a little latency
# while undershooting costs correctness.
_RESPONSE_TIMEOUT = 60


async def _measured(command: str, request: Awaitable[T]) -> T:
    """Await one request to Home Assistant, counting and timing it by ``command``.

    A request cancelled by its caller is neither counted nor timed: it is the
    caller giving up, not Home Assistant.
    """
    started = time.monotonic()
    outcome = ha_metrics.ERROR
    try:
        result = await request
    except (TimeoutError, asyncio.TimeoutError):
        # Both: on Python 3.10 a frame that never comes raises asyncio's own
        # TimeoutError, and a deadline run out raises the builtin one.
        outcome = ha_metrics.TIMEOUT
        raise
    except asyncio.CancelledError:
        outcome = ""
        raise
    else:
        outcome = ha_metrics.OK
        return result
    finally:
        if outcome:
            ha_metrics.record_request(command, outcome, time.monotonic() - started)


class HAWebSocketClient:
    def __init__(self, ws_url: str, token: str):
        self.ws_url = ws_url
        self.token = token
        self._ws = None
        self._msg_id = 0

    async def __aenter__(self):
        started = time.monotonic()
        try:
            self._ws = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20)
            await self._authenticate()
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                ha_metrics.CONNECT_FAILURES.inc()
            if self._ws is not None:
                # A socket that opened but did not authenticate; closing it must
                # not hide why.
                with contextlib.suppress(Exception):
                    await self._ws.close()
            raise
        ha_metrics.CONNECT_DURATION.observe(time.monotonic() - started)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._ws:
            await self._ws.close()

    async def _recv(self, timeout: float | None) -> Any:
        """Receive one frame, optionally bounded. `None` waits indefinitely."""
        if self._ws is None:
            raise RuntimeError("No WS client")
        raw = self._ws.recv()
        if timeout is not None:
            raw = asyncio.wait_for(raw, timeout=timeout)
        return json.loads(await raw)

    async def _authenticate(self):
        if self._ws is None:
            raise RuntimeError("No WS client")
        hello = await self._recv(_RESPONSE_TIMEOUT)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected hello: {hello}")

        await self._ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        resp = await self._recv(_RESPONSE_TIMEOUT)
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {resp}")

    async def call(self, ws_type: str, **kwargs) -> Any:
        """Call a Home Assistant WebSocket command and return result payload.

        Counted and timed by command in `ha_metrics`; see `_measured`.
        """
        return await _measured(ws_type, self._call(ws_type, **kwargs))

    async def _call(self, ws_type: str, **kwargs) -> Any:
        """The exchange itself: send the command, read until its answer arrives."""
        if self._ws is None:
            raise RuntimeError("No WS client")
        self._msg_id += 1
        msg_id = self._msg_id

        payload = {"id": msg_id, "type": ws_type}
        payload.update(kwargs)
        await self._ws.send(json.dumps(payload))

        # One deadline for the whole exchange rather than per frame: this loop
        # skips messages belonging to other requests, so a busy connection would
        # otherwise keep resetting the clock and the call would never end.
        deadline = time.monotonic() + _RESPONSE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Home Assistant did not answer {ws_type!r} within {_RESPONSE_TIMEOUT}s"
                )
            resp = await self._recv(remaining)
            if resp.get("id") == msg_id:
                if not resp.get("success"):
                    raise RuntimeError(f"WS call failed: {resp}")
                return resp.get("result")

    async def receive_json(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive the next raw JSON message from Home Assistant.

        Unbounded by default, because the caller waiting for the next event
        should wait as long as it takes. Command paths pass their remaining
        deadline.
        """
        payload = await self._recv(timeout)
        if not isinstance(payload, dict):
            raise TypeError(f"Unexpected websocket payload: {payload!r}")
        return payload

    async def subscribe_events(self, event_type: str | None = None) -> int:
        """Subscribe to Home Assistant events and return the subscription id.

        Counted and timed as a ``subscribe_events`` request, like every command;
        the events that follow are a subscription's to wait for, and are not.
        """
        return await _measured("subscribe_events", self._subscribe(event_type))

    async def _subscribe(self, event_type: str | None) -> int:
        """The exchange itself: ask for the events, read until it is confirmed."""
        if self._ws is None:
            raise RuntimeError("No WS client")
        self._msg_id += 1
        msg_id = self._msg_id

        payload = {"id": msg_id, "type": "subscribe_events"}
        if event_type:
            payload["event_type"] = event_type
        await self._ws.send(json.dumps(payload))

        deadline = time.monotonic() + _RESPONSE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Home Assistant did not confirm the subscription within {_RESPONSE_TIMEOUT}s"
                )
            resp = await self.receive_json(remaining)
            if resp.get("id") != msg_id:
                continue
            if not resp.get("success"):
                raise RuntimeError(f"WS subscribe failed: {resp}")
            return msg_id

    async def receive_event(self, subscription_id: int) -> dict[str, Any]:
        """Wait for the next event message for a specific subscription."""
        while True:
            resp = await self.receive_json()
            if resp.get("type") == "event" and resp.get("id") == subscription_id:
                return resp

    async def call_service(
        self, domain: str, service: str, entity_id: str, **service_data: Any
    ) -> Any:
        """Call a Home Assistant service for an entity."""
        return await self.call(
            "call_service",
            domain=domain,
            service=service,
            service_data={"entity_id": entity_id, **service_data},
        )

    async def get_entity_state(self, entity_id: str) -> dict[str, Any] | None:
        """Return the current state object for a single entity, or None if not found."""
        states = await self.call("get_states")
        for state in states or []:
            if state.get("entity_id") == entity_id:
                return state
        return None
