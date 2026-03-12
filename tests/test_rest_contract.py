import types
import unittest

from interfaces.chat_interfaces import RESTInterface


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
