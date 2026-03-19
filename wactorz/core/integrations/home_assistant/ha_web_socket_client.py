import websockets
import json
from typing import Any


class HAWebSocketClient:
    def __init__(self, ws_url: str, token: str):
        self.ws_url = ws_url
        self.token = token
        self._ws = None
        self._msg_id = 0

    async def __aenter__(self):
        self._ws = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20)
        await self._authenticate()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._ws:
            await self._ws.close()

    async def _authenticate(self):
        assert self._ws is not None
        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected hello: {hello}")

        await self._ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        resp = json.loads(await self._ws.recv())
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {resp}")

    async def call(self, ws_type: str, **kwargs) -> Any:
        """Call a Home Assistant WebSocket command and return result payload."""
        assert self._ws is not None
        self._msg_id += 1
        msg_id = self._msg_id

        payload = {"id": msg_id, "type": ws_type}
        payload.update(kwargs)
        await self._ws.send(json.dumps(payload))

        while True:
            resp = json.loads(await self._ws.recv())
            if resp.get("id") == msg_id:
                if not resp.get("success"):
                    raise RuntimeError(f"WS call failed: {resp}")
                return resp.get("result")

    async def receive_json(self) -> dict[str, Any]:
        """Receive the next raw JSON message from Home Assistant."""
        assert self._ws is not None
        payload = json.loads(await self._ws.recv())
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected websocket payload: {payload!r}")
        return payload

    async def subscribe_events(self, event_type: str | None = None) -> int:
        """Subscribe to Home Assistant events and return the subscription id."""
        assert self._ws is not None
        self._msg_id += 1
        msg_id = self._msg_id

        payload = {"id": msg_id, "type": "subscribe_events"}
        if event_type:
            payload["event_type"] = event_type
        await self._ws.send(json.dumps(payload))

        while True:
            resp = await self.receive_json()
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
