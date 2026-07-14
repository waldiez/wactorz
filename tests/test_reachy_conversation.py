"""Focused tests for Reachy Mini's opt-in multi-turn voice session."""

import asyncio
import types
import unittest
from unittest import mock

import numpy as np

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE
from wactorz.catalogue_agents.reachy_stt import Transcription
from wactorz.catalogue_agents.reachy_vad import VoiceCapture

NS = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)


class FakeMedia:
    def start_recording(self):
        pass

    def stop_recording(self):
        pass

    def get_audio_sample(self):
        return None

    def get_input_audio_samplerate(self):
        return 16000

    def get_input_channels(self):
        return 2


class FakeAgent:
    name = "reachy-mini"

    def __init__(self):
        self.state = {
            "mini": types.SimpleNamespace(media=FakeMedia()),
            "media_backend": "webrtc",
            "conversation_session": None,
            "conversation_state": "idle",
            "last_cmd": None,
        }
        self.published, self.notifications, self.logs = [], [], []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def notify_user(self, text):
        self.notifications.append(text)

    async def log(self, text, level="info"):
        self.logs.append((level, text))

    def run_in_background(self, coro):
        return asyncio.create_task(coro)


def captured(reason=None):
    audio = np.zeros(0, np.float32) if reason else np.full(1600, 0.2, np.float32)
    return VoiceCapture(audio, 16000, 1, 0.1 if audio.size else 0.0, reason, 4)


