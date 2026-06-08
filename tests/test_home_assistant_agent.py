import json
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
sys.modules.setdefault("websockets", types.ModuleType("websockets"))

from wactorz.agents.home_assistant_agent import HomeAssistantAgent
from wactorz.agents.llm_agent import ToolCall, ToolCompletion
from wactorz.core.actor import Message, MessageType


class _ClassifyingLLM:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, *args, **kwargs):
        return self.response, {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


class _SequencedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.responses.pop(0), {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


class _ToolLLM:
    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = []

    async def complete_with_tools(self, **kwargs):
        self.calls.append(kwargs)
        if self.completions:
            return self.completions.pop(0)
        return ToolCompletion(content="done", usage={})


class _FailingToolLLM:
    async def complete_with_tools(self, **kwargs):
        raise RuntimeError("provider does not support tools")


class _FailingCompleteLLM:
    async def complete(self, *args, **kwargs):
        raise RuntimeError("llm failed")


def _make_agent(testcase: unittest.TestCase, llm_provider=None) -> HomeAssistantAgent:
    tmpdir = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmpdir.cleanup)
    agent = HomeAssistantAgent(
        llm_provider=llm_provider,
        name="home-assistant-agent-test",
        persistence_dir=tmpdir.name,
    )
    agent.ha_url = "ws://ha.local:8123/api/websocket"
    agent.ha_token = "token"
    return agent


def _valid_automation(name: str = "Porch lights") -> dict:
    return {
        "name": name,
        "trigger": [{"platform": "state", "entity_id": "binary_sensor.porch_motion", "to": "on"}],
        "condition": [],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.porch"}}],
        "mode": "single",
    }


def _devices() -> dict:
    return {
        "connected": True,
        "reason": "",
        "data": {
            "devices": [{"id": "dev-1", "name": "Porch Motion"}],
            "entities": [
                {"entity_id": "binary_sensor.porch_motion", "name": "Porch Motion"},
                {"entity_id": "light.porch", "name": "Porch Light"},
                {"entity_id": "light.back_porch", "name": "Back Porch Light"},
                {"entity_id": "sensor.kitchen_temp", "name": "Kitchen Temp"},
            ],
            "floors": [],
            "areas": [{"area_id": "porch", "name": "Porch"}],
        },
    }


class HomeAssistantAgentOtherFeatureTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, llm_provider=None) -> HomeAssistantAgent:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        agent = HomeAssistantAgent(
            llm_provider=llm_provider,
            name="home-assistant-agent-test",
            persistence_dir=tmpdir.name,
        )
        agent.ha_url = "ws://ha.local:8123/api/websocket"
        agent.ha_token = "token"
        return agent

    def _valid_automation(self) -> dict:
        return {
            "name": "Porch lights",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.porch_motion", "to": "on"}],
            "condition": [],
            "action": [{"service": "light.turn_on", "target": {"entity_id": "light.porch"}}],
            "mode": "single",
        }

    async def test_classify_action_accepts_other(self):
        """The HA action classifier must accept the new `other` class from the LLM."""
        agent = self._agent(_ClassifyingLLM("other"))

        self.assertEqual(await agent._classify_action("how is my home doing?"), "other")

    async def test_thermometer_existence_and_state_are_other(self):
        """Normal thermometer lookup/state questions route to the regular HA `other` flow."""
        agent = self._agent()

        self.assertEqual(agent._classify_action_heuristic("do I have any thermometers?"), "other")
        self.assertEqual(agent._classify_action_heuristic("what is the state of my thermometer?"), "other")

    async def test_classify_action_get_entities_state_is_literal_heuristic_only(self):
        """Only the literal trigger routes to the deterministic entity-state action."""
        agent = self._agent(_ClassifyingLLM("other"))

        self.assertEqual(
            await agent._classify_action("get_entities_state sensor.kitchen_temp"),
            "get_entities_state",
        )
        self.assertEqual(await agent._classify_action("state of sensor.kitchen_temp"), "other")

    async def test_entity_state_request_publishes_single_explicit_entity(self):
        """A single explicit entity state is fetched and published to homeassistant/state_changes/<entity_id>
        with a full state-change payload so dynamic agents can filter on entity_id."""
        agent = self._agent()
        agent._mqtt_publish = AsyncMock()

        state_obj = {"entity_id": "sensor.kitchen_temp", "state": "21"}

        with patch(
            "wactorz.agents.home_assistant_agent.get_states",
            new=AsyncMock(return_value=[
                state_obj,
                {"entity_id": "light.kitchen", "state": "off"},
            ]),
        ) as get_states:
            result = await agent._process("get_entities_state sensor.kitchen_temp")

        get_states.assert_awaited_once_with(agent.ha_url, agent.ha_token)
        agent._mqtt_publish.assert_awaited_once_with(
            "homeassistant/state_changes/sensor.kitchen_temp",
            {
                "event_type": "state_changed",
                "entity_id": "sensor.kitchen_temp",
                "new_state": state_obj,
                "old_state": None,
            },
        )
        self.assertIn("sensor.kitchen_temp: 21", result["result"])
        self.assertEqual(result["data"]["missing"], [])

    async def test_entity_state_request_publishes_multiple_explicit_entities(self):
        """Multiple explicit entities publish one MQTT message per found state."""
        agent = self._agent()
        agent._mqtt_publish = AsyncMock()

        state_obj_temp = {"entity_id": "sensor.kitchen_temp", "state": "21"}
        state_obj_light = {"entity_id": "light.kitchen", "state": "on"}

        with patch(
            "wactorz.agents.home_assistant_agent.get_states",
            new=AsyncMock(return_value=[state_obj_temp, state_obj_light]),
        ):
            result = await agent._handle_entities_state_request(
                "get_entities_state sensor.kitchen_temp and light.kitchen"
            )

        self.assertEqual(agent._mqtt_publish.await_count, 2)
        agent._mqtt_publish.assert_any_await(
            "homeassistant/state_changes/sensor.kitchen_temp",
            {
                "event_type": "state_changed",
                "entity_id": "sensor.kitchen_temp",
                "new_state": state_obj_temp,
                "old_state": None,
            },
        )
        agent._mqtt_publish.assert_any_await(
            "homeassistant/state_changes/light.kitchen",
            {
                "event_type": "state_changed",
                "entity_id": "light.kitchen",
                "new_state": state_obj_light,
                "old_state": None,
            },
        )
        self.assertIn("sensor.kitchen_temp: 21", result["result"])
        self.assertIn("light.kitchen: on", result["result"])

    async def test_entity_state_request_reports_missing_without_publish(self):
        """Missing explicit entities are reported and not published."""
        agent = self._agent()
        agent._mqtt_publish = AsyncMock()

        state_obj = {"entity_id": "sensor.kitchen_temp", "state": "21"}

        with patch(
            "wactorz.agents.home_assistant_agent.get_states",
            new=AsyncMock(return_value=[state_obj]),
        ):
            result = await agent._handle_entities_state_request(
                "get_entities_state sensor.kitchen_temp and light.missing"
            )

        # Only the found entity is published — missing ones are silently skipped.
        agent._mqtt_publish.assert_awaited_once_with(
            "homeassistant/state_changes/sensor.kitchen_temp",
            {
                "event_type": "state_changed",
                "entity_id": "sensor.kitchen_temp",
                "new_state": state_obj,
                "old_state": None,
            },
        )
        self.assertEqual(result["data"]["missing"], ["light.missing"])
        self.assertIn("Missing: light.missing", result["result"])

    async def test_entity_state_request_without_explicit_id_returns_error(self):
        """State requests without literal entity IDs do not query HA or publish MQTT."""
        agent = self._agent()
        agent._mqtt_publish = AsyncMock()

        with patch(
            "wactorz.agents.home_assistant_agent.get_states",
            new=AsyncMock(return_value=[]),
        ) as get_states:
            result = await agent._handle_entities_state_request("get_entities_state my thermometer")

        self.assertEqual(result["error"], "explicit_entity_id_required")
        self.assertIn("explicit Home Assistant entity IDs", result["result"])
        get_states.assert_not_awaited()
        agent._mqtt_publish.assert_not_awaited()

    async def test_entity_state_request_missing_ha_config_returns_config_error(self):
        """Explicit entity requests stop before querying or publishing when HA config is missing."""
        agent = self._agent()
        agent.ha_url = ""
        agent._mqtt_publish = AsyncMock()

        with patch(
            "wactorz.agents.home_assistant_agent.get_states",
            new=AsyncMock(return_value=[]),
        ) as get_states:
            result = await agent._handle_entities_state_request("get_entities_state sensor.kitchen_temp")

        self.assertEqual(result["error"], "HA_URL or HA_TOKEN not configured.")
        get_states.assert_not_awaited()
        agent._mqtt_publish.assert_not_awaited()

    async def test_classify_action_unknown_means_not_ha(self):
        """Non-HA requests remain `unknown` and must not fetch Home Assistant data."""
        agent = self._agent()

        self.assertEqual(agent._classify_action_heuristic("write me a Python script"), "unknown")
        with patch(
            "wactorz.agents.home_assistant_agent.get_simplified_ha_data",
            new=AsyncMock(return_value={}),
        ) as get_data:
            result = await agent._process("write me a Python script")

        self.assertIn("I can help with Home Assistant", result["result"])
        get_data.assert_not_awaited()

    async def test_other_routes_to_other_handler(self):
        """The dispatcher sends the new `other` action to the dedicated tool-loop handler."""
        agent = self._agent()
        agent._classify_action = AsyncMock(return_value="other")
        agent._handle_other_request = AsyncMock(return_value={"result": "handled"})

        result = await agent._process("what is going on in my home?")

        self.assertEqual(result, {"result": "handled"})
        agent._handle_other_request.assert_awaited_once_with("what is going on in my home?")

    async def test_other_missing_ha_config_returns_config_error(self):
        """The `other` flow stops early with a clear error when HA is not configured."""
        agent = self._agent(_ToolLLM([]))
        agent.ha_url = ""

        result = await agent._handle_other_request("what is the kitchen temperature?")

        self.assertEqual(result["error"], "HA_URL or HA_TOKEN not configured.")
        self.assertIn("HA_URL or HA_TOKEN not configured", result["result"])

    async def test_other_tool_loop_calls_simplified_ha_data(self):
        """A model tool request executes `get_simplified_ha_data` and returns the final answer."""
        llm = _ToolLLM(
            [
                ToolCompletion(
                    content="",
                    usage={"input_tokens": 2},
                    tool_calls=[ToolCall(id="call-1", name="get_simplified_ha_data")],
                    assistant_message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "get_simplified_ha_data", "arguments": "{}"},
                            }
                        ],
                    },
                ),
                ToolCompletion(content="Kitchen is 21 C.", usage={"output_tokens": 4}),
            ]
        )
        agent = self._agent(llm)

        with patch(
            "wactorz.agents.home_assistant_agent.get_simplified_ha_data",
            new=AsyncMock(return_value={"entities": [{"entity_id": "sensor.kitchen_temp", "state": "21"}]}),
        ) as get_data:
            result = await agent._handle_other_request("what is the kitchen temperature?")

        get_data.assert_awaited_once_with(agent.ha_url, agent.ha_token)
        self.assertEqual(result["result"], "Kitchen is 21 C.")

    async def test_other_tool_loop_reuses_duplicate_tool_result(self):
        """Repeated requests for the same HA data within one loop reuse the cached result."""
        llm = _ToolLLM(
            [
                ToolCompletion(
                    content="",
                    usage={},
                    tool_calls=[
                        ToolCall(id="call-1", name="get_simplified_ha_data"),
                        ToolCall(id="call-2", name="get_simplified_ha_data"),
                    ],
                    assistant_message={"role": "assistant", "content": "", "tool_calls": []},
                ),
                ToolCompletion(content="I checked it once.", usage={}),
            ]
        )
        agent = self._agent(llm)

        with patch(
            "wactorz.agents.home_assistant_agent.get_simplified_ha_data",
            new=AsyncMock(return_value={"entities": []}),
        ) as get_data:
            result = await agent._handle_other_request("summarize my home")

        self.assertEqual(result["result"], "I checked it once.")
        get_data.assert_awaited_once()

    async def test_other_tool_loop_stops_after_max_rounds(self):
        """The `other` tool loop has a hard round limit to avoid infinite LLM/tool cycles."""
        llm = _ToolLLM(
            [
                ToolCompletion(
                    content="",
                    usage={},
                    tool_calls=[ToolCall(id=f"call-{i}", name="get_simplified_ha_data")],
                    assistant_message={"role": "assistant", "content": "", "tool_calls": []},
                )
                for i in range(5)
            ]
        )
        agent = self._agent(llm)
        agent._other_tool_max_rounds = 3

        with patch(
            "wactorz.agents.home_assistant_agent.get_simplified_ha_data",
            new=AsyncMock(return_value={"entities": []}),
        ):
            result = await agent._handle_other_request("keep checking my home")

        self.assertEqual(result["error"], "tool_round_limit")
        self.assertIn("within 3 tool rounds", result["result"])

    async def test_other_tool_loop_provider_error_is_reported(self):
        """Provider tool-call failures are reported as user-visible HA tool errors."""
        agent = self._agent(_FailingToolLLM())

        result = await agent._handle_other_request("what is the kitchen temperature?")

        self.assertIn("Home Assistant tool request failed", result["result"])
        self.assertIn("provider does not support tools", result["error"])

    async def test_edit_automation_updates_generated_automation(self):
        """Editing awaits generation and sends the generated automation payload to HA."""
        updated_automation = self._valid_automation()
        llm = _SequencedLLM(
            [
                '{"found": true, "automation_id": "abc123", "automation_name": "Porch lights"}',
                json.dumps({"can_edit": True, "automation": updated_automation}),
            ]
        )
        agent = self._agent(llm)

        automations = [{"id": "abc123", "alias": "Porch lights"}]
        devices = {"data": {"entities": [{"entity_id": "light.porch"}]}}

        with (
            patch(
                "wactorz.agents.home_assistant_agent.get_automations",
                new=AsyncMock(return_value=[{"id": "abc123", "alias": "Porch lights", **updated_automation}]),
            ),
            patch(
                "wactorz.agents.home_assistant_agent.update_automation",
                new=AsyncMock(return_value=True),
            ) as update,
        ):
            result = await agent._edit_automation("turn on porch light on motion", automations, devices)

        self.assertTrue(result["edited"])
        update.assert_awaited_once_with(agent.ha_url, agent.ha_token, "abc123", updated_automation)
        self.assertEqual(result["automation"], updated_automation)

    async def test_edit_automation_uses_minimal_config_when_full_config_missing(self):
        """The edit flow preserves the old fallback when HA cannot return full config."""
        updated_automation = self._valid_automation()
        llm = _SequencedLLM(
            [
                '{"found": true, "automation_id": "abc123", "automation_name": "Porch lights"}',
                json.dumps({"can_edit": True, "automation": updated_automation}),
            ]
        )
        agent = self._agent(llm)

        with (
            patch(
                "wactorz.agents.home_assistant_agent.get_automations",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "wactorz.agents.home_assistant_agent.update_automation",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await agent._edit_automation(
                "turn on porch light on motion",
                [{"id": "abc123", "alias": "Porch lights"}],
                {"data": {"entities": []}},
            )

        self.assertTrue(result["edited"])
        edit_payload = llm.calls[1]["kwargs"]["messages"][0]["content"]
        self.assertEqual(
            json.loads(edit_payload)["existing_automation"],
            {"id": "abc123", "alias": "Porch lights"},
        )

    async def test_edit_automation_identify_failure_stops_before_ha_update(self):
        """If no automation can be identified, edit does not fetch full config or update HA."""
        llm = _SequencedLLM(['{"found": false, "result": "Ambiguous automation."}'])
        agent = self._agent(llm)

        with (
            patch(
                "wactorz.agents.home_assistant_agent.get_automations",
                new=AsyncMock(return_value=[]),
            ) as get_automations,
            patch(
                "wactorz.agents.home_assistant_agent.update_automation",
                new=AsyncMock(return_value=True),
            ) as update,
        ):
            result = await agent._edit_automation(
                "change porch lights",
                [{"id": "abc123", "alias": "Porch lights"}],
                {"data": {"entities": []}},
            )

        self.assertFalse(result["edited"])
        get_automations.assert_not_awaited()
        update.assert_not_awaited()

    async def test_edit_automation_can_edit_false_stops_before_ha_update(self):
        """If the edit LLM refuses the edit, no Home Assistant update is sent."""
        llm = _SequencedLLM(
            [
                '{"found": true, "automation_id": "abc123", "automation_name": "Porch lights"}',
                '{"can_edit": false, "result": "The requested change is unclear."}',
            ]
        )
        agent = self._agent(llm)

        with (
            patch(
                "wactorz.agents.home_assistant_agent.get_automations",
                new=AsyncMock(return_value=[{"id": "abc123", "alias": "Porch lights"}]),
            ),
            patch(
                "wactorz.agents.home_assistant_agent.update_automation",
                new=AsyncMock(return_value=True),
            ) as update,
        ):
            result = await agent._edit_automation(
                "change porch lights",
                [{"id": "abc123", "alias": "Porch lights"}],
                {"data": {"entities": []}},
            )

        self.assertFalse(result["edited"])
        update.assert_not_awaited()

    async def test_edit_automation_validation_failure_stops_before_ha_update(self):
        """Generated automation must pass local validation before HA is updated."""
        invalid_automation = {
            "name": "Porch lights",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.porch_motion", "to": "on"}],
            "condition": [],
            "mode": "single",
        }
        llm = _SequencedLLM(
            [
                '{"found": true, "automation_id": "abc123", "automation_name": "Porch lights"}',
                json.dumps({"can_edit": True, "automation": invalid_automation}),
            ]
        )
        agent = self._agent(llm)

        with (
            patch(
                "wactorz.agents.home_assistant_agent.get_automations",
                new=AsyncMock(return_value=[{"id": "abc123", "alias": "Porch lights"}]),
            ),
            patch(
                "wactorz.agents.home_assistant_agent.update_automation",
                new=AsyncMock(return_value=True),
            ) as update,
        ):
            result = await agent._edit_automation(
                "change porch lights",
                [{"id": "abc123", "alias": "Porch lights"}],
                {"data": {"entities": []}},
            )

        self.assertFalse(result["edited"])
        self.assertIn("automation.action", result["result"])
        update.assert_not_awaited()

    async def test_edit_automation_update_exception_returns_not_edited(self):
        """Home Assistant update failures are reported without claiming success."""
        updated_automation = self._valid_automation()
        llm = _SequencedLLM(
            [
                '{"found": true, "automation_id": "abc123", "automation_name": "Porch lights"}',
                json.dumps({"can_edit": True, "automation": updated_automation}),
            ]
        )
        agent = self._agent(llm)

        with (
            patch(
                "wactorz.agents.home_assistant_agent.get_automations",
                new=AsyncMock(return_value=[{"id": "abc123", "alias": "Porch lights"}]),
            ),
            patch(
                "wactorz.agents.home_assistant_agent.update_automation",
                new=AsyncMock(side_effect=RuntimeError("HA rejected it")),
            ),
        ):
            result = await agent._edit_automation(
                "change porch lights",
                [{"id": "abc123", "alias": "Porch lights"}],
                {"data": {"entities": []}},
            )

        self.assertFalse(result["edited"])
        self.assertIn("HA rejected it", result["result"])


class HomeAssistantAgentEntrypointDispatchTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, llm_provider=None) -> HomeAssistantAgent:
        return _make_agent(self, llm_provider)

    async def test_process_routes_every_supported_action(self):
        agent = self._agent()
        routes = [
            ("list_areas", "_list_areas", {}, {"result": "areas"}),
            ("list_devices", "_list_devices", {}, {"result": "devices"}),
            ("list_entities", "_list_entities", {}, {"result": "entities"}),
            ("get_entities_state", "_handle_entities_state_request", {"text"}, {"result": "state"}),
            ("other", "_handle_other_request", {"text"}, {"result": "other"}),
        ]
        for action, method_name, expects_text, expected in routes:
            with self.subTest(action=action):
                agent._classify_action = AsyncMock(return_value=action)
                mocked = AsyncMock(return_value=expected)
                setattr(agent, method_name, mocked)
                self.assertEqual(await agent._process("hello"), expected)
                if expects_text:
                    mocked.assert_awaited_once_with("hello")
                else:
                    mocked.assert_awaited_once_with()

    async def test_process_routes_automation_and_hardware_actions(self):
        agent = self._agent()

        agent._classify_action = AsyncMock(return_value="list_automations")
        agent._get_automations_brief = AsyncMock(return_value=[{"name": "A"}])
        self.assertIn("Found 1 automation", (await agent._process("list"))["result"])

        agent._classify_action = AsyncMock(return_value="delete_automation")
        agent._get_automations_brief = AsyncMock(return_value=[{"id": "a", "name": "A"}])
        agent._delete_automation = AsyncMock(return_value={"deleted": True})
        self.assertEqual(await agent._process("delete"), {"deleted": True})
        agent._delete_automation.assert_awaited_once()

        for action in ("recommend_hardware", "create_automation"):
            with self.subTest(action=action):
                agent._classify_action = AsyncMock(return_value=action)
                agent._get_devices = AsyncMock(return_value=_devices())
                agent._recommend_hardware = AsyncMock(return_value={"can_fulfill": True})
                self.assertEqual(await agent._process("make automation"), {"can_fulfill": True})
                agent._recommend_hardware.assert_awaited_once_with("make automation", _devices())

        agent._classify_action = AsyncMock(return_value="edit_automation")
        agent._get_automations_brief = AsyncMock(return_value=[{"id": "a", "name": "A"}])
        agent._get_devices = AsyncMock(return_value=_devices())
        agent._edit_automation = AsyncMock(return_value={"edited": True})
        self.assertEqual(await agent._process("edit"), {"edited": True})
        agent._edit_automation.assert_awaited_once()

        agent._classify_action = AsyncMock(return_value="unknown")
        self.assertIn("I can help with Home Assistant", (await agent._process("x"))["result"])

    async def test_chat_persists_history_and_stream_yields_response_then_done_marker(self):
        agent = self._agent()
        agent._process = AsyncMock(return_value={"result": "hello back"})
        agent._maybe_summarize = AsyncMock()
        agent.persist = MagicMock()
        agent._log_chat_turn = MagicMock()

        self.assertEqual(await agent.chat("hello"), "hello back")

        self.assertEqual([m["role"] for m in agent._conversation_history[-2:]], ["user", "assistant"])
        agent.persist.assert_called_with("conversation_history", agent._conversation_history)
        agent._maybe_summarize.assert_awaited_once()
        agent._log_chat_turn.assert_called_once()

        agent.chat = AsyncMock(return_value="streamed")
        chunks = []
        async for chunk in agent.chat_stream("hello"):
            chunks.append(chunk)
        self.assertEqual(chunks, ["streamed", {}])

    async def test_handle_message_ignores_non_tasks_and_sends_task_results(self):
        agent = self._agent()
        agent.send = AsyncMock()
        agent._process = AsyncMock(return_value={"result": "done"})

        await agent.handle_message(Message(MessageType.HEARTBEAT, "sender", "ping"))
        agent._process.assert_not_awaited()
        agent.send.assert_not_awaited()
        self.assertEqual(agent.metrics.tasks_completed, 0)

        await agent.handle_message(Message(MessageType.TASK, "sender", {"text": "list", "_task_id": "t-1"}))
        agent._process.assert_awaited_once_with("list")
        sent_payload = agent.send.await_args.args[2]
        self.assertEqual(sent_payload["task"], "list")
        self.assertEqual(sent_payload["_task_id"], "t-1")
        self.assertEqual(agent.metrics.tasks_completed, 1)

    async def test_handle_message_preselected_hardware_routes_directly_to_create(self):
        agent = self._agent()
        agent.send = AsyncMock()
        agent._process = AsyncMock()
        agent._create_automation = AsyncMock(return_value={"result": "created"})

        payload = {"task": "turn on light", "entities": [" light.porch "], "hardware": [{"hardware": "light"}]}
        await agent.handle_message(Message(MessageType.TASK, "sender", payload))

        agent._process.assert_not_awaited()
        agent._create_automation.assert_awaited_once_with(
            "turn on light",
            ["light.porch"],
            [{"hardware": "light"}],
        )
        self.assertEqual(agent.send.await_args.args[1], MessageType.RESULT)


class HomeAssistantAgentRegistryAndCacheTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, llm_provider=None) -> HomeAssistantAgent:
        return _make_agent(self, llm_provider)

    async def test_fetch_registry_items_handles_config_shape_and_errors(self):
        agent = self._agent()
        fetcher = AsyncMock(return_value=[{"id": "one"}])
        self.assertEqual(await agent._fetch_registry_items(fetcher), ([{"id": "one"}], None))
        fetcher.assert_awaited_once_with(agent.ha_url, agent.ha_token)

        fetcher = AsyncMock(return_value={"not": "a list"})
        self.assertEqual(await agent._fetch_registry_items(fetcher), ([], None))

        fetcher = AsyncMock(side_effect=RuntimeError("offline"))
        items, error = await agent._fetch_registry_items(fetcher)
        self.assertEqual(items, [])
        self.assertIn("offline", error)

        agent.ha_url = ""
        self.assertEqual(await agent._fetch_registry_items(AsyncMock()), ([], "HA_URL or HA_TOKEN not configured."))

    async def test_list_areas_devices_entities_format_empty_success_and_errors(self):
        agent = self._agent()
        with patch("wactorz.agents.home_assistant_agent.get_areas", new=AsyncMock(return_value=[])):
            self.assertIn("No areas", (await agent._list_areas())["result"])
        with patch("wactorz.agents.home_assistant_agent.get_areas", new=AsyncMock(return_value=[
            {"area_id": "kitchen", "name": "Kitchen"},
            {"area_id": "blank"},
        ])):
            result = await agent._list_areas()
            self.assertEqual(result["areas"][1]["name"], "(unnamed)")
            self.assertIn("Kitchen (kitchen)", result["result"])

        with patch("wactorz.agents.home_assistant_agent.get_devices", new=AsyncMock(return_value=[
            {"id": "d1", "name_by_user": "Lamp", "manufacturer": "Acme", "model": "L1"},
            {"id": "d2"},
        ])):
            result = await agent._list_devices()
            self.assertEqual(result["devices"][0]["device_id"], "d1")
            self.assertIn("Lamp (Acme L1)", result["result"])

        with patch("wactorz.agents.home_assistant_agent.get_entities", new=AsyncMock(return_value=[
            {"entity_id": "light.kitchen", "platform": "mqtt"},
            {"entity_id": "sensor.temp", "original_name": "Temp"},
        ])):
            result = await agent._list_entities()
            self.assertEqual(result["entities"][1]["name"], "Temp")
            self.assertIn("light.kitchen (mqtt)", result["result"])

        agent.ha_token = ""
        self.assertIn("HA_URL or HA_TOKEN", (await agent._list_devices())["result"])

    async def test_get_devices_handles_config_success_unexpected_type_exception_and_cache(self):
        agent = self._agent()
        with patch(
            "wactorz.agents.home_assistant_agent.get_simplified_ha_data",
            new=AsyncMock(return_value={"entities": [{"entity_id": "light.porch"}]}),
        ) as get_data:
            first = await agent._get_devices()
            second = await agent._get_devices()
        self.assertTrue(first["connected"])
        self.assertEqual(second, first)
        get_data.assert_awaited_once()

        agent._device_cache = {"timestamp": 0.0, "data": None}
        with patch("wactorz.agents.home_assistant_agent.get_simplified_ha_data", new=AsyncMock(return_value=[])):
            self.assertEqual((await agent._get_devices())["data"], {})

        agent._device_cache = {"timestamp": 0.0, "data": None}
        with patch(
            "wactorz.agents.home_assistant_agent.get_simplified_ha_data",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await agent._get_devices()
            self.assertFalse(result["connected"])
            self.assertIn("boom", result["reason"])

        agent.ha_url = ""
        agent._device_cache = {"timestamp": 0.0, "data": None}
        self.assertFalse((await agent._get_devices())["connected"])

    async def test_automation_brief_cache_and_listing(self):
        agent = self._agent()
        full = [
            {"id": "id-1", "alias": "Morning", "description": "Wake up"},
            {"automation_id": "id-2", "name": "Night"},
        ]
        with patch("wactorz.agents.home_assistant_agent.get_automations", new=AsyncMock(return_value=full)) as get_auto:
            first = await agent._get_automations_brief()
            second = await agent._get_automations_brief()
        self.assertEqual(first[0], {"id": "id-1", "name": "Morning", "description": "Wake up"})
        self.assertEqual(first[1]["id"], "id-2")
        self.assertEqual(second, first)
        get_auto.assert_awaited_once()
        self.assertIn("Morning", agent._list_automations(first)["result"])

        agent._automation_cache = {"timestamp": 0.0, "data": None}
        with patch(
            "wactorz.agents.home_assistant_agent.get_automations",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ):
            self.assertEqual(await agent._get_automations_brief(), [])

        agent.ha_url = ""
        agent._automation_cache = {"timestamp": 0.0, "data": None}
        self.assertIn("or Home Assistant is not configured", agent._list_automations([])["result"])


class HomeAssistantAgentHardwareTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, llm_provider=None) -> HomeAssistantAgent:
        return _make_agent(self, llm_provider)

    async def test_recommend_hardware_config_no_llm_success_filtering_and_correction(self):
        agent = self._agent()
        disconnected = {"connected": False, "reason": "not configured", "data": {}}
        result = await agent._recommend_hardware("porch lights", disconnected)
        self.assertFalse(result["can_fulfill"])
        self.assertIn("not configured", result["result"])

        result = await self._agent()._recommend_hardware("porch lights", _devices())
        self.assertFalse(result["can_fulfill"])
        self.assertIn("No LLM provider configured", result["result"])

        response = json.dumps(
            {
                "can_fulfill": True,
                "primary_hardware": [
                    {
                        "hardware": "Porch light",
                        "why": "already installed",
                        "protocol": "Zigbee",
                        "required_entities": ["light.porch", "light.hallucinated"],
                    }
                ],
                "alternatives": [
                    {
                        "hardware": "Back porch light",
                        "why": "nearby",
                        "required_entities": ["light.back_porch"],
                        "alternative_to": "light.porch",
                    },
                    {
                        "hardware": "Duplicate primary",
                        "required_entities": ["light.porch"],
                        "alternative_to": "light.porch",
                    },
                ],
            }
        )
        agent = self._agent(_SequencedLLM([response]))
        result = await agent._recommend_hardware("porch lights", _devices())
        self.assertTrue(result["can_fulfill"])
        self.assertEqual(result["primary_hardware"][0]["required_entities"], ["light.porch"])
        self.assertEqual(result["primary_hardware"][0]["required_domains"], ["light"])
        self.assertEqual([a["hardware"] for a in result["alternatives"]], ["Back porch light"])

        agent = self._agent(_SequencedLLM([
            '{"can_fulfill": true, "primary_hardware": []}',
            json.dumps(
                {
                    "can_fulfill": True,
                    "primary_hardware": [{"hardware": "Light", "required_entities": ["light.porch"]}],
                }
            ),
        ]))
        result = await agent._recommend_hardware("porch lights", _devices())
        self.assertTrue(result["can_fulfill"])
        self.assertEqual(len(agent.llm.calls), 2)

        result = await self._agent(_SequencedLLM(["not-json"]))._recommend_hardware("porch lights", _devices())
        self.assertFalse(result["can_fulfill"])
        self.assertIn("Hardware recommendation error", result["result"])

    async def test_select_hardware_success_correction_and_errors(self):
        agent = self._agent()
        result = await agent._select_hardware("make automation", _devices())
        self.assertFalse(result["can_fulfill"])
        self.assertIn("No LLM provider configured", result["result"])

        fenced = """```json
{"can_fulfill": true, "hardware": [{"hardware": "Motion sensor", "protocol": "Zigbee", "why": "motion", "required_entities": ["binary_sensor.porch_motion"]}]}
```"""
        result = await self._agent(_SequencedLLM([fenced]))._select_hardware("make automation", _devices())
        self.assertTrue(result["can_fulfill"])
        self.assertIn("Best hardware", result["result"])

        agent = self._agent(_SequencedLLM([
            '{"can_fulfill": true, "hardware": []}',
            '{"can_fulfill": false, "result": "nothing suitable"}',
        ]))
        result = await agent._select_hardware("make automation", _devices())
        self.assertFalse(result["can_fulfill"])
        self.assertIn("nothing suitable", result["result"])
        self.assertEqual(len(agent.llm.calls), 2)

        result = await self._agent(_SequencedLLM(["[]"]))._select_hardware("make automation", _devices())
        self.assertFalse(result["can_fulfill"])
        self.assertIn("Hardware selection error", result["result"])


class HomeAssistantAgentAutomationCrudTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, llm_provider=None) -> HomeAssistantAgent:
        return _make_agent(self, llm_provider)

    async def test_generate_automation_refusal_success_defaults_and_validation(self):
        agent = self._agent()
        self.assertIn("No LLM provider", (await agent._generate_automation("x", [], []))["result"])

        agent = self._agent(_SequencedLLM(['{"can_create": false, "result": "need more"}']))
        result = await agent._generate_automation("x", [], [])
        self.assertFalse(result["can_create"])
        self.assertEqual(result["result"], "need more")

        minimal = {"name": "A", "trigger": [{"platform": "time"}], "action": [{"service": "light.turn_on"}]}
        agent = self._agent(_SequencedLLM([f"```json\n{json.dumps({'can_create': True, 'automation': minimal})}\n```"]))
        result = await agent._generate_automation("x", ["light.porch"], [])
        self.assertTrue(result["can_create"])
        self.assertEqual(result["automation"]["condition"], [])
        self.assertEqual(result["automation"]["mode"], "single")

        with self.assertRaisesRegex(ValueError, "automation.name"):
            await self._agent(_SequencedLLM(['{"can_create": true, "automation": []}']))._generate_automation("x", [], [])
        with self.assertRaisesRegex(ValueError, "automation.action"):
            await self._agent(_SequencedLLM([
                json.dumps({"can_create": True, "automation": {"name": "A", "trigger": [{"platform": "time"}]}})
            ]))._generate_automation("x", [], [])

    async def test_insert_and_create_automation_paths(self):
        agent = self._agent()
        agent.ha_url = ""
        self.assertFalse((await agent._insert_automation(_valid_automation()))["inserted"])

        agent = self._agent()
        with patch(
            "wactorz.agents.home_assistant_agent.create_automation_via_rest",
            new=AsyncMock(return_value={"ok": True}),
        ) as create:
            result = await agent._insert_automation(_valid_automation())
        self.assertTrue(result["inserted"])
        create.assert_awaited_once()

        with patch(
            "wactorz.agents.home_assistant_agent.create_automation_via_rest",
            new=AsyncMock(side_effect=RuntimeError("rejected")),
        ):
            self.assertIn("rejected", (await agent._insert_automation(_valid_automation()))["error"])

        agent = self._agent()
        agent._generate_automation = AsyncMock(return_value={"can_create": False, "result": "no", "automation": {}})
        self.assertFalse((await agent._create_automation("x", [], []))["can_create"])

        agent._generate_automation = AsyncMock(return_value={"can_create": True, "automation": _valid_automation()})
        agent._insert_automation = AsyncMock(return_value={"inserted": False, "error": "offline"})
        result = await agent._create_automation("x", [], [])
        self.assertTrue(result["can_create"])
        self.assertFalse(result["inserted"])
        self.assertIn("offline", result["result"])

        agent._insert_automation = AsyncMock(return_value={"inserted": True, "response": {"ok": True}})
        self.assertTrue((await agent._create_automation("x", [], []))["inserted"])

        agent._generate_automation = AsyncMock(side_effect=RuntimeError("boom"))
        self.assertIn("boom", (await agent._create_automation("x", [], []))["result"])

    async def test_delete_automation_paths(self):
        self.assertFalse((await self._agent()._delete_automation("delete", []))["deleted"])
        self.assertIn(
            "No LLM",
            (await self._agent()._delete_automation("delete", [{"id": "a", "name": "A"}]))["result"],
        )

        cases = [
            (_SequencedLLM(["not-json"]), "Could not identify"),
            (_SequencedLLM(["[]"]), "Could not identify which automation"),
            (_SequencedLLM(['{"found": false, "result": "ambiguous"}']), "ambiguous"),
            (_SequencedLLM(['{"found": true, "automation_name": "A"}']), "Could not determine automation ID"),
        ]
        for llm, expected_text in cases:
            with self.subTest(expected_text=expected_text):
                result = await self._agent(llm)._delete_automation("delete", [{"id": "a", "name": "A"}])
                self.assertFalse(result["deleted"])
                self.assertIn(expected_text, result["result"])

        llm = _SequencedLLM(['{"found": true, "automation_id": "a", "automation_name": "A"}'])
        agent = self._agent(llm)
        agent.ha_token = ""
        self.assertIn("HA_URL", (await agent._delete_automation("delete", [{"id": "a", "name": "A"}]))["result"])

        llm = _SequencedLLM(['{"found": true, "automation_id": "a", "automation_name": "A"}'])
        agent = self._agent(llm)
        agent._automation_cache = {"timestamp": 1.0, "data": [{"id": "a"}]}
        with patch("wactorz.agents.home_assistant_agent.delete_automation", new=AsyncMock(return_value=True)) as delete:
            result = await agent._delete_automation("delete", [{"id": "a", "name": "A"}])
        self.assertTrue(result["deleted"])
        self.assertIsNone(agent._automation_cache["data"])
        delete.assert_awaited_once_with(agent.ha_url, agent.ha_token, "a")

        for helper, expected in (
            (AsyncMock(return_value=False), "returned an error"),
            (AsyncMock(side_effect=RuntimeError("offline")), "offline"),
        ):
            llm = _SequencedLLM(['{"found": true, "automation_id": "a", "automation_name": "A"}'])
            with patch("wactorz.agents.home_assistant_agent.delete_automation", new=helper):
                result = await self._agent(llm)._delete_automation("delete", [{"id": "a", "name": "A"}])
            self.assertFalse(result["deleted"])
            self.assertIn(expected, result["result"])


