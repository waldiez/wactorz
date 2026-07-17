"""Focused tests for Reachy Mini's opt-in multi-turn voice session."""

import asyncio
import os
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
        self.published, self.notifications, self.chat_messages, self.logs = [], [], [], []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def notify_user(self, text, **extra):
        self.chat_messages.append((text, extra))
        if extra.get("from") != "user":
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
            pending = _session.pop("pending_capture", None)
            if pending is not None:
                return pending
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
        spoken = [text for text, extra in agent.chat_messages if extra.get("from") == "user"]
        self.assertEqual(
            spoken,
            ["Turn off the living-room light", "Turn it back on", "Dim it"],
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
        self.assertEqual(agent.chat_messages[0][1].get("from"), "user")

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


    async def test_motor_noise_does_not_transcribe_or_consume_a_turn(self):
        agent, bridge = FakeAgent(), mock.AsyncMock()
        _, session = await self.run_session(
            agent,
            {"max_turns": 2},
            [captured("noise_rejected"), captured()],
            ["bye"],
            bridge,
        )
        self.assertEqual(session["stop_reason"], "stop_phrase")
        self.assertEqual(session["turn_index"], 1)
        bridge.assert_not_awaited()
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

    async def test_default_session_is_inactivity_driven(self):
        agent = FakeAgent()
        _, session = await self.run_session(
            agent,
            {},
            [captured("inactivity_timeout")],
            [],
            mock.AsyncMock(),
        )
        self.assertEqual(session["stop_reason"], "inactivity_timeout")
        self.assertNotEqual(session["stop_reason"], "max_turns")

    async def test_barge_in_audio_becomes_the_next_turn(self):
        agent, bridge_calls = FakeAgent(), []

        async def bridge(_agent, text, _task_id, **kwargs):
            bridge_calls.append(text)
            await kwargs["before_speak"]("A long answer that gets interrupted.")
            session = kwargs["barge_in_session"]
            session["pending_capture"] = captured()
            return {
                "result": "A long answer that gets interrupted.",
                "spoken_result": "A long answer",
                "spoke": True,
                "interrupted": True,
                "speech_error": None,
            }

        _, session = await self.run_session(
            agent,
            {"barge_in": True},
            [captured()],
            ["Tell me about today", "stop listening"],
            bridge,
        )
        self.assertEqual(len(bridge_calls), 1)
        self.assertEqual(session["barge_in_count"], 1)
        self.assertEqual(session["stop_reason"], "stop_phrase")
        self.assertTrue(session["history"][0]["interrupted"])
        self.assertEqual(session["history"][0]["response"], "A long answer")

    def test_voice_response_is_short_and_dashboard_friendly(self):
        reply = (
            "Here is the result. https://example.com/details "
            + "This is additional information. " * 30
        )
        spoken = NS["_voice_friendly_reply"](reply, limit=180)
        self.assertNotIn("https://", spoken)
        self.assertIn("Wactorz chat", spoken)
        self.assertLess(len(spoken), 260)
        chunks = NS["_speech_chunks"](spoken, max_chars=90)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 120 for chunk in chunks))

    async def test_punctuation_only_transcript_is_ignored(self):
        agent, bridge = FakeAgent(), mock.AsyncMock()
        _, session = await self.run_session(
            agent,
            {"max_turns": 2},
            [captured(), captured()],
            [". . . .", "bye"],
            bridge,
        )
        self.assertEqual(session["stop_reason"], "stop_phrase")
        bridge.assert_not_awaited()
        user_turns = [text for text, extra in agent.chat_messages if extra.get("from") == "user"]
        self.assertEqual(user_turns, ["bye"])

    async def test_default_barge_in_is_disabled(self):
        agent = FakeAgent()
        session = {"payload": {}, "cancel_event": None}
        monitor = await NS["_begin_barge_in_monitor"](agent, session, 2.0)
        self.assertIsNone(monitor)

    async def test_optional_state_motion_changes_only_antennas(self):
        agent = FakeAgent()
        calls = []

        def set_target(**kwargs):
            calls.append(kwargs)

        goto_target = mock.Mock()
        agent.state.update(
            {
                "mini": types.SimpleNamespace(
                    media=FakeMedia(), set_target=set_target, goto_target=goto_target
                ),
                "np": np,
                "motion_lock": asyncio.Lock(),
            }
        )

        await NS["_conversation_state_motion"](agent, "listening")

        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {"antennas"})
        goto_target.assert_not_called()

    async def test_state_motion_is_off_by_default(self):
        agent = FakeAgent()
        session = {"payload": {}}
        NS["_schedule_conversation_state_motion"](agent, session, "listening")
        self.assertNotIn("_motion_task", session)

    def test_natural_stop_phrases(self):
        for phrase in ("Bye.", "Goodbye!", "That's all"):
            with self.subTest(phrase=phrase):
                self.assertTrue(NS["_conversation_stop_phrase"](phrase))



    def test_spoken_reply_removes_visual_flourishes_and_ha_syntax(self):
        visual = "Hey! \U0001f60a *waves enthusiastically* Ready when you are."
        spoken = NS["_voice_friendly_reply"](visual)
        self.assertNotIn("\U0001f60a", spoken)
        self.assertNotIn("waves", spoken)
        self.assertEqual(spoken, "Hey! Ready when you are.")

        actuation = NS["_voice_friendly_reply"](
            "Done: light.turn_on -> light.main_light.",
            user_text="Make the light pink",
        )
        self.assertEqual(actuation, "Okay, the light is pink.")

    def test_reachy_name_is_repaired_without_naming_the_user(self):
        self.assertEqual(NS["_normalize_reachy_transcript"]("Hey Richie"), "Hey Reachy")
        self.assertIn("I'm Reachy", NS["_sanitize_reachy_identity_reply"]("I'm Richie"))
        self.assertEqual(NS["_sanitize_reachy_identity_reply"]("Need anything, Richie?"), "Need anything?")
        self.assertEqual(
            NS["_normalize_reachy_transcript"]("Turn off the man light"), "Turn off the main light"
        )

    def test_conversation_stt_defaults_to_english_but_honors_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(NS["_conversation_stt_payload"]({})["stt_language"], "en")
        explicit = NS["_conversation_stt_payload"]({"stt_language": "el"})
        self.assertEqual(explicit["stt_language"], "el")

    async def test_direct_dance_request_dispatches_a_real_gesture(self):
        agent, seen = FakeAgent(), []

        async def fake_dispatch(_agent, cmd, payload, return_result=False):
            seen.append((cmd, payload.get("name")))
            return {"ok": True, "cmd": cmd, "result": "done"}

        with mock.patch.dict(NS, {"_dispatch": fake_dispatch}):
            result = await NS["handle_task"](agent, {"text": "Do a little dance for me"})
        self.assertEqual(seen, [("gesture", "dance")])
        self.assertEqual(result["cmd"], "gesture")

    async def test_voice_dance_stays_local_and_moves_the_robot(self):
        agent, bridge = FakeAgent(), mock.AsyncMock()
        gesture = mock.AsyncMock(
            return_value={"gesture": "dance", "result": "Ta-da! I did a little dance."}
        )
        speak = mock.AsyncMock(
            return_value={"spoke": True, "interrupted": False, "spoken_result": "Ta-da!"}
        )

        with mock.patch.dict(
            NS,
            {
                "_gesture": gesture,
                "_speak_reply": speak,
            },
        ):
            _, session = await self.run_session(
                agent,
                {"max_turns": 2},
                [captured(), captured()],
                ["Do a little dance for me", "bye"],
                bridge,
            )
        self.assertEqual(session["stop_reason"], "stop_phrase")
        bridge.assert_not_awaited()
        gesture.assert_awaited_once()
        speak.assert_awaited_once()

    async def test_opt_in_idle_motion_stops_on_state_change(self):
        agent, entered = FakeAgent(), asyncio.Event()

        async def blocked_idle(_agent, _session):
            entered.set()
            await asyncio.Future()

        session = {
            "payload": {"idle_motion": True},
            "state": "listening",
            "cancel_event": types.SimpleNamespace(is_set=lambda: False),
        }
        with mock.patch.dict(NS, {"_conversation_idle_motion_loop": blocked_idle}):
            NS["_schedule_conversation_idle_motion"](agent, session, "listening")
            await entered.wait()
            task = session["_idle_motion_task"]
            self.assertIsNotNone(task)
            NS["_schedule_conversation_idle_motion"](agent, session, "routing")
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_listening_idle_motion_targets_only_antennas(self):
        agent, calls = FakeAgent(), []

        def set_target(**kwargs):
            calls.append(kwargs)

        agent.state.update(
            {
                "mini": types.SimpleNamespace(media=FakeMedia(), set_target=set_target),
                "np": np,
                "motion_lock": asyncio.Lock(),
            }
        )
        await NS["_conversation_idle_antenna_target"](agent, 8, 12)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {"antennas"})


    async def test_confirmed_speech_stops_idle_motors_immediately(self):
        agent = FakeAgent()

        async def moving():
            await asyncio.Future()

        idle_task = asyncio.create_task(moving())
        await asyncio.sleep(0)
        session = {
            "cancel_event": types.SimpleNamespace(is_set=lambda: False),
            "worker": None,
            "_idle_motion_task": idle_task,
        }

        def fake_capture(_media, _cancel, _config, on_speech_start):
            on_speech_start()
            return captured()

        with mock.patch(
            "wactorz.catalogue_agents.reachy_vad.capture_utterance", fake_capture
        ):
            await NS["_conversation_capture"](agent, session, object())
        await asyncio.gather(idle_task, return_exceptions=True)

    async def test_idle_sweep_is_fluid_and_antenna_only(self):
        agent, calls = FakeAgent(), []

        def set_target(**kwargs):
            calls.append(kwargs)

        agent.state.update(
            {
                "mini": types.SimpleNamespace(media=FakeMedia(), set_target=set_target),
                "np": np,
                "motion_lock": asyncio.Lock(),
            }
        )
        session = {
            "state": "listening",
            "cancel_event": types.SimpleNamespace(is_set=lambda: False),
        }
        with mock.patch.object(NS["asyncio"], "sleep", mock.AsyncMock()):
            await NS["_conversation_idle_sweep"](
                agent, session, (0.0, 0.0), (5.0, 7.0), duration=1.12
            )
        angles = np.rad2deg(np.stack([call["antennas"] for call in calls]))
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(set(call) == {"antennas"} for call in calls))
        self.assertLess(float(np.max(np.abs(np.diff(angles, axis=0)))), 2.0)
        np.testing.assert_allclose(angles[-1], [7.0, 5.0], atol=0.01)
    async def test_idle_motion_is_off_by_default(self):
        agent = FakeAgent()
        session = {"payload": {}}
        NS["_schedule_conversation_idle_motion"](agent, session, "listening")
        self.assertIsNone(session["_idle_motion_task"])



    async def test_dance_choreography_sends_complete_safe_poses(self):
        agent, calls = FakeAgent(), []

        def goto_target(**kwargs):
            calls.append(kwargs)

        agent.state.update(
            {
                "mini": types.SimpleNamespace(media=FakeMedia(), goto_target=goto_target),
                "np": np,
                "create_head_pose": lambda **kwargs: kwargs,
                "motion_lock": asyncio.Lock(),
            }
        )

        with mock.patch.object(NS["asyncio"], "sleep", mock.AsyncMock()):
            result = await NS["_gesture"](
                agent, {"name": "dance", "duration": 0.12}
            )

        self.assertEqual(result["gesture"], "dance")
        self.assertEqual(len(calls), 6)
        required = {"head", "antennas", "body_yaw", "duration"}
        self.assertTrue(all(set(call) == required for call in calls))


if __name__ == "__main__":
    unittest.main()
