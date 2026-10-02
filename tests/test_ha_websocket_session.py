"""A Home Assistant WebSocket session against a real server that speaks the protocol.

The server here authenticates with a token, answers commands by id, interleaves
events for other subscriptions, and refuses what HA would refuse. The client
must authenticate before anything else, match each answer to the request that
asked for it rather than to whatever frame arrives next, and turn a refusal into
an error that names it. `tests/test_ha_ws_timeouts.py` covers the deadlines.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets

from wactorz.core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient

TOKEN = "good-token"  # a test fixture, not a credential
STATES = [{"entity_id": "light.hall", "state": "on"}, {"entity_id": "sensor.t", "state": "21"}]


class _HomeAssistant:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.hello: dict[str, Any] = {"type": "auth_required"}

    async def handler(self, ws: Any) -> None:
        await ws.send(json.dumps(self.hello))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            # Something else's traffic first, so matching by id is exercised.
            await ws.send(json.dumps({"id": 999, "type": "event", "event": {}}))
            if msg["type"] == "get_states":
                await ws.send(
                    json.dumps(
                        {"id": msg["id"], "type": "result", "success": True, "result": STATES}
                    )
                )
            elif msg["type"] == "call_service":
                ok = msg["domain"] != "nope"
                await ws.send(
                    json.dumps({"id": msg["id"], "type": "result", "success": ok, "result": None})
                )
            elif msg["type"] == "subscribe_events":
                ok = msg.get("event_type") != "forbidden"
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": ok}))
                if ok:
                    await ws.send(json.dumps({"id": msg["id"] + 100, "type": "event"}))
                    await ws.send(
                        json.dumps(
                            {
                                "id": msg["id"],
                                "type": "event",
                                "event": {"data": {"entity_id": "light.hall"}},
                            }
                        )
                    )
                    await ws.send(json.dumps(["not", "a", "dict"]))


@pytest.fixture(name="ha")
async def ha_fixture() -> AsyncIterator[tuple[_HomeAssistant, str]]:
    ha = _HomeAssistant()
    async with websockets.serve(ha.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield ha, f"ws://127.0.0.1:{port}/api/websocket"


class TestSession:
    async def test_states_are_fetched_and_one_entity_found(
        self, ha: tuple[_HomeAssistant, str]
    ) -> None:
        _, url = ha
        async with HAWebSocketClient(url, TOKEN) as client:
            assert await client.get_entity_state("sensor.t") == {
                "entity_id": "sensor.t",
                "state": "21",
            }
            assert await client.get_entity_state("sensor.missing") is None

    async def test_a_service_call_carries_the_entity_and_data(
        self, ha: tuple[_HomeAssistant, str]
    ) -> None:
        server, url = ha
        async with HAWebSocketClient(url, TOKEN) as client:
            await client.call_service("light", "turn_on", "light.hall", brightness=40)

            with pytest.raises(RuntimeError, match="WS call failed"):
                await client.call_service("nope", "x", "light.hall")

        assert server.received[0] == {
            "id": 1,
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.hall", "brightness": 40},
        }

    async def test_a_subscription_receives_only_its_own_events(
        self, ha: tuple[_HomeAssistant, str]
    ) -> None:
        _, url = ha
        async with HAWebSocketClient(url, TOKEN) as client:
            subscription = await client.subscribe_events("state_changed")
            event = await client.receive_event(subscription)

            assert event["event"]["data"]["entity_id"] == "light.hall"
            with pytest.raises(TypeError, match="Unexpected websocket payload"):
                await client.receive_json(timeout=5)

    async def test_a_refused_subscription_is_an_error(self, ha: tuple[_HomeAssistant, str]) -> None:
        _, url = ha
        async with HAWebSocketClient(url, TOKEN) as client:
            with pytest.raises(RuntimeError, match="WS subscribe failed"):
                await client.subscribe_events("forbidden")

    async def test_a_wrong_token_is_refused_at_connect(
        self, ha: tuple[_HomeAssistant, str]
    ) -> None:
        _, url = ha

        with pytest.raises(RuntimeError, match="Auth failed"):
            async with HAWebSocketClient(url, "wrong"):
                pass

    async def test_a_server_that_is_not_home_assistant_is_refused(
        self, ha: tuple[_HomeAssistant, str]
    ) -> None:
        server, url = ha
        server.hello = {"type": "welcome"}

        with pytest.raises(RuntimeError, match="Unexpected hello"):
            async with HAWebSocketClient(url, TOKEN):
                pass


class TestWithoutAConnection:
    async def test_every_request_needs_a_connection(self) -> None:
        client = HAWebSocketClient("ws://unused", TOKEN)

        with pytest.raises(RuntimeError, match="No WS client"):
            await client.call("get_states")
        with pytest.raises(RuntimeError, match="No WS client"):
            await client.subscribe_events()
        with pytest.raises(RuntimeError, match="No WS client"):
            await client._authenticate()
        with pytest.raises(RuntimeError, match="No WS client"):
            await client.receive_json()

    async def test_leaving_without_connecting_is_harmless(self) -> None:
        await HAWebSocketClient("ws://unused", TOKEN).__aexit__(None, None, None)
