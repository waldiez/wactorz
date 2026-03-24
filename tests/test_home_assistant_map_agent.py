import base64
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
sys.modules.setdefault("websockets", types.ModuleType("websockets"))

from wactorz.agents.home_assistant_map_agent import HomeAssistantMapAgent, MapUpdateDispatcher
from wactorz.core.actor import Message, MessageType


class HomeAssistantMapAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_start_refreshes_map_and_persists_latest_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = HomeAssistantMapAgent(
                name="home-assistant-map-agent-test",
                persistence_dir=tmpdir,
            )
            agent.ha_url = "http://ha.local:8123"
            agent.ha_ws_url = "ws://ha.local:8123/api/websocket"
            agent.ha_token = "token"

            payload = {
                "type": "home_assistant_map_update",
                "event_type": "entity_registry_updated",
                "timestamp": 123.0,
                "event": {},
                "devices": [],
            }
            agent._build_map_update_payload = AsyncMock(return_value=payload)
            agent._dispatcher.dispatch = AsyncMock()
            agent._mqtt_publish = AsyncMock(return_value=True)
            agent._entity_registry_listener = AsyncMock(return_value=None)

            await agent.on_start()

            agent._build_map_update_payload.assert_awaited_once_with(
                event=None,
                include_states=True,
            )
            agent._dispatcher.dispatch.assert_not_awaited()
            self.assertEqual(agent.get_latest_map_payload(), payload)
            self.assertEqual(agent.recall("latest_map_payload"), payload)
            self.assertEqual(agent.metrics.tasks_completed, 1)

    async def test_refresh_dispatches_map_update_and_replies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = HomeAssistantMapAgent(
                name="home-assistant-map-agent-test",
                persistence_dir=tmpdir,
            )

            payload = {
                "type": "home_assistant_map_update",
                "event_type": "entity_registry_updated",
                "timestamp": 123.0,
                "event": {},
                "devices": [],
            }
            agent._build_map_update_payload = AsyncMock(return_value=payload)
            agent._dispatcher.dispatch = AsyncMock()
            agent.send = AsyncMock(return_value=True)

            msg = Message(
                type=MessageType.TASK,
                sender_id="sender-1",
                payload={"text": "refresh"},
            )

            await agent.handle_message(msg)

            agent._build_map_update_payload.assert_awaited_once_with(
                event=None,
                include_states=True,
            )
            agent._dispatcher.dispatch.assert_awaited_once_with(payload)
            agent.send.assert_awaited_once_with("sender-1", MessageType.RESULT, payload)
            self.assertEqual(agent.get_latest_map_payload(), payload)
            self.assertEqual(agent.recall("latest_map_payload"), payload)
            self.assertEqual(agent.metrics.tasks_completed, 1)

    async def test_refresh_simple_dispatches_map_update_without_states(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = HomeAssistantMapAgent(
                name="home-assistant-map-agent-test",
                persistence_dir=tmpdir,
            )

            payload = {
                "type": "home_assistant_map_update",
                "event_type": "entity_registry_updated",
                "timestamp": 123.0,
                "event": {},
                "devices": [],
            }
            agent._build_map_update_payload = AsyncMock(return_value=payload)
            agent._dispatcher.dispatch = AsyncMock()
            agent.send = AsyncMock(return_value=True)

            msg = Message(
                type=MessageType.TASK,
                sender_id="sender-1",
                payload={"text": "refresh simple"},
            )

            await agent.handle_message(msg)

            agent._build_map_update_payload.assert_awaited_once_with(
                event=None,
                include_states=False,
            )
            agent._dispatcher.dispatch.assert_awaited_once_with(payload)
            agent.send.assert_awaited_once_with("sender-1", MessageType.RESULT, payload)
            self.assertEqual(agent.metrics.tasks_completed, 1)

    async def test_dispatcher_chunks_oversized_payloads_on_same_topic(self):
        fake_agent = types.SimpleNamespace(
            name="home-assistant-map-agent-test",
            _registry=None,
            _mqtt_publish=AsyncMock(),
        )
        dispatcher = MapUpdateDispatcher(
            agent=fake_agent,
            mqtt_topic="homeassistant/map/entities_with_location",
            max_payload_bytes=450,
        )
        payload = {
            "type": "home_assistant_map_update",
            "event_type": "entity_registry_updated",
            "timestamp": 123.0,
            "event": {"data": {"action": "refresh"}},
            "devices": [
                {
                    "device_id": "oversized-device",
                    "name": "A" * 500,
                    "entities": [
                        {
                            "entity_id": "sensor.a",
                            "attributes": {"blob": "X" * 1200},
                        }
                    ],
                }
            ],
        }

        await dispatcher.dispatch(payload)

        calls = fake_agent._mqtt_publish.await_args_list
        self.assertGreaterEqual(len(calls), 2)

        first_topic, first_payload = calls[0].args[:2]
        self.assertEqual(first_topic, "homeassistant/map/entities_with_location")
        self.assertEqual(first_payload["type"], "home_assistant_map_update_chunked")
        self.assertEqual(first_payload["total_devices"], 1)

        chunk_payloads = [call.args[1] for call in calls[1:]]
        self.assertTrue(chunk_payloads)
        self.assertTrue(all(item["type"] == "home_assistant_map_update_chunk" for item in chunk_payloads))
        self.assertTrue(all(dispatcher._payload_size(item) <= dispatcher._max_payload_bytes for item in [first_payload, *chunk_payloads]))

        encoded = "".join(chunk["data"] for chunk in chunk_payloads)
        rebuilt_payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(rebuilt_payload, payload)

    def test_chunk_builder_respects_4kb_limit_with_double_digit_chunk_indexes(self):
        fake_agent = types.SimpleNamespace(
            name="home-assistant-map-agent-test",
            _registry=None,
            _mqtt_publish=AsyncMock(),
        )
        dispatcher = MapUpdateDispatcher(
            agent=fake_agent,
            mqtt_topic="homeassistant/map/entities_with_location",
            max_payload_bytes=4096,
        )
        payload = {
            "type": "home_assistant_map_update",
            "event_type": "entity_registry_updated",
            "timestamp": 123.0,
            "event": {"data": {"action": "refresh"}},
            "devices": [
                {
                    "device_id": "oversized-device",
                    "name": "A" * 500,
                    "entities": [
                        {
                            "entity_id": "sensor.a",
                            "attributes": {"blob": "X" * 50000},
                        }
                    ],
                }
            ],
        }

        manifest, chunks = dispatcher._build_chunked_payloads(payload)

        self.assertGreaterEqual(len(chunks), 11)
        self.assertLessEqual(dispatcher._payload_size(manifest), dispatcher._max_payload_bytes)
        self.assertTrue(all(dispatcher._payload_size(chunk) <= dispatcher._max_payload_bytes for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