async def immediate_cooldown(agent, session, turn, _seconds):
    await NS["_conversation_publish"](agent, session, "cooldown", turn)


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def run_session(self, agent, payload, clips, texts, bridge):
        clips, texts = iter(clips), iter(texts)

        async def fake_capture(_agent, _session, _config):
            return next(clips)

        async def fake_stt(_wav, _payload):
            value = next(texts)
            if isinstance(value, Exception):
                raise value
            return Transcription(value, "fake", "fake")

        with (
            mock.patch.dict(
                NS,
                {
                    "_conversation_capture": fake_capture,
                    "_conversation_cooldown": immediate_cooldown,
                    "_bridge_to_main": bridge,
                },
            ),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", fake_stt),
        ):
            result = await NS["_conversation_start"](agent, payload)
            session = agent.state["conversation_session"]
            await session["task"]
            return result, session

    async def test_three_home_assistant_turns_keep_context(self):
        agent, routed = FakeAgent(), []
        responses = ["The light is off.", "The light is back on.", "It is dimmed."]

        async def bridge(_agent, text, _task_id, **kwargs):
            routed.append(text)
            reply = responses[len(routed) - 1]
            await kwargs["before_speak"](reply)
            return {"result": reply, "spoke": True, "speech_error": None}

        _, session = await self.run_session(
            agent,
            {"max_turns": 3},
            [captured()] * 3,
            ["Turn off the living-room light", "Turn it back on", "Dim it"],
            bridge,
        )
        self.assertEqual(session["stop_reason"], "max_turns")
        self.assertEqual(agent.notifications, responses)
        self.assertEqual(routed[0], "Turn off the living-room light")
        self.assertIn("Current request: Turn it back on", routed[1])
        self.assertIn("Previous user request: Turn off the living-room light", routed[1])
        states = [p["state"] for _, p in agent.published if p.get("type") == "conversation"]
        for state in ("listening", "transcribing", "routing", "speaking", "cooldown", "stopped"):
            self.assertIn(state, states)
        final = [p for _, p in agent.published if p.get("state") == "stopped"][-1]
        required = {
            "session_id",
            "turn_index",
            "state",
            "transcript",
            "response",
            "capture_duration_s",
            "transcription_duration_s",
            "routing_duration_s",
            "stop_reason",
            "ok",
            "error",
            "type",
            "ts",
        }
        self.assertTrue(required <= final.keys())

    async def test_stop_phrase_never_routes(self):
        agent, bridge = FakeAgent(), mock.AsyncMock()
        _, session = await self.run_session(
            agent, {"max_turns": 5}, [captured()], ["Goodbye, Reachy!"], bridge
        )
        self.assertEqual(session["stop_reason"], "stop_phrase")
        bridge.assert_not_awaited()

    async def test_inactivity_timeout(self):
        agent = FakeAgent()
        _, session = await self.run_session(
            agent, {"max_turns": 5}, [captured("inactivity_timeout")], [], mock.AsyncMock()
        )
        self.assertEqual(session["stop_reason"], "inactivity_timeout")

    async def test_explicit_mqtt_stop_cancels_listening(self):
        agent, entered, cancelled = FakeAgent(), asyncio.Event(), asyncio.Event()

        async def blocked_capture(_agent, _session, _config):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with mock.patch.dict(NS, {"_conversation_capture": blocked_capture}):
            start = await NS["_dispatch"](agent, "conversation_start", {}, True)
            await entered.wait()
            stop = await NS["_dispatch"](agent, "conversation_stop", {}, True)
        self.assertTrue(start["ok"] and stop["ok"])
        self.assertEqual(stop["stop_reason"], "explicit_stop")
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(agent.state["conversation_session"])

    async def test_explicit_stop_cancels_routing(self):
        agent, entered, cancelled = FakeAgent(), asyncio.Event(), asyncio.Event()

        async def fake_capture(*_):
            return captured()

        async def fake_stt(*_):
            return Transcription("Turn off the light", "fake", "fake")

        async def blocked_bridge(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with (
            mock.patch.dict(
                NS, {"_conversation_capture": fake_capture, "_bridge_to_main": blocked_bridge}
            ),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", fake_stt),
        ):
            await NS["_conversation_start"](agent, {})
            await entered.wait()
            await NS["_conversation_stop"](agent, {})
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(agent.state["conversation_session"])

    async def test_explicit_stop_interrupts_speech(self):
        agent, entered = FakeAgent(), asyncio.Event()

        async def fake_capture(*_):
            return captured()

        async def fake_stt(*_):
            return Transcription("Turn off the light", "fake", "fake")

        async def blocked_speech(_agent, _text, _task_id, **kwargs):
            await kwargs["before_speak"]("Done")
            agent.state["_speaking"] = True
            entered.set()
            await asyncio.Future()

        stopped = mock.AsyncMock()
        with (
            mock.patch.dict(
                NS,
                {
                    "_conversation_capture": fake_capture,
                    "_bridge_to_main": blocked_speech,
                    "_stop_audio": stopped,
                },
            ),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", fake_stt),
        ):
            await NS["_conversation_start"](agent, {})
            await entered.wait()
            await NS["_conversation_stop"](agent, {})

        stopped.assert_awaited_once_with(agent)

    async def test_own_speech_finishes_then_stale_audio_flushes(self):
        agent, timeline = FakeAgent(), []
        count = 0

        async def fake_capture(*_):
            nonlocal count
            count += 1
            timeline.append(f"listen:{count}")
            if count == 2:
                self.assertLess(timeline.index("speech:finished"), timeline.index("flush"))
                self.assertLess(timeline.index("flush"), timeline.index("listen:2"))
            return captured()

        texts = iter(("Turn off the light", "stop listening"))

        async def fake_stt(*_):
            return Transcription(next(texts), "fake", "fake")

        async def bridge(_agent, _text, _task_id, **kwargs):
            await kwargs["before_speak"]("Done")
            timeline.append("speech:started")
            self.assertEqual(count, 1)
            await asyncio.sleep(0)
            timeline.append("speech:finished")
            return {"result": "Done", "spoke": True, "speech_error": None}

        def drain(*_):
            timeline.append("flush")
            return 7

        with (
            mock.patch.dict(NS, {"_conversation_capture": fake_capture, "_bridge_to_main": bridge}),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", fake_stt),
            mock.patch("wactorz.catalogue_agents.reachy_vad.drain_audio", drain),
        ):
            await NS["_conversation_start"](agent, {"max_turns": 3, "cooldown_s": 0.1})
            session = agent.state["conversation_session"]
            await session["task"]
        self.assertEqual(session["stop_reason"], "stop_phrase")

    async def test_second_session_is_rejected(self):
        agent, entered = FakeAgent(), asyncio.Event()

        async def blocked_capture(*_):
            entered.set()
            await asyncio.Future()

        with mock.patch.dict(NS, {"_conversation_capture": blocked_capture}):
            await NS["_conversation_start"](agent, {})
            await entered.wait()
            second = await NS["_dispatch"](agent, "conversation_start", {}, True)
            await NS["_conversation_stop"](agent, {})
        self.assertFalse(second["ok"])
        self.assertEqual(second["stage"], "session_active")

    async def test_turn_failure_recovers_cleanly(self):
        agent, calls = FakeAgent(), 0

        async def bridge(_agent, _text, _task_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary route outage")
            await kwargs["before_speak"]("Recovered")
            return {"result": "Recovered", "spoke": True, "speech_error": None}

        _, session = await self.run_session(
            agent,
            {"max_turns": 4},
            [captured()] * 3,
            ["First try", "Second try", "end conversation"],
            bridge,
        )
        self.assertEqual(session["stop_reason"], "stop_phrase")
        self.assertEqual(agent.notifications, ["Recovered"])
        errors = [p for _, p in agent.published if p.get("state") == "error"]
        self.assertTrue(any("routing_failed" in (p.get("error") or "") for p in errors))

    async def test_chat_shortcuts_dispatch_start_and_stop(self):
        agent, seen = FakeAgent(), []

        async def fake_dispatch(_agent, cmd, payload, return_result=False):
            seen.append(cmd)
            return {"ok": True, "cmd": cmd, "result": cmd}

        with mock.patch.dict(NS, {"_dispatch": fake_dispatch}):
            start = await NS["handle_task"](agent, {"text": "start conversation"})
            stop = await NS["handle_task"](agent, {"text": "stop conversation"})
        self.assertEqual(seen, ["conversation_start", "conversation_stop"])
        self.assertEqual(start["cmd"], "conversation_start")
        self.assertEqual(stop["cmd"], "conversation_stop")


if __name__ == "__main__":
    unittest.main()
