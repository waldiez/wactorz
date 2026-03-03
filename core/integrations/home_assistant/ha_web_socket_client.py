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
