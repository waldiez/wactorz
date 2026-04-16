import sys
import types
import unittest

from prometheus_client import CONTENT_TYPE_LATEST


def _install_aiohttp_web_stub() -> None:
    class _Response:
        def __init__(self, *, body=b"", headers=None, content_type=None, status=200):
            self.body = body
            self.status = status
            self.headers = dict(headers or {})
            if content_type is not None:
                self.headers.setdefault("Content-Type", content_type)

    web = types.SimpleNamespace(
        Request=type("Request", (), {}),
        HTTPException=type("HTTPException", (Exception,), {"status": 500}),
        Response=_Response,
        middleware=lambda fn: fn,
    )
    sys.modules["aiohttp"] = types.SimpleNamespace(web=web)


_install_aiohttp_web_stub()

from wactorz.interfaces.chat_interfaces import RESTInterface


class _FakeMetrics:
    messages_processed = 7
    errors = 2
    last_heartbeat = 123
    restart_count = 1


class _FakeActor:
    actor_id = "actor-123"
    name = "main"
    protected = True
    metrics = _FakeMetrics()

    def get_status(self):
        return {"state": "idle"}


class _FakeMapActor:
    def __init__(self, payload):
        self._payload = payload

    def get_latest_map_payload(self):
        return self._payload


class _FakeRegistry:
    def __init__(self, map_actor=None):
        self._map_actor = map_actor

    def find_by_name(self, name):
        if name == "home-assistant-map-agent":
            return self._map_actor
        return None


class RestContractTest(unittest.TestCase):
    def test_actor_payload_matches_frontend_contract(self):
        iface = RESTInterface(main_actor=types.SimpleNamespace(), port=8080)
        payload = iface._actor_payload(_FakeActor())
        self.assertEqual(
            payload,
            {
                "id": "actor-123",
                "name": "main",
                "state": "initializing",
                "protected": True,
            },
        )

    def test_metrics_payload_uses_rust_style_keys(self):
        iface = RESTInterface(main_actor=types.SimpleNamespace(), port=8080)
        payload = iface._metrics_payload(_FakeActor())
        self.assertEqual(payload["messages_processed"], 7)
        self.assertEqual(payload["messages_failed"], 2)
        self.assertEqual(payload["restart_count"], 1)
        self.assertIn("llm_cost_usd", payload)

    def test_latest_ha_map_payload_reads_from_running_map_agent(self):
        expected = {"type": "home_assistant_map_update", "devices": [{"device_id": "one"}]}
        registry = _FakeRegistry(map_actor=_FakeMapActor(expected))
        iface = RESTInterface(main_actor=types.SimpleNamespace(_registry=registry), port=8080)
        self.assertEqual(iface._latest_ha_map_payload(), expected)

    def test_prometheus_monitor_returns_prometheus_content_type(self):
        iface = RESTInterface(main_actor=types.SimpleNamespace(), port=8080)
        response = iface._monitor.metrics_response()
        self.assertEqual(response.headers["Content-Type"], CONTENT_TYPE_LATEST)
