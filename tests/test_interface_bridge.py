"""Tests for the generic dynamic-agent to Main interface bridge."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from wactorz.agents.dynamic import DynamicAgent
from wactorz.agents.main.actor import MainActor
from wactorz.core.actor import Message, MessageType


class DynamicHandleTaskMailboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_handle_task_runs_in_background_so_results_can_be_received(self):
        actor = DynamicAgent(code="", name="bridge-agent")
        entered = asyncio.Event()
        release = asyncio.Event()
        replied = asyncio.Event()
        sent = []

        async def handle_task(_api, _payload):
            entered.set()
            await release.wait()
            return {"result": "done"}

        async def send(target, message_type, payload):
            sent.append((target, message_type, payload))
            replied.set()

        actor._fn_handle_task = handle_task
        actor.send = send
        msg = Message(type=MessageType.TASK, sender_id="caller", payload={})

        await asyncio.wait_for(actor.handle_message(msg), timeout=0.1)
        await asyncio.wait_for(entered.wait(), timeout=0.1)
        self.assertEqual(sent, [])
        release.set()
        await asyncio.wait_for(replied.wait(), timeout=0.1)
        self.assertEqual(sent[0][2]["result"], "done")


class MainInterfaceBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_interface_task_uses_orchestrator_context_and_replies(self):
        actor = MainActor(llm_provider=None)
        replied = asyncio.Event()
        seen = {}
        sent = []

        async def process(text):
            seen["source"] = actor._current_interface_source()
            seen["history"] = actor._current_interface_history()
            seen["voice"] = actor._current_interface_is_voice()
            seen["context"] = actor._current_interface_context()
            seen["prefix"] = actor._prefix_with_live_context(text)
            return "It is sunny."

        async def send(target, message_type, payload):
            sent.append((target, message_type, payload))
            replied.set()

        actor.process_user_input = AsyncMock(side_effect=process)
        actor.send = send
        actor._registry = types.SimpleNamespace(
            all_actors=lambda: [
                types.SimpleNamespace(name="reachy-mini"),
                types.SimpleNamespace(name="weather-agent"),
            ]
        )
        msg = Message(
            type=MessageType.TASK,
            sender_id="reachy-id",
            payload={
                "text": "weather?",
                "_via_interface": True,
                "_interface_source": "reachy-mini",
                "_interface_voice": True,
                "_interface_history": [{"transcript": "hello", "response": "Hi."}],
                "_interface_context": {
                    "display_name": "Reachy",
                    "kind": "embodied_robot",
                    "capabilities": {"gesture": ["dance", "turn_around"]},
                    "prompt_note": "The robot's name is Reachy.",
                },
                "_task_id": "abc",
                "_reply_to": "reachy-id",
            },
        )

        await actor._handle_task(msg)
        await asyncio.wait_for(replied.wait(), timeout=1.0)

        self.assertEqual(seen["source"], "reachy-mini")
        self.assertEqual(seen["history"][0]["transcript"], "hello")
        self.assertTrue(seen["voice"])
        self.assertEqual(seen["context"]["display_name"], "Reachy")
        self.assertIn("physical robot Reachy", seen["prefix"])
        self.assertEqual(actor._current_interface_source(), "")
        self.assertEqual(actor._current_interface_history(), ())
        self.assertFalse(actor._current_interface_is_voice())
        self.assertEqual(actor._current_interface_context(), {})
        self.assertEqual(sent[0][0], "reachy-id")
        self.assertEqual(sent[0][1], MessageType.RESULT)
        self.assertEqual(sent[0][2]["_task_id"], "abc")
        self.assertEqual(sent[0][2]["result"], "It is sunny.")

    async def test_interface_actions_are_allowlisted_and_removed_from_reply(self):
        from wactorz.agents.main import actor as main_module

        actor = MainActor(llm_provider=None)
        token = main_module._INTERFACE_CONTEXT.set(
            {
                "display_name": "Reachy",
                "kind": "embodied_robot",
                "capabilities": {"gesture": ("dance", "turn_around")},
            }
        )
        try:
            clean, actions = actor._extract_interface_actions(
                'Okay. <interface_action>{"cmd":"gesture","name":"turn_around"}</interface_action>'
                '<interface_action>{"cmd":"gesture","name":"unsafe"}</interface_action>'
            )
        finally:
            main_module._INTERFACE_CONTEXT.reset(token)

        self.assertEqual(clean, "Okay.")
        self.assertEqual(actions, [{"cmd": "gesture", "name": "turn_around"}])

    async def test_interface_source_cannot_be_redelegated(self):
        from wactorz.agents.main import actor as main_module

        actor = MainActor(llm_provider=None)
        actor._resolve_or_spawn = AsyncMock()
        token = main_module._INTERFACE_SOURCE.set("reachy-mini")
        try:
            structured, results = await actor._process_delegate_commands(
                '<delegate>{"agent":"reachy-mini","task":"say four"}</delegate>'
            )
            loose = await actor._execute_llm_delegations("@reachy-mini say four")
        finally:
            main_module._INTERFACE_SOURCE.reset(token)

        marker = "[interface loop prevented: reachy-mini]"
        self.assertEqual(structured, marker)
        self.assertEqual(results, [marker])
        self.assertEqual(loose, marker)
        actor._resolve_or_spawn.assert_not_awaited()