class HomeAssistantAgentEditAndHelperRegressionTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self, llm_provider=None) -> HomeAssistantAgent:
        return _make_agent(self, llm_provider)

    async def test_edit_guard_and_helper_failure_paths(self):
        self.assertFalse((await self._agent()._edit_automation("x", [], _devices()))["edited"])
        self.assertIn("No LLM", (await self._agent()._edit_automation("x", [{"id": "a"}], _devices()))["result"])

        agent = self._agent(_SequencedLLM([]))
        agent.ha_url = ""
        self.assertIn("HA_URL", (await agent._edit_automation("x", [{"id": "a"}], _devices()))["result"])

        for llm, expected in (
            (_SequencedLLM(["not-json"]), "Could not identify automation"),
            (_SequencedLLM(["[]"]), "Could not identify which automation"),
            (_SequencedLLM(['{"found": true}']), "Could not determine"),
            (_SequencedLLM(['{"found": true, "automation_id": "a", "automation_name": "A"}', "[]"]), "Invalid generated"),
            (_SequencedLLM(['{"found": true, "automation_id": "a", "automation_name": "A"}', '{"can_edit": true, "automation": []}']), "automation.name"),
        ):
            with self.subTest(expected=expected):
                with patch("wactorz.agents.home_assistant_agent.get_automations", new=AsyncMock(return_value=[])):
                    result = await self._agent(llm)._edit_automation("x", [{"id": "a", "name": "A"}], _devices())
                self.assertFalse(result["edited"])
                self.assertIn(expected, result["result"])

    async def test_edit_fetches_config_by_id_or_alias_and_caps_entities(self):
        many_entities = {"data": {"entities": [{"entity_id": f"sensor.e{i}"} for i in range(105)]}}
        for full_config in (
            {"id": "a", "alias": "Different", **_valid_automation()},
            {"id": "different", "alias": "A", **_valid_automation()},
        ):
            with self.subTest(full_config=full_config):
                llm = _SequencedLLM([
                    '{"found": true, "automation_id": "a", "automation_name": "A"}',
                    json.dumps({"can_edit": True, "automation": _valid_automation("Edited")}),
                ])
                with (
                    patch("wactorz.agents.home_assistant_agent.get_automations", new=AsyncMock(return_value=[full_config])),
                    patch("wactorz.agents.home_assistant_agent.update_automation", new=AsyncMock(return_value=True)),
                ):
                    result = await self._agent(llm)._edit_automation("x", [{"id": "a", "name": "A"}], many_entities)
                self.assertTrue(result["edited"])
                edit_payload = json.loads(llm.calls[1]["kwargs"]["messages"][0]["content"])
                self.assertEqual(len(edit_payload["available_entities"]), 100)
                self.assertEqual(edit_payload["existing_automation"]["name"], "Porch lights")

        with patch(
            "wactorz.agents.home_assistant_agent.get_automations",
            new=AsyncMock(side_effect=RuntimeError("old HA")),
        ):
            self.assertEqual(await self._agent()._get_automation_config("a", "A"), {})


