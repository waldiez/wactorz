import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
sys.modules.setdefault("websockets", types.ModuleType("websockets"))
sys.modules.setdefault("openai", types.ModuleType("openai"))

from wactorz.agents.main_actor import MainActor
from wactorz.agents.llm_agent import OpenAIProvider
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
        actor._handle_actuate_intent = AsyncMock(return_value="Done: light.turn_on -> light.living_room.")
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
        provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FakeCompletions()))

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

            with patch("wactorz.agents.one_off_actuator_agent.CONFIG", types.SimpleNamespace(ha_url="http://ha.local:8123", ha_token="token")), \
                 patch("wactorz.agents.one_off_actuator_agent.fetch_devices_entities_with_location", AsyncMock(return_value=[])):
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

            with patch("wactorz.agents.one_off_actuator_agent.CONFIG", types.SimpleNamespace(ha_url="http://ha.local:8123", ha_token="token")), \
                 patch("wactorz.agents.one_off_actuator_agent.fetch_devices_entities_with_location", AsyncMock(return_value=[])), \
                 patch("wactorz.agents.one_off_actuator_agent.HAWebSocketClient", return_value=fake_client):
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


if __name__ == "__main__":
    unittest.main()
