import asyncio
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
sys.modules.setdefault("websockets", types.ModuleType("websockets"))
sys.modules.setdefault("openai", types.ModuleType("openai"))

from wactorz.agents.llm_agent import OpenAIProvider
from wactorz.agents.main_actor import MainActor
from wactorz.agents.one_off_actuator_agent import OneOffActuatorAgent


class _FakeLLM:
    def __init__(self, response: str):
        self._response = response

    async def complete(self, messages, system="", **kwargs):
        return self._response, {}


class _FakeHAClient:
    def __init__(self, ws_url: str, token: str):
        self.ws_url = ws_url
        self.token = token
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def call_service(self, domain, service, entity_id, **service_data):
        self.calls.append((domain, service, entity_id, service_data))
        return {"ok": True}


class MainActorActuateRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_classify_intent_accepts_actuate(self):
        actor = MainActor(llm_provider=_FakeLLM("ACTUATE"))
        self.assertEqual(await actor._classify_intent("turn on the light"), "ACTUATE")

    async def test_process_user_input_routes_actuate_to_one_off_handler(self):
        actor = MainActor(llm_provider=None)
        actor._classify_intent = AsyncMock(return_value="ACTUATE")
        actor._handle_actuate_intent = AsyncMock(
            return_value="Done: light.turn_on -> light.living_room."
        )
        actor.delegate_task = AsyncMock()

        result = await actor.process_user_input("turn on the living room light")

        self.assertEqual(result, "Done: light.turn_on -> light.living_room.")
        actor._handle_actuate_intent.assert_awaited_once_with("turn on the living room light")
        actor.delegate_task.assert_not_called()

    async def test_classify_intent_requests_none_reasoning_effort(self):
        llm = types.SimpleNamespace(complete=AsyncMock(return_value=("ACTUATE", {})))
        actor = MainActor(llm_provider=llm)

        result = await actor._classify_intent("turn off the office light")

        self.assertEqual(result, "ACTUATE")
        self.assertEqual(llm.complete.await_args.kwargs["reasoning_effort"], "none")


class MainInterfaceBridgeTest(unittest.IsolatedAsyncioTestCase):
    """An interface bridge (e.g. reachy) routes through the FULL orchestrator."""

    async def test_via_interface_task_runs_process_user_input_and_replies(self):
        from wactorz.core.actor import Message, MessageType

        actor = MainActor(llm_provider=None)
        actor.process_user_input = AsyncMock(return_value="It is sunny.")
        sent = []

        async def _send(target, mtype, payload):
            sent.append((target, mtype, payload))

        actor.send = _send

        msg = Message(
            type=MessageType.TASK,
            sender_id="reachy-1",
            payload={"text": "weather?", "_via_interface": True,
                     "_task_id": "abc", "_reply_to": "reachy-1"},
        )
        await actor._handle_task(msg)
        await asyncio.sleep(0.05)  # the request runs as a background task

        actor.process_user_input.assert_awaited_once_with("weather?")
        self.assertEqual(len(sent), 1)
        target, mtype, payload = sent[0]
        self.assertEqual(target, "reachy-1")
        self.assertEqual(mtype, MessageType.RESULT)
        self.assertEqual(payload["_task_id"], "abc")
        self.assertEqual(payload["result"], "It is sunny.")

    async def test_plain_task_does_not_use_orchestrator(self):
        from wactorz.core.actor import Message, MessageType

        actor = MainActor(llm_provider=None)
        actor.process_user_input = AsyncMock()

        # No _via_interface flag -> inherited handler (no-op with llm=None).
        msg = Message(type=MessageType.TASK, sender_id="x", payload={"text": "hi"})
        await actor._handle_task(msg)
        await asyncio.sleep(0.02)

        actor.process_user_input.assert_not_called()


class OpenAIProviderReasoningTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_passes_reasoning_effort_when_provided(self):
        class _FakeCompletions:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="HA"))],
                    usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                )

        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.model = "gpt-5-mini"
        provider.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=_FakeCompletions())
        )

        text, _usage = await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="low",
            max_tokens=12,
        )

        self.assertEqual(text, "HA")
        self.assertEqual(provider.client.chat.completions.calls[0]["reasoning_effort"], "low")


class OneOffActuatorAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_execute_request_returns_no_match_message_for_empty_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = OneOffActuatorAgent(
                request="turn on the mystery light",
                llm_provider=_FakeLLM("[]"),
                task_id="actuate_test",
                reply_to_id="main-actor",
                persistence_dir=tmpdir,
            )

            with (
                patch(
                    "wactorz.agents.one_off_actuator_agent.CONFIG",
                    types.SimpleNamespace(ha_url="http://ha.local:8123", ha_token="token"),
                ),
                patch(
                    "wactorz.agents.one_off_actuator_agent.fetch_devices_entities_with_location",
                    AsyncMock(return_value=[]),
                ),
            ):
                result = await agent._execute_request()

            self.assertEqual(result, "I couldn't identify a matching device for that request.")

    async def test_execute_request_runs_service_calls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_client = _FakeHAClient("ws://ha.local:8123/api/websocket", "token")
            agent = OneOffActuatorAgent(
                request="turn on the living room light",
                llm_provider=_FakeLLM(
                    '[{"domain":"light","service":"turn_on","entity_id":"light.living_room","service_data":{"brightness_pct":50}}]'
                ),
                task_id="actuate_test",
                reply_to_id="main-actor",
                persistence_dir=tmpdir,
            )

            with (
                patch(
                    "wactorz.agents.one_off_actuator_agent.CONFIG",
                    types.SimpleNamespace(ha_url="http://ha.local:8123", ha_token="token"),
                ),
                patch(
                    "wactorz.agents.one_off_actuator_agent.fetch_devices_entities_with_location",
                    AsyncMock(return_value=[]),
                ),
                patch(
                    "wactorz.agents.one_off_actuator_agent.HAWebSocketClient",
                    return_value=fake_client,
                ),
            ):
                result = await agent._execute_request()

            self.assertEqual(result, "Done: light.turn_on -> light.living_room.")
            self.assertEqual(
                fake_client.calls,
                [("light", "turn_on", "light.living_room", {"brightness_pct": 50})],
            )

    async def test_resolve_actions_accumulates_usage_costs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            llm = types.SimpleNamespace(
                complete=AsyncMock(
                    return_value=(
                        '[{"domain":"light","service":"turn_on","entity_id":"light.office","service_data":{}}]',
                        {"input_tokens": 11, "output_tokens": 7, "cost_usd": 0.00123},
                    )
                )
            )
            agent = OneOffActuatorAgent(
                request="turn off the office light",
                llm_provider=llm,
                task_id="actuate_test",
                reply_to_id="main-actor",
                persistence_dir=tmpdir,
            )

            actions = await agent._resolve_actions([])

            self.assertEqual(len(actions), 1)
            self.assertEqual(agent.total_input_tokens, 11)
            self.assertEqual(agent.total_output_tokens, 7)
            self.assertEqual(agent.total_cost_usd, 0.00123)
            self.assertEqual(agent._build_metrics()["cost_usd"], 0.00123)


class GenericColorLightCollapseTest(unittest.TestCase):
    """A generic 'change the light colour' must not fan out across every colour
    light. These exercise the pure repair/collapse path — no robot, no HA."""

    def _agent(self, request):
        return OneOffActuatorAgent(
            request=request,
            llm_provider=_FakeLLM("[]"),
            task_id="actuate_test",
            reply_to_id="main-actor",
            persistence_dir=tempfile.gettempdir(),
        )

    def _two_color_lights(self):
        # Both support rgb, so neither gets reassigned during colour repair.
        return [
            {"entity_id": "light.led_strip",
             "state": {"attributes": {"supported_color_modes": ["rgb"]}}},
            {"entity_id": "light.main",
             "state": {"attributes": {"supported_color_modes": ["rgb"]}}},
        ]

    def _both_on(self):
        from wactorz.agents.home_assistant_actuator_agent import ActuatorAction

        return [
            ActuatorAction(domain="light", service="turn_on",
                           entity_id="light.led_strip", service_data={}),
            ActuatorAction(domain="light", service="turn_on",
                           entity_id="light.main", service_data={}),
        ]

    def test_generic_color_request_collapses_to_one_light(self):
        agent = self._agent("turn the light pink")
        repaired = agent._repair_color_actions(self._both_on(), self._two_color_lights())
        light_ons = [a for a in repaired if a.domain == "light" and a.service == "turn_on"]
        self.assertEqual(len(light_ons), 1)  # not both
        self.assertEqual(light_ons[0].entity_id, "light.main")  # main, not the strip
        self.assertIn("rgb_color", light_ons[0].service_data)

    def test_prefers_main_light_over_accent_strip_regardless_of_order(self):
        # led_strip is listed FIRST; the main light must still win the tiebreak.
        agent = self._agent("change the light to blue")
        self.assertEqual(agent._find_color_light(self._two_color_lights()), "light.main")

    def test_plural_request_keeps_every_light(self):
        agent = self._agent("turn all the lights pink")
        repaired = agent._repair_color_actions(self._both_on(), self._two_color_lights())
        light_ons = [a for a in repaired if a.domain == "light" and a.service == "turn_on"]
        self.assertEqual({a.entity_id for a in light_ons},
                         {"light.led_strip", "light.main"})  # explicit plural -> all

    def test_enrichment_block_does_not_make_singular_look_plural(self):
        # Main appends an entity list; a "Living Room Lights" entity in it must
        # not defeat the collapse for a singular user request.
        request = (
            "turn the light pink\n\n"
            "[AVAILABLE HA ENTITIES — match the user's device to one of these:\n"
            "  light.led_strip (Living Room Lights)\n"
            "  light.main (Main Light)\n]"
        )
        agent = self._agent(request)
        self.assertFalse(agent._request_targets_multiple_lights())
        repaired = agent._repair_color_actions(self._both_on(), self._two_color_lights())
        light_ons = [a for a in repaired if a.domain == "light" and a.service == "turn_on"]
        self.assertEqual(len(light_ons), 1)

    def test_non_color_request_is_untouched(self):
        # No colour requested -> repair is a passthrough, both lights stay.
        agent = self._agent("turn on the light")
        repaired = agent._repair_color_actions(self._both_on(), self._two_color_lights())
        self.assertEqual(len(repaired), 2)


if __name__ == "__main__":
    unittest.main()
