import json
import unittest
from unittest.mock import AsyncMock

from wactorz.agents.home_assistant_agent import HomeAssistantAgent
from wactorz.agents.llm_agent import LLMProvider


class _QueuedLLM(LLMProvider):
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.system_prompts: list[str] = []

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        self.system_prompts.append(system)
        if not self._responses:
            raise AssertionError("No queued LLM response available")
        return self._responses.pop(0), {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _devices_fixture() -> dict:
    return {
        "connected": True,
        "reason": "",
        "domains": {"binary_sensor", "light"},
        "devices": [
            {
                "device_id": "motion-1",
                "name": "Living Room Motion Sensor",
                "manufacturer": "Acme",
                "model": "Motion 1",
                "area": "Living Room",
                "entities": [
                    {
                        "entity_id": "binary_sensor.living_room_motion",
                        "unique_id": "motion-entity-1",
                        "platform": "mqtt",
                        "area": "Living Room",
                        "original_name": "Living Room Motion",
                        "name": "Living Room Motion",
                    }
                ],
            },
            {
                "device_id": "presence-1",
                "name": "Living Room Presence Sensor",
                "manufacturer": "Acme",
                "model": "Presence 1",
                "area": "Living Room",
                "entities": [
                    {
                        "entity_id": "binary_sensor.living_room_presence",
                        "unique_id": "presence-entity-1",
                        "platform": "mqtt",
                        "area": "Living Room",
                        "original_name": "Living Room Presence",
                        "name": "Living Room Presence",
                    }
                ],
            },
            {
                "device_id": "light-1",
                "name": "Living Room Lamp",
                "manufacturer": "Acme",
                "model": "Lamp 1",
                "area": "Living Room",
                "entities": [
                    {
                        "entity_id": "light.living_room_lamp",
                        "unique_id": "light-entity-1",
                        "platform": "mqtt",
                        "area": "Living Room",
                        "original_name": "Living Room Lamp",
                        "name": "Living Room Lamp",
                    }
                ],
            },
            {
                "device_id": "light-2",
                "name": "Living Room Ceiling",
                "manufacturer": "Acme",
                "model": "Ceiling 1",
                "area": "Living Room",
                "entities": [
                    {
                        "entity_id": "light.living_room_ceiling",
                        "unique_id": "light-entity-2",
                        "platform": "mqtt",
                        "area": "Living Room",
                        "original_name": "Living Room Ceiling",
                        "name": "Living Room Ceiling",
                    }
                ],
            },
        ],
    }


class HomeAssistantAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_recommend_hardware_returns_primary_and_alternatives(self):
        llm = _QueuedLLM(
            [
                json.dumps(
                    {
                        "can_fulfill": True,
                        "result": "Use the motion sensor and one light; keep the others as alternatives.",
                        "primary_hardware": [
                            {
                                "hardware": "Living Room Motion Sensor",
                                "why": "Fast trigger for entry detection.",
                                "protocol": "MQTT",
                                "required_domains": ["binary_sensor"],
                                "required_entities": ["binary_sensor.living_room_motion"],
                            },
                            {
                                "hardware": "Living Room Ceiling",
                                "why": "Main room light for the automation action.",
                                "protocol": "MQTT",
                                "required_domains": ["light"],
                                "required_entities": ["light.living_room_ceiling"],
                            },
                        ],
                        "alternatives": [
                            {
                                "hardware": "Living Room Presence Sensor",
                                "why": "More stable occupancy signal.",
                                "protocol": "MQTT",
                                "required_domains": ["binary_sensor"],
                                "required_entities": ["binary_sensor.living_room_presence"],
                                "alternative_to": "binary_sensor.living_room_motion",
                            },
                            {
                                "hardware": "Living Room Lamp",
                                "why": "Alternative light target.",
                                "protocol": "MQTT",
                                "required_domains": ["light"],
                                "required_entities": ["light.living_room_lamp"],
                                "alternative_to": "light.living_room_ceiling",
                            },
                        ],
                    }
                )
            ]
        )
        agent = HomeAssistantAgent(llm_provider=llm, name="test-ha-agent")

        result = await agent._recommend_hardware(
            "What existing hardware can automatically turn on the room light when someone enters?",
            _devices_fixture(),
        )

        self.assertTrue(result["can_fulfill"])
        self.assertEqual(result["hardware"], result["primary_hardware"])
        self.assertEqual(len(result["primary_hardware"]), 2)
        self.assertEqual(len(result["alternatives"]), 2)
        self.assertTrue(result["based_on_available_hardware"])
        self.assertIn("Can be done with existing hardware: yes.", result["result"])
        self.assertIn("Alternatives:", result["result"])
        self.assertEqual(result["alternatives"][0]["alternative_to"], "binary_sensor.living_room_motion")
        self.assertEqual(result["alternatives"][1]["alternative_to"], "light.living_room_ceiling")
        self.assertIn("alternative to binary_sensor.living_room_motion", result["result"])

        available_entities = HomeAssistantAgent._available_entity_ids(_devices_fixture())
        for item in result["primary_hardware"] + result["alternatives"]:
            self.assertTrue(set(item["required_entities"]).issubset(available_entities))

    async def test_recommend_hardware_rejects_unavailable_entities(self):
        llm = _QueuedLLM(
            [
                json.dumps(
                    {
                        "can_fulfill": True,
                        "result": "Use the garage motion sensor.",
                        "primary_hardware": [
                            {
                                "hardware": "Garage Motion Sensor",
                                "why": "Detects entry.",
                                "protocol": "MQTT",
                                "required_domains": ["binary_sensor"],
                                "required_entities": ["binary_sensor.garage_motion"],
                            }
                        ],
                        "alternatives": [
                            {
                                "hardware": "Garage Light",
                                "why": "Alternative target light.",
                                "protocol": "MQTT",
                                "required_domains": ["light"],
                                "required_entities": ["light.garage_light"],
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "can_fulfill": False,
                        "result": "The discovered hardware does not include an entry sensor for this room.",
                        "primary_hardware": [],
                        "alternatives": [],
                    }
                ),
            ]
        )
        agent = HomeAssistantAgent(llm_provider=llm, name="test-ha-agent")

        result = await agent._recommend_hardware(
            "What existing hardware can automatically turn on the garage light when someone enters?",
            _devices_fixture(),
        )

        self.assertFalse(result["can_fulfill"])
        self.assertEqual(result["primary_hardware"], [])
        self.assertEqual(result["alternatives"], [])
        self.assertIn("Can be done with existing hardware: no.", result["result"])
        self.assertIn("does not include an entry sensor", result["result"])

    async def test_recommend_hardware_returns_partial_matches_when_not_fulfillable(self):
        llm = _QueuedLLM(
            [
                json.dumps(
                    {
                        "can_fulfill": False,
                        "result": "A motion sensor is available, but there is no compatible siren in the discovered hardware.",
                        "primary_hardware": [
                            {
                                "hardware": "Living Room Motion Sensor",
                                "why": "Can detect entry for part of the request.",
                                "protocol": "MQTT",
                                "required_domains": ["binary_sensor"],
                                "required_entities": ["binary_sensor.living_room_motion"],
                            }
                        ],
                        "alternatives": [
                            {
                                "hardware": "Living Room Presence Sensor",
                                "why": "Alternative trigger input.",
                                "protocol": "MQTT",
                                "required_domains": ["binary_sensor"],
                                "required_entities": ["binary_sensor.living_room_presence"],
                                "alternative_to": "binary_sensor.living_room_motion",
                            }
                        ],
                    }
                )
            ]
        )
        agent = HomeAssistantAgent(llm_provider=llm, name="test-ha-agent")

        result = await agent._recommend_hardware(
            "What existing hardware can detect entry and sound a siren in the living room?",
            _devices_fixture(),
        )

        self.assertFalse(result["can_fulfill"])
        self.assertEqual(len(result["primary_hardware"]), 1)
        self.assertEqual(result["hardware"], result["primary_hardware"])
        self.assertEqual(len(result["alternatives"]), 1)
        self.assertIn("Primary hardware:", result["result"])
        self.assertIn("no compatible siren", result["result"])

    async def test_create_automation_flow_uses_existing_hardware_selection_chain(self):
        agent = HomeAssistantAgent(llm_provider=None, name="test-ha-agent")
        devices = _devices_fixture()
        agent._get_devices = AsyncMock(return_value=devices)
        agent._select_hardware = AsyncMock(
            return_value={
                "can_fulfill": True,
                "hardware": [
                    {
                        "hardware": "Living Room Motion Sensor",
                        "why": "Fast trigger for entry detection.",
                        "protocol": "MQTT",
                        "required_domains": ["binary_sensor"],
                        "required_entities": ["binary_sensor.living_room_motion", "light.living_room_ceiling"],
                    }
                ],
            }
        )
        agent._create_automation = AsyncMock(
            return_value={"can_create": True, "inserted": True, "result": "created"}
        )
        agent._recommend_hardware = AsyncMock(
            return_value={"can_fulfill": True, "result": "should not be used"}
        )

        result = await agent._process(
            "Create an automation to turn on the living room light when someone enters the room."
        )

        self.assertEqual(result["result"], "created")
        agent._select_hardware.assert_awaited_once_with(
            "Create an automation to turn on the living room light when someone enters the room.",
            devices,
        )
        agent._create_automation.assert_awaited_once_with(
            "Create an automation to turn on the living room light when someone enters the room.",
            ["binary_sensor.living_room_motion", "light.living_room_ceiling"],
            agent._select_hardware.return_value["hardware"],
        )
        agent._recommend_hardware.assert_not_called()

    async def test_mixed_hardware_and_create_request_stays_on_create_path(self):
        agent = HomeAssistantAgent(llm_provider=None, name="test-ha-agent")
        devices = _devices_fixture()
        agent._get_devices = AsyncMock(return_value=devices)
        agent._select_hardware = AsyncMock(return_value={"can_fulfill": False, "result": "missing"})
        agent._create_automation = AsyncMock(return_value={"result": "created"})
        agent._recommend_hardware = AsyncMock(return_value={"result": "hardware only"})

        result = await agent._process(
            "Create an automation to turn on the room light when someone enters, and choose between the motion sensor and presence sensor."
        )

        self.assertEqual(result, agent._select_hardware.return_value)
        agent._select_hardware.assert_awaited_once_with(
            "Create an automation to turn on the room light when someone enters, and choose between the motion sensor and presence sensor.",
            devices,
        )
        agent._recommend_hardware.assert_not_called()
        agent._create_automation.assert_not_called()