class HomeAssistantAgentStaticHelperTest(unittest.TestCase):
    def test_usage_classification_and_payload_helpers(self):
        agent = _make_agent(self)
        agent._persist_cost = MagicMock()
        agent._accumulate_usage({"input_tokens": 2, "output_tokens": 3, "cost_usd": 0.5})
        self.assertEqual(agent.total_input_tokens, 2)
        self.assertEqual(agent.total_output_tokens, 3)
        self.assertEqual(agent.total_cost_usd, 0.5)
        agent._accumulate_usage("bad")
        self.assertEqual(agent.total_input_tokens, 2)

        self.assertEqual(HomeAssistantAgent._extract_payload({"text": " x ", "entities": "bad"}), ("x", [], []))
        self.assertEqual(HomeAssistantAgent._extract_payload({"task": "x", "entities": [" light.a "]}), ("x", ["light.a"], []))
        self.assertEqual(HomeAssistantAgent._extract_payload("plain"), ("plain", [], []))
        self.assertEqual(HomeAssistantAgent._extract_task_id({"task": "t"}, "fallback"), "t")
        self.assertEqual(HomeAssistantAgent._extract_task_id({}, "fallback"), "fallback")
        self.assertEqual(HomeAssistantAgent._strip_fences("```json\n{\"a\": 1}\n```"), '{"a": 1}')
        self.assertIn("I can help", HomeAssistantAgent._unsupported_action_response("x")["result"])

    async def _classify(self, llm, text):
        return await _make_agent(self, llm)._classify_action(text)

    def test_classification_fallbacks_and_heuristic_categories(self):
        async def run():
            self.assertEqual(await self._classify(_ClassifyingLLM("nonsense"), "list devices"), "list_devices")
            self.assertEqual(await self._classify(_FailingCompleteLLM(), "list areas"), "list_areas")

        import asyncio
        asyncio.run(run())

        expectations = {
            "show me areas": "list_areas",
            "what devices": "list_devices",
            "show entities": "list_entities",
            "get_entities_state sensor.a": "get_entities_state",
            "what are my automations": "list_automations",
            "remove automation porch": "delete_automation",
            "rename automation porch": "edit_automation",
            "set up automation": "create_automation",
            "what sensor do I need": "recommend_hardware",
            "kitchen temperature": "other",
            "write code": "unknown",
        }
        for text, expected in expectations.items():
            with self.subTest(text=text):
                self.assertEqual(HomeAssistantAgent._classify_action_heuristic(text), expected)

    def test_validation_entity_and_hardware_helpers(self):
        valid = _valid_automation()
        self.assertIsNone(HomeAssistantAgent._validate_automation(valid))
        for mutation, expected in (
            ({"name": ""}, "automation.name"),
            ({"trigger": []}, "automation.trigger"),
            ({"action": []}, "automation.action"),
            ({"condition": {}}, "automation.condition"),
            ({"mode": ""}, "automation.mode"),
        ):
            automation = dict(valid)
            automation.update(mutation)
            self.assertIn(expected, HomeAssistantAgent._validate_automation(automation))

        devices = {"data": {"entities": [{"entity_id": "light.a"}, {}, {"entity_id": "sensor.b"}]}}
        self.assertEqual(HomeAssistantAgent._entity_ids_from_devices(devices), ["light.a", "sensor.b"])
        self.assertEqual(HomeAssistantAgent._available_entity_ids(devices), {"light.a", "sensor.b"})
        self.assertEqual(HomeAssistantAgent._extract_entity_ids("Light.A light.a SENSOR.B"), ["light.a", "sensor.b"])
        self.assertEqual(
            HomeAssistantAgent._extract_entity_ids_from_hardware(
                {"hardware": [{"required_entities": ["light.a", "light.a", "sensor.b"]}]}
            ),
            ["light.a", "sensor.b"],
        )

        normalized = HomeAssistantAgent._normalize_available_hardware_items(
            [
                {"hardware": " Lamp ", "required_entities": ["light.a", "light.missing"]},
                {"hardware": "Lamp", "required_entities": ["light.a"]},
                {"hardware": "", "required_entities": ["sensor.b"]},
                {"hardware": "Bad", "required_entities": ["no_domain"]},
            ],
            {"light.a", "sensor.b"},
        )
        self.assertEqual(normalized, [{
            "hardware": "Lamp",
            "why": "",
            "protocol": "N/A",
            "required_domains": ["light"],
            "required_entities": ["light.a"],
        }])

        alternatives = HomeAssistantAgent._filter_hardware_alternatives(
            [{"hardware": "Lamp", "required_entities": ["light.a"]}],
            [
                {"hardware": "Alt", "required_entities": ["sensor.b"], "alternative_to": "light.a"},
                {"hardware": "No link", "required_entities": ["sensor.b"]},
                {"hardware": "Overlap", "required_entities": ["light.a"], "alternative_to": "light.a"},
            ],
        )
        self.assertEqual([a["hardware"] for a in alternatives], ["Alt"])
        self.assertIn("Alt", HomeAssistantAgent._hardware_summary_lines(alternatives)[0])


if __name__ == "__main__":
    unittest.main()
