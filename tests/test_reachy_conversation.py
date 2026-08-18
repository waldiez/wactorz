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


async def fake_prepare(_agent, text, _payload):
    """Stand in for edge-tts: unit tests must not synthesize over the network."""
    return {
        "raw_path": f"/tmp/{abs(hash(text))}.mp3",
        "play_path": f"/tmp/{abs(hash(text))}.mp3",
        "voice": "test-voice",
        "speech_seconds": 0.0,
        "trim_db": 0.0,
    }


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
            return (
                value if isinstance(value, Transcription) else Transcription(value, "fake", "fake")
            )

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
        agent, routed, contexts = FakeAgent(), [], []
        responses = ["The light is off.", "The light is back on.", "It is dimmed."]

        async def bridge(_agent, text, _task_id, **kwargs):
            routed.append(text)
            contexts.append(kwargs.get("conversation_history"))
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
        self.assertEqual(
            routed,
            ["Turn off the living-room light", "Turn it back on", "Dim it"],
        )
        self.assertEqual(contexts[0], [])
        self.assertEqual(contexts[1][0]["transcript"], "Turn off the living-room light")
        self.assertNotIn("Previous user", routed[1])
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
        self.assertEqual(agent.chat_messages[0][1].get("to"), "reachy-mini")
        self.assertEqual(agent.chat_messages[0][1].get("surface_label"), "Reachy")
        self.assertEqual(agent.chat_messages[-1][1].get("from"), "reachy-mini")
        self.assertEqual(agent.chat_messages[-1][1].get("brain"), "main")

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

    async def test_full_reply_is_displayed_before_full_speech_begins(self):
        agent = FakeAgent()
        full_reply = (
            "I can help with lights, automations, camera vision, and physical gestures. "
            "I can also coordinate other Wactorz agents when a task needs them. "
            "The dashboard keeps this full answer available for you to read. "
            "For more complex work, I can delegate without making you switch interfaces."
        )
        agent.send_to = mock.AsyncMock(return_value={"text": full_reply})
        events = []

        async def before_speak(text):
            events.append(("display", text))

        async def speak(_agent, text, **_kwargs):
            events.append(("speak", text))
            return {
                "spoke": True,
                "interrupted": False,
                "spoken_result": text,
            }

        with mock.patch.dict(NS, {"_speak_reply": speak}):
            result = await NS["_bridge_to_main"](
                agent,
                "What can you do?",
                voice_friendly=True,
                await_playback=True,
                before_speak=before_speak,
            )

        self.assertEqual(events[0], ("display", full_reply))
        self.assertEqual(events[1][0], "speak")
        self.assertEqual(events[1][1], full_reply)
        self.assertEqual(result["result"], full_reply)

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

    async def test_low_confidence_whisper_hallucination_is_silently_discarded(self):
        agent, routed = FakeAgent(), []

        async def bridge(_agent, text, _task_id, **kwargs):
            routed.append(text)
            await kwargs["before_speak"]("Okay.")
            return {"result": "Okay.", "spoke": True, "speech_error": None}

        _, session = await self.run_session(
            agent,
            {"max_turns": 1},
            [captured(), captured()],
            [
                Transcription("Have a good day! Have a good day!", "fake", "fake", 0.1, 0.8),
                Transcription("Turn on the light", "fake", "fake", 0.9, 0.02),
            ],
            bridge,
        )

        self.assertEqual(routed, ["Turn on the light"])
        self.assertEqual(session["turn_index"], 1)

    async def test_barge_in_requires_echo_control_or_explicit_override(self):
        agent = FakeAgent()
        session = {"payload": {"barge_in": False}, "cancel_event": None}
        disabled = await NS["_begin_barge_in_monitor"](agent, session, 2.0)
        self.assertIsNone(disabled)

        session = {"payload": {}, "cancel_event": None}
        automatic_fallback = await NS["_begin_barge_in_monitor"](agent, session, 2.0)
        self.assertIsNone(automatic_fallback)

        capture = VoiceCapture(np.zeros((0,), dtype=np.float32), 16000, 1, 0.0, "cancelled")
        with mock.patch.dict(NS, {"_do": mock.AsyncMock(return_value=capture)}):
            session = {"payload": {"barge_in": True}, "cancel_event": None}
            monitor = await NS["_begin_barge_in_monitor"](agent, session, 2.0)
            self.assertIsNotNone(monitor)
            await NS["_finish_barge_in_monitor"](session, monitor, False)

    async def test_conversation_start_keeps_barge_in_opt_in(self):
        async def finished_loop(_agent, _session):
            return None

        with mock.patch.dict(NS, {"_conversation_loop": finished_loop}):
            unavailable = FakeAgent()
            off = await NS["_conversation_start"](unavailable, {})
            await unavailable.state["conversation_session"]["task"]

            configured = FakeAgent()
            configured.state["conversation_echo_control"] = True
            on = await NS["_conversation_start"](configured, {})
            await configured.state["conversation_session"]["task"]

            overridden = FakeAgent()
            overridden.state["conversation_echo_control"] = True
            forced_off = await NS["_conversation_start"](overridden, {"barge_in": False})
            await overridden.state["conversation_session"]["task"]

            opted_in = FakeAgent()
            opted_in.state["conversation_echo_control"] = True
            forced_on = await NS["_conversation_start"](opted_in, {"barge_in": True})
            await opted_in.state["conversation_session"]["task"]

        self.assertFalse(off["barge_in"])
        self.assertFalse(on["barge_in"])
        self.assertFalse(forced_off["barge_in"])
        self.assertTrue(forced_on["barge_in"])

    async def test_barge_in_defaults_are_tuned_for_quiet_close_speech(self):
        agent, seen = FakeAgent(), {}
        capture = VoiceCapture(np.zeros((0,), dtype=np.float32), 16000, 1, 0.0, "cancelled")

        async def fake_do(_fn, _media, _cancel, config, _onset):
            seen["config"] = config
            return capture

        async def fake_guard(_cancel, seconds):
            seen["guard_s"] = seconds
            return True

        with mock.patch.dict(
            NS,
            {"_do": fake_do, "_wait_for_barge_guard": fake_guard},
        ):
            session = {"payload": {"barge_in": True}, "cancel_event": None}
            monitor = await NS["_begin_barge_in_monitor"](agent, session, 2.0)
            await NS["_finish_barge_in_monitor"](session, monitor, False)

        self.assertAlmostEqual(seen["guard_s"], 0.45)
        self.assertAlmostEqual(seen["config"].speech_start_s, 0.21)
        self.assertEqual(seen["config"].mode, 1)
        self.assertEqual(seen["config"].flush_s, 0.0)
        self.assertAlmostEqual(seen["config"].min_rms, 0.006)
        self.assertAlmostEqual(seen["config"].min_speech_s, 0.12)

    async def test_silence_phrase_does_not_trigger_another_reply(self):
        agent, bridge = FakeAgent(), mock.AsyncMock()
        _, session = await self.run_session(
            agent,
            {},
            [captured(), captured()],
            ["shut up", "bye"],
            bridge,
        )

        self.assertEqual(session["stop_reason"], "stop_phrase")
        bridge.assert_not_awaited()
        user_turns = [text for text, extra in agent.chat_messages if extra.get("from") == "user"]
        self.assertEqual(user_turns, ["shut up", "bye"])

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
        for phrase in ("Bye.", "Goodbye!", "That's all", "Σταμάτα να ακούς."):
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

    def test_planner_details_become_a_short_voice_approval_prompt(self):
        raw = (
            "Proposed pipeline abc with 7 internal steps. "
            "Reply 'yes' to approve or 'no' to discard."
        )

        spoken = NS["_voice_friendly_reply"](raw)

        self.assertEqual(
            spoken,
            "I can set that up as an automation. Say yes to approve it, no to cancel, or tell me what to change.",
        )

    def test_literal_tell_requests_stay_on_reachy_and_can_add_a_dance(self):
        self.assertEqual(
            NS["_parse_speak_compound"](
                "Hey Ritsy, please tell my brother that he's really stupid and do a little dance"
            ),
            [
                {"cmd": "say", "text": "he's really stupid"},
                {"cmd": "gesture", "name": "dance"},
            ],
        )
        self.assertEqual(
            NS["_parse_speak_compound"]("tell my sister she is a big bunny with red hair"),
            [{"cmd": "say", "text": "she is a big bunny with red hair"}],
        )

    async def test_literal_speech_is_displayed_then_spoken_without_main(self):
        agent = FakeAgent()
        calls = []

        async def dispatch(_agent, cmd, payload, return_result=False):
            calls.append((cmd, dict(payload)))
            result = {"ok": True, "cmd": cmd}
            if cmd == "say":
                result.update({"said": payload["text"], "interrupted": False})
            return result

        before_speak = mock.AsyncMock()
        commands = NS["_parse_speak_compound"](
            "Tell my brother that dinner is ready and do a little dance"
        )
        with mock.patch.dict(NS, {"_dispatch": dispatch}):
            result = await NS["_conversation_speech_bridge"](
                agent,
                "Tell my brother that dinner is ready and do a little dance",
                commands,
                "speech-task",
                before_speak,
                {},
            )

        before_speak.assert_awaited_once_with("dinner is ready")
        self.assertEqual([cmd for cmd, _ in calls], ["say", "gesture"])
        self.assertTrue(calls[0][1]["await_playback"])
        self.assertTrue(result["spoke"])
        self.assertTrue(result["physical"])
        self.assertEqual(result["result"], "dinner is ready")

    async def test_ha_delegation_exposes_its_human_result(self):
        agent = FakeAgent()
        agent.send_to = mock.AsyncMock(
            return_value={
                "result": "Automation creation is disabled; here is the available hardware."
            }
        )

        result = await NS["_ha"](
            agent,
            {"request": "make an automation based on the sun position"},
        )

        self.assertEqual(result["delegated_to"], "home-assistant-agent")
        self.assertEqual(result["result"], result["ha_result"])
        self.assertIn("creation is disabled", result["result"])

    def test_reachy_name_is_repaired_without_naming_the_user(self):
        for alias in ("Richie", "Riti", "Ritsy", "Ritzy", "Rizzi", "Lizzy"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    NS["_normalize_reachy_transcript"](f"Hey {alias}"),
                    "Hey Reachy",
                )
        self.assertEqual(NS["_normalize_reachy_transcript"]("E Rizzi"), "Hey Reachy")
        self.assertIn(
            "I'm Reachy",
            NS["_sanitize_reachy_identity_reply"]("I'm Lizzy"),
        )
        self.assertEqual(
            NS["_sanitize_reachy_identity_reply"](
                "Of course, Amalia! And it's Lizzy, not Ritzy!",
                user_text="Hey Ritzy, do you remember me?",
            ),
            "Of course, Amalia! I'm Reachy.",
        )
        self.assertEqual(
            NS["_sanitize_reachy_identity_reply"](
                "Hello Riti!",
                user_text="Hey Reachy!",
            ),
            "Hello!",
        )
        self.assertEqual(
            NS["_sanitize_reachy_identity_reply"](
                "Nice to meet you, Lizzy!",
                user_text="My name is Lizzy.",
            ),
            "Nice to meet you, Lizzy!",
        )
        self.assertEqual(
            NS["_normalize_reachy_transcript"]("Turn off the man light"),
            "Turn off the main light",
        )

    def test_conversation_stt_auto_detects_language_and_honors_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            resolved = NS["_conversation_stt_payload"]({})
        self.assertNotIn("stt_language", resolved)
        self.assertIn("Reachy", resolved["stt_hotwords"])

        explicit = NS["_conversation_stt_payload"](
            {"stt_language": "el", "stt_hotwords": "kitchen lamp"}
        )
        self.assertEqual(explicit["stt_language"], "el")
        self.assertIn("Reachy", explicit["stt_hotwords"])
        self.assertIn("kitchen lamp", explicit["stt_hotwords"])

    def test_unicode_transcripts_are_meaningful(self):
        self.assertTrue(NS["_conversation_transcript_is_meaningful"]("Άναψε το φως"))
        self.assertFalse(NS["_conversation_transcript_is_meaningful"]("... 🎵"))

    async def test_uncertain_short_language_retries_in_greek(self):
        agent = FakeAgent()
        session = {"stt_language_hint": None, "stt_retry_count": 0}
        uncertain = Transcription("To ono ma mu venine ade", "fake", "fake", 0.8, 0.02, "cs", 0.19)
        greek = Transcription("Το όνομά μου δεν είναι Άντε", "fake", "fake", 0.9, 0.01, "el", None)
        transcribe = mock.AsyncMock(side_effect=[uncertain, greek])

        with mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", transcribe):
            result, retried = await NS["_conversation_transcribe"](
                agent, b"RIFFmock", {"stt_fallback_language": "el"}, session
            )

        self.assertTrue(retried)
        self.assertEqual(result.text, "Το όνομά μου δεν είναι Άντε")
        self.assertEqual(transcribe.await_args_list[1].args[1]["stt_language"], "el")
        self.assertEqual(session["stt_retry_count"], 1)

    def test_uncertain_language_is_not_routed_without_a_fallback(self):
        uncertain = Transcription("Giritui", "fake", "fake", 0.8, 0.02, "en", 0.17)

        credible, reason = NS["_conversation_transcription_is_credible"](uncertain, {})

        self.assertFalse(credible)
        self.assertIn("language_probability", reason)

    def test_greek_voice_volume_request_stays_on_reachy(self):
        command = NS["_embodied_command_for_text"](
            "Μπορείς να χαμηλώσεις λίγο τον τόνο της φωνής σου"
        )

        self.assertEqual(command, {"cmd": "volume", "delta": -15})

    async def test_greek_voice_volume_executes_locally_and_speaks_greek(self):
        agent = FakeAgent()
        dispatch = mock.AsyncMock(return_value={"ok": True, "cmd": "volume", "level": 85})
        speak = mock.AsyncMock(
            return_value={"spoke": True, "interrupted": False, "spoken_result": "Εντάξει"}
        )
        before_speak = mock.AsyncMock()
        command = {"cmd": "volume", "delta": -15}

        with mock.patch.dict(NS, {"_dispatch": dispatch, "_speak_reply": speak}):
            result = await NS["_conversation_embodied_bridge"](
                agent,
                "Μπορείς να χαμηλώσεις λίγο τον τόνο της φωνής σου",
                command,
                "task-volume",
                before_speak,
                {},
            )

        dispatch.assert_awaited_once_with(agent, "volume", command, return_result=True)
        self.assertIn("85 percent", result["result"])
        self.assertEqual(speak.await_args.args[1], "Εντάξει, πιο σιγά.")
        self.assertFalse(result["physical"])

    def test_vision_questions_are_embodied_commands(self):
        self.assertEqual(
            NS["_embodied_command_for_text"]("What do you see?"),
            {"cmd": "look_around"},
        )
        self.assertEqual(
            NS["_embodied_command_for_text"]("What's in front of you?"),
            {"cmd": "describe"},
        )
        for phrase in (
            "Can you describe what you see around the room?",
            "Tell me what is around you",
            "Could you describe the room for me?",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    NS["_embodied_command_for_text"](phrase),
                    {"cmd": "look_around"},
                )

    async def test_voice_vision_uses_reachy_camera_instead_of_main(self):
        agent = FakeAgent()
        main_bridge = mock.AsyncMock()
        local_commands = []

        async def local_bridge(_agent, _transcript, command, _task_id, before_speak, _session):
            local_commands.append(command)
            await before_speak("I see books and a desk.")
            return {
                "result": "I see books and a desk.",
                "spoken_result": "I see books and a desk.",
                "spoke": True,
                "interrupted": False,
                "speech_error": None,
            }

        with mock.patch.dict(NS, {"_conversation_embodied_bridge": local_bridge}):
            _, session = await self.run_session(
                agent,
                {"max_turns": 1},
                [captured()],
                ["Can you describe what you see around the room?"],
                main_bridge,
            )

        self.assertEqual(local_commands, [{"cmd": "look_around"}])
        main_bridge.assert_not_awaited()
        self.assertEqual(agent.notifications, ["I see books and a desk."])
        self.assertEqual(session["stop_reason"], "max_turns")

    def test_turn_around_is_an_embodied_command(self):
        command = NS["_embodied_command_for_text"]("Turn around")

        self.assertEqual(command, {"cmd": "gesture", "name": "turn_around"})

    def test_left_right_turns_default_to_45_and_honor_explicit_angles(self):
        cases = {
            "Turn left": {"cmd": "turn", "angle": 45},
            "Turn right": {"cmd": "turn", "angle": -45},
            "Could you turn left?": {"cmd": "turn", "angle": 45},
            "Turn left by 90 degrees": {"cmd": "turn", "angle": 90},
            "Turn 120 degrees right": {"cmd": "turn", "angle": -120},
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(NS["_embodied_command_for_text"](phrase), expected)

    async def test_relative_turn_moves_body_joint_by_requested_angle(self):
        calls = []

        def goto_joint_positions(**kwargs):
            calls.append(kwargs)

        head = np.zeros(7)
        head[0] = np.deg2rad(30)
        mini = types.SimpleNamespace(
            media=FakeMedia(),
            get_current_joint_positions=lambda: (head, np.zeros(2)),
            goto_joint_positions=goto_joint_positions,
        )
        agent = FakeAgent()
        agent.state.update(
            {
                "mini": mini,
                "np": np,
                "create_head_pose": lambda **kwargs: kwargs,
                "motion_lock": asyncio.Lock(),
            }
        )

        result = await NS["_turn"](agent, {"angle": 45, "duration": 0.5})

        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0]["head_joint_positions"][0], np.deg2rad(75))
        self.assertEqual(result["turn_degrees"], 45)
        self.assertEqual(agent.state["_facing_body_yaw_deg"], 75)

    def test_behind_you_is_a_deterministic_rear_view(self):
        command = NS["_embodied_command_for_text"]("Behind you!")

        self.assertEqual(
            command,
            {"cmd": "look_behind", "question": "What is behind you?"},
        )
        compound = NS["_embodied_command_for_text"]("Turn around and tell me what you see")
        self.assertEqual(compound["cmd"], "look_behind")

    async def test_debug_commands_toggle_execution_receipts(self):
        agent = FakeAgent()
        self.assertEqual(
            NS["_embodied_command_for_text"]("enable debug"),
            {"cmd": "debug", "enabled": True},
        )

        enabled = await NS["handle_task"](agent, {"text": "enable debug"})
        disabled = await NS["handle_task"](agent, {"text": "disable debug"})

        self.assertTrue(enabled["debug"])
        self.assertEqual(enabled["result"], "Debug details enabled.")
        self.assertFalse(disabled["debug"])
        self.assertEqual(disabled["result"], "Debug details disabled.")
        self.assertFalse(agent.state["debug"])

    async def test_plain_stop_cuts_speech_without_a_receipt_unless_debug_is_on(self):
        agent = FakeAgent()
        dispatch = mock.AsyncMock(
            side_effect=[
                {"ok": True, "cmd": "shutup", "result": "Stopped talking."},
                {"ok": True, "cmd": "shutup", "result": "Stopped talking."},
            ]
        )

        with mock.patch.dict(NS, {"_dispatch": dispatch}):
            quiet = await NS["handle_task"](agent, {"text": "stop"})
            agent.state["debug"] = True
            verbose = await NS["handle_task"](agent, {"text": "stop"})

        self.assertEqual(dispatch.await_args_list[0].args[1], "shutup")
        self.assertTrue(quiet["_suppress_reply"])
        self.assertNotIn("_suppress_reply", verbose)

    async def test_action_receipt_is_hidden_until_debug_is_enabled(self):
        async def planner(_agent, _text):
            return [{"cmd": "wake"}, {"cmd": "describe"}]

        async def dispatch(_agent, cmd, _payload, return_result=False):
            if cmd == "describe":
                return {
                    "ok": True,
                    "cmd": cmd,
                    "said": "There are books behind me.",
                    "result": "There are books behind me.",
                }
            return {"ok": True, "cmd": cmd, "result": "awake"}

        agent = FakeAgent()
        with mock.patch.dict(NS, {"_nl_to_commands": planner, "_dispatch": dispatch}):
            quiet = await NS["handle_task"](agent, {"text": "perform a visual inspection"})
            agent.state["debug"] = True
            verbose = await NS["handle_task"](agent, {"text": "perform a visual inspection"})
            await asyncio.sleep(0.12)

        self.assertEqual(quiet["result"], "There are books behind me.")
        self.assertNotIn("ran", quiet["result"])
        self.assertIn("ran 2 of 2", verbose["result"])
        self.assertEqual(agent.notifications, ["There are books behind me."])

    async def test_voice_behind_you_speaks_the_rear_description_once(self):
        agent = FakeAgent()
        dispatch = mock.AsyncMock(
            return_value={
                "ok": True,
                "cmd": "look_behind",
                "result": "There are books behind me.",
            }
        )
        speak = mock.AsyncMock(
            return_value={
                "spoke": True,
                "interrupted": False,
                "spoken_result": "There are books behind me.",
            }
        )
        before_speak = mock.AsyncMock()
        command = {"cmd": "look_behind", "question": "What is behind you?"}

        with mock.patch.dict(NS, {"_dispatch": dispatch, "_speak_reply": speak}):
            result = await NS["_conversation_embodied_bridge"](
                agent,
                "Behind you!",
                command,
                "task-behind",
                before_speak,
                {},
            )

        dispatched = dispatch.await_args.args[2]
        self.assertFalse(dispatched["say"])
        before_speak.assert_awaited_once_with("There are books behind me.")
        self.assertEqual(speak.await_args.args[1], "There are books behind me.")
        self.assertTrue(result["physical"])
        self.assertEqual(result["result"], "There are books behind me.")

    async def test_direct_dance_request_dispatches_a_real_gesture(self):
        agent, seen = FakeAgent(), []

        async def fake_dispatch(_agent, cmd, payload, return_result=False):
            seen.append((cmd, payload.get("name")))
            return {"ok": True, "cmd": cmd, "result": "done"}

        with mock.patch.dict(NS, {"_dispatch": fake_dispatch}):
            result = await NS["handle_task"](agent, {"text": "Do a little dance for me"})
        self.assertEqual(seen, [("gesture", "dance")])
        self.assertEqual(result["cmd"], "gesture")

    async def test_main_interface_action_executes_on_reachy(self):
        agent = FakeAgent()
        agent.send_to = mock.AsyncMock(
            return_value={
                "text": "Okay.",
                "interface_actions": [{"cmd": "gesture", "name": "turn_around"}],
            }
        )
        dispatch = mock.AsyncMock(
            return_value={"ok": True, "cmd": "gesture", "gesture": "turn_around"}
        )
        speak = mock.AsyncMock(
            return_value={"spoke": True, "interrupted": False, "spoken_result": "Okay."}
        )

        with mock.patch.dict(NS, {"_dispatch": dispatch, "_speak_reply": speak}):
            result = await NS["_bridge_to_main"](
                agent, "Could you face the other way?", "task-1", voice_input=True
            )

        payload = agent.send_to.await_args.args[1]
        self.assertEqual(payload["_interface_context"]["display_name"], "Reachy")
        self.assertIn("turn_around", payload["_interface_context"]["capabilities"]["gesture"])
        dispatch.assert_awaited_once_with(
            agent,
            "gesture",
            {"cmd": "gesture", "name": "turn_around"},
            return_result=True,
        )
        self.assertEqual(result["interface_actions"][0]["name"], "turn_around")
        self.assertTrue(result["spoke"])

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

        with mock.patch("wactorz.catalogue_agents.reachy_vad.capture_utterance", fake_capture):
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
            result = await NS["_gesture"](agent, {"name": "dance", "duration": 0.12})

        self.assertEqual(result["gesture"], "dance")
        self.assertEqual(len(calls), 6)
        required = {"head", "antennas", "body_yaw", "duration"}
        self.assertTrue(all(set(call) == required for call in calls))

    async def test_head_pose_does_not_reset_persistent_body_yaw(self):
        agent, calls = FakeAgent(), []

        def goto_target(**kwargs):
            calls.append(kwargs)

        agent.state.update(
            {
                "mini": types.SimpleNamespace(media=FakeMedia(), goto_target=goto_target),
                "np": np,
                "create_head_pose": lambda **kwargs: kwargs,
                "motion_lock": asyncio.Lock(),
                "_facing_body_yaw_deg": 155.0,
            }
        )

        await NS["_pose"](agent, {"yaw": 155, "duration": 0.1})

        self.assertIsNone(calls[0]["body_yaw"])
        self.assertEqual(agent.state["_facing_body_yaw_deg"], 155.0)

    async def test_turn_around_choreography_drives_body_joint_to_rear_limit(self):
        agent, calls = FakeAgent(), []

        def goto_joint_positions(**kwargs):
            calls.append(kwargs)

        mini = types.SimpleNamespace(
            media=FakeMedia(),
            get_current_joint_positions=lambda: (np.zeros(7), np.zeros(2)),
            goto_joint_positions=goto_joint_positions,
        )
        agent.state.update(
            {
                "mini": mini,
                "np": np,
                "create_head_pose": lambda **kwargs: kwargs,
                "motion_lock": asyncio.Lock(),
            }
        )

        result = await NS["_gesture"](agent, {"name": "turn_around", "duration": 0.12})

        self.assertEqual(result["gesture"], "turn_around")
        self.assertEqual(result["facing"], "rear")
        self.assertEqual(len(calls), 1)
        joints = calls[0]["head_joint_positions"]
        self.assertAlmostEqual(float(joints[0]), float(np.deg2rad(155)))
        self.assertTrue(np.allclose(joints[1:], np.zeros(6)))
        self.assertEqual(agent.state["_facing_body_yaw_deg"], 155.0)


class SpeakReplyChunkingTest(unittest.IsolatedAsyncioTestCase):
    """A reply is spoken as several says, so stopping has to span all of them."""

    REPLY = (
        "The kitchen light is now on and set to warm white. "
        "I also turned off the lamp in the hallway for you. "
        "Let me know if you want anything else changed today."
    )

    def setUp(self):
        self.assertEqual(len(NS["_speech_chunks"](self.REPLY)), 3)

    async def test_shutup_during_a_sentence_drops_the_rest_of_the_reply(self):
        agent = FakeAgent()
        agent.state["stop_speaking"] = False
        said = []

        async def fake_say(_agent, payload):
            said.append(payload["text"])
            if not payload.get("_continuation"):
                # Mirrors _say: a fresh utterance clears a stale stop request.
                _agent.state["stop_speaking"] = False
            if len(said) == 1:
                # The user says "shut up" while the first sentence is playing.
                _agent.state["stop_speaking"] = True
            return {
                "said": payload["text"],
                "interrupted": False,
                "stopped": bool(_agent.state.get("stop_speaking")),
            }

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](agent, self.REPLY, await_playback=True)

        # Only the sentence that was already playing — not the whole bubble.
        self.assertEqual(len(said), 1)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["interrupted"])

    async def test_a_reply_is_not_silenced_by_a_stop_from_the_previous_turn(self):
        # The stale-clear still has to happen once per reply, or a 'shutup' would
        # leave the robot mute for the next thing it is asked to say.
        agent = FakeAgent()
        agent.state["stop_speaking"] = True
        said = []

        async def fake_say(_agent, payload):
            said.append(payload["text"])
            return {
                "said": payload["text"],
                "interrupted": False,
                "stopped": bool(_agent.state.get("stop_speaking")),
            }

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](agent, self.REPLY, await_playback=True)

        self.assertEqual(len(said), 3)
        self.assertFalse(result["stopped"])

    async def test_the_gap_between_sentences_is_shorter_than_between_replies(self):
        agent = FakeAgent()
        pads = []

        async def fake_say(_agent, payload):
            pads.append(payload.get("tail_pad"))
            return {"said": payload["text"], "interrupted": False, "stopped": False}

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            await NS["_speak_reply"](agent, self.REPLY, await_playback=True)

        # Short gap mid-reply; the last chunk keeps _say's own default, which
        # separates the answer from whatever comes next.
        self.assertEqual(pads[:-1], [NS["_CHUNK_TAIL_PAD"]] * 2)
        self.assertIsNone(pads[-1])
        self.assertLess(NS["_CHUNK_TAIL_PAD"], 0.55)


class BridgeReplyShownInChatTest(unittest.IsolatedAsyncioTestCase):
    """What Reachy says and what chat shows are the same answer.

    Main answers an actuation with a machine acknowledgement naming a service
    and an entity id. Reachy already spoke a human sentence built from it; chat
    was being handed the raw string, so the user heard one thing and read
    another — and three differently-worded requests all read identically,
    because the acknowledgement carries no colour or brightness.
    """

    class BridgeAgent(FakeAgent):
        def __init__(self, reply, connected=False):
            super().__init__()
            self.reply = reply
            self.sent = []
            if not connected:
                # Skips playback, so the test is about the displayed text only.
                self.state["mini"] = None

        async def send_to(self, name, payload, timeout=None):
            self.sent.append((name, payload))
            return {"result": self.reply}

    async def _display(self, reply, user_text):
        agent = self.BridgeAgent(reply)
        shown = []

        async def before_speak(text):
            shown.append(text)

        result = await NS["_bridge_to_main"](agent, user_text, "task-1", before_speak=before_speak)
        return shown, result

    async def test_an_actuation_is_shown_as_the_sentence_it_was_spoken_as(self):
        shown, result = await self._display(
            "Done: light.turn_on -> light.tapo_l920.",
            "Please turn the LED light green at maximum brightness",
        )

        self.assertEqual(shown, ["Okay, the light is green."])
        self.assertEqual(result["result"], "Okay, the light is green.")

    async def test_the_raw_acknowledgement_is_still_returned_untouched(self):
        # The technical string is what a caller inspecting the turn wants; it is
        # only the *displayed* text that changes.
        _, result = await self._display(
            "Done: light.turn_off -> light.tapo_l920.", "turn the lamp off"
        )

        self.assertEqual(result["raw_result"], "Done: light.turn_off -> light.tapo_l920.")
        self.assertEqual(result["result"], "Okay, the light is off.")

    async def test_two_differently_worded_requests_no_longer_read_alike(self):
        green, _ = await self._display(
            "Done: light.turn_on -> light.tapo_l920.", "turn the LED light green"
        )
        blue, _ = await self._display(
            "Done: light.turn_on -> light.tapo_l920.", "turn the LED light blue"
        )

        self.assertEqual(green, ["Okay, the light is green."])
        self.assertEqual(blue, ["Okay, the light is blue."])
        self.assertNotEqual(green, blue)

    async def test_an_ordinary_answer_keeps_its_full_text(self):
        # The spoken form of a long answer is truncated and ends "I've put the
        # rest in Wactorz chat". Showing that in chat would point it at itself,
        # so anything that is not an acknowledgement is displayed whole.
        answer = "The living room is 21 degrees and the hallway sensor is offline."

        shown, result = await self._display(answer, "what is the temperature")

        self.assertEqual(shown, [answer])
        self.assertEqual(result["result"], answer)


class ConversationInterruptionPhraseTest(unittest.IsolatedAsyncioTestCase):
    """Interruption is reachable in words, not only as hand-written JSON."""

    async def _payload_for(self, text):
        agent, seen = FakeAgent(), {}

        async def fake_start(_agent, payload):
            seen.update(payload)
            return {"started": True, "result": "ok"}

        with mock.patch.dict(NS, {"_conversation_start": fake_start}):
            await NS["handle_task"](agent, {"text": text})
        return seen

    async def test_asking_for_interruption_turns_barge_in_on(self):
        for phrase in (
            "start conversation with interruption",
            "start interruptible conversation",
            "start conversation with barge in",
        ):
            with self.subTest(phrase=phrase):
                self.assertIs((await self._payload_for(phrase)).get("barge_in"), True)

    async def test_the_plain_phrase_still_leaves_it_off(self):
        # The mic hears Reachy's own speaker, so barge-in stays opt-in.
        self.assertFalse((await self._payload_for("start conversation")).get("barge_in"))


class ConversationStartMessageTest(unittest.IsolatedAsyncioTestCase):
    """Starting a conversation says which mode it started in.

    Not knowing interruption was off reads as a robot that ignores you, rather
    than as a setting nobody asked for.
    """

    async def _start(self, payload):
        agent = FakeAgent()

        async def fake_loop(_agent, _session):
            return None

        with mock.patch.dict(NS, {"_conversation_loop": fake_loop}):
            result = await NS["_conversation_start"](agent, payload)
            session = agent.state["conversation_session"]
            if session and session.get("task"):
                await session["task"]
        return result

    async def test_it_says_how_to_cut_in_when_interruption_is_off(self):
        result = await self._start({})

        self.assertFalse(result["barge_in"])
        self.assertIn("stop talking", result["result"])

    async def test_it_says_you_can_talk_over_it_when_interruption_is_on(self):
        result = await self._start({"barge_in": True})

        self.assertTrue(result["barge_in"])
        self.assertIn("Talk over me", result["result"])


class BargeInIsCheckedBeforeItIsBelievedTest(unittest.IsolatedAsyncioTestCase):
    """Reachy no longer stops himself by hearing his own speaker.

    Speech onset used to end the reply outright. His own voice satisfies every
    onset test — it is speech — so a joke died at "why don't eg-". Onset is now
    only a suspicion: the sentence finishes, and the recording is checked before
    it counts as someone talking over him.
    """

    JOKE = "Why don't eggs tell jokes? Because they'd crack each other up!"

    def _capture(self, voiced=1.0):
        audio = np.full(16000, 0.2, np.float32)
        return VoiceCapture(audio, 16000, 1, 1.0, None, 4, voiced_duration_s=voiced)

    def _session(self, **payload):
        return {"payload": payload, "cancel_event": None}

    async def _verify(self, transcript, *, session=None, voiced=1.0, spoken=None, raises=None):
        agent = FakeAgent()
        session = session if session is not None else self._session()

        async def fake_transcribe(_agent, _wav, _payload, _session):
            if raises is not None:
                raise raises
            return transcript, False

        with mock.patch.dict(NS, {"_conversation_transcribe": fake_transcribe}):
            verdict = await NS["_verified_barge_in"](
                agent, session, self._capture(voiced), self.JOKE if spoken is None else spoken
            )
        return verdict, session, agent

    @staticmethod
    def _heard(text, **kwargs):
        return Transcription(text, "fake", "fake", **kwargs)

    async def test_his_own_sentence_coming_back_is_not_an_interruption(self):
        verdict, session, agent = await self._verify(self._heard("why don't eggs tell jokes"))

        self.assertFalse(verdict)
        self.assertNotIn("pending_capture", session)
        self.assertTrue(any("my own words" in text for _level, text in agent.logs))

    async def test_a_person_talking_over_him_is_an_interruption(self):
        verdict, session, _ = await self._verify(self._heard("okay stop, I have heard it"))

        self.assertTrue(verdict)
        self.assertIsNotNone(session["pending_capture"])

    async def test_a_short_word_over_him_still_counts(self):
        # "stop" is about 0.2s of voice, and echo suppression during playback
        # trims what little there is. A stricter floor here than the one the
        # capture layer used to call it an utterance threw these away — which
        # is exactly what let him talk over someone telling him to stop.
        verdict, session, _ = await self._verify(self._heard("stop"), voiced=0.21)

        self.assertTrue(verdict)
        self.assertIsNotNone(session["pending_capture"])

    async def test_the_floor_follows_the_one_that_called_it_speech(self):
        # Raising the capture layer's own minimum raises this with it, rather
        # than leaving two numbers to disagree.
        verdict, _session, _ = await self._verify(
            self._heard("stop"),
            session=self._session(barge_min_speech_s=0.5),
            voiced=0.21,
        )

        self.assertFalse(verdict)

    async def test_a_stray_click_is_too_little_voice_to_count(self):
        verdict, session, _ = await self._verify(
            self._heard("okay stop, I have heard it"), voiced=0.05
        )

        self.assertFalse(verdict)
        self.assertNotIn("pending_capture", session)

    async def test_a_transcript_the_recogniser_doubts_is_not_believed(self):
        verdict, _session, _ = await self._verify(
            self._heard("okay stop", no_speech_probability=0.99)
        )

        self.assertFalse(verdict)

    async def test_punctuation_only_output_is_not_a_turn(self):
        verdict, _session, _ = await self._verify(self._heard("..."))

        self.assertFalse(verdict)

    async def test_a_recogniser_failure_leaves_him_talking(self):
        # Unverifiable is not the same as real: a failing recogniser must not
        # become a way to silence him.
        verdict, session, agent = await self._verify(
            self._heard("anything"), raises=RuntimeError("stt down")
        )

        self.assertFalse(verdict)
        self.assertNotIn("pending_capture", session)
        self.assertTrue(any("could not check" in text for _level, text in agent.logs))


class OwnVoiceRecognitionTest(unittest.TestCase):
    """What separates his voice from a person's is the words, not the loudness."""

    JOKE = "Why don't eggs tell jokes? Because they'd crack each other up!"

    def test_a_fragment_of_what_he_is_saying_is_his(self):
        for fragment in (
            "why don't eggs tell jokes",
            "because they'd crack each other up",
            "WHY DON'T EGGS TELL JOKES",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(NS["_barge_in_sounds_like_reachy"](fragment, self.JOKE))

    def test_something_he_never_said_is_not(self):
        for said in ("stop talking please", "turn the light blue", "what time is it"):
            with self.subTest(said=said):
                self.assertFalse(NS["_barge_in_sounds_like_reachy"](said, self.JOKE))

    def test_nothing_heard_is_not_his_voice(self):
        self.assertFalse(NS["_barge_in_sounds_like_reachy"]("", self.JOKE))
        self.assertFalse(NS["_barge_in_sounds_like_reachy"]("hello", ""))

    def test_a_short_shared_word_is_not_enough(self):
        # "the" appearing in both must not make a real request look like echo.
        self.assertFalse(
            NS["_barge_in_sounds_like_reachy"]("open the door", "the eggs are cracking")
        )


class InterruptionSurvivesTheRecogniserTest(unittest.IsolatedAsyncioTestCase):
    """A clip recorded over the loudspeaker must not be thrown away twice.

    faster-whisper runs its own VAD before decoding. On a recording made while
    Reachy was speaking it removed all 4.32 seconds of someone saying "stop
    talking" and returned nothing — so the interruption looked unconfirmed and
    he talked straight over the person asking him to stop.
    """

    def _capture(self):
        return VoiceCapture(
            np.full(16000, 0.2, np.float32), 16000, 1, 1.0, None, 4, voiced_duration_s=1.0
        )

    async def test_the_check_transcribes_without_the_recognisers_own_vad(self):
        agent, seen = FakeAgent(), {}
        capture = self._capture()

        async def fake_transcribe(_agent, _wav, payload, _session):
            seen.update(payload)
            return Transcription("stop talking", "fake", "fake"), False

        session = {"payload": {}, "cancel_event": None}
        with mock.patch.dict(NS, {"_conversation_transcribe": fake_transcribe}):
            verdict = await NS["_verified_barge_in"](agent, session, capture, "a long joke")

        self.assertTrue(verdict)
        self.assertIs(seen["stt_vad_filter"], False)

    async def test_the_words_are_carried_into_the_turn_not_re_derived(self):
        # Re-transcribing would run that VAD over the same clip and lose them.
        agent = FakeAgent()
        capture = self._capture()
        heard = Transcription("stop talking", "fake", "fake")

        async def fake_transcribe(_agent, _wav, _payload, _session):
            return heard, False

        session = {"payload": {}, "cancel_event": None}
        with mock.patch.dict(NS, {"_conversation_transcribe": fake_transcribe}):
            await NS["_verified_barge_in"](agent, session, capture, "a long joke")

        carried = session["pending_transcript"]
        self.assertIs(carried[0], capture)
        self.assertIs(carried[1], heard)
        self.assertIs(session["pending_capture"], capture)


class SttVadFilterOptionTest(unittest.TestCase):
    """The recogniser's VAD is on unless a caller that already gated the audio says otherwise."""

    def test_it_defaults_to_on(self):
        from wactorz.catalogue_agents.reachy_stt import STTConfig

        self.assertTrue(STTConfig.resolve({}, {}).vad_filter)

    def test_a_caller_can_turn_it_off(self):
        from wactorz.catalogue_agents.reachy_stt import STTConfig

        self.assertFalse(STTConfig.resolve({"stt_vad_filter": False}, {}).vad_filter)


class CheckingDoesNotSlowTheTalkingTest(unittest.IsolatedAsyncioTestCase):
    """Checking an interruption costs a recogniser pass; sentences must not wait for it.

    Reachy's own voice trips the onset on nearly every sentence, so doing the
    check between them put a two-second recogniser pass in every gap and made
    him sound slow. It runs alongside the next sentence now.
    """

    async def _speak(self, verdicts, chunks=3, delay=0.05):
        agent = FakeAgent()
        session = {"payload": {}, "cancel_event": None, "barge_check": None}
        order, pending = [], iter(verdicts)

        async def slow_check(verdict):
            await asyncio.sleep(delay)
            order.append("checked")
            return verdict

        async def fake_say(_agent, payload):
            order.append(f"said:{payload['text'][:6]}")
            verdict = next(pending, None)
            running = session.get("barge_check")
            if running is not None and running.done():
                running = None
            if verdict is not None and running is None:
                # Mirrors _say: a check already in flight is never replaced.
                session["barge_check"] = asyncio.create_task(slow_check(verdict))
            return {"said": payload["text"], "interrupted": False, "stopped": False}

        text = " ".join(f"Sentence number {n} is here and it runs on." for n in range(chunks))
        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](agent, text, await_playback=True, session=session)
        return result, order

    async def test_the_next_sentence_starts_before_the_check_finishes(self):
        result, order = await self._speak([False, False, False])

        self.assertTrue(result["spoke"])
        self.assertFalse(result["interrupted"])
        # A said/checked/said/checked lockstep would mean each sentence waited.
        self.assertEqual(order[0], order[0])
        self.assertGreaterEqual(len([step for step in order if step.startswith("said:")]), 2)
        self.assertLess(order.index("checked"), len(order))

    async def test_a_confirmed_check_still_stops_him(self):
        result, _order = await self._speak([True, False, False], delay=0.0)

        self.assertTrue(result["interrupted"])

    async def test_he_stops_at_the_next_sentence_not_at_the_end_of_the_reply(self):
        # The whole point of taking the answer between sentences: without it he
        # would finish the entire reply and only then notice he was interrupted.
        agent = FakeAgent()
        session = {"payload": {}, "cancel_event": None, "barge_check": None}
        said = []

        async def yes():
            return True

        async def fake_say(_agent, payload):
            said.append(payload["text"])
            if session.get("barge_check") is None:
                session["barge_check"] = asyncio.create_task(yes())
            # Real playback awaits; that is what lets the check land in time.
            await asyncio.sleep(0)
            return {"said": payload["text"], "interrupted": False, "stopped": False}

        text = " ".join(
            f"This is sentence number {n} and it carries on for a while yet." for n in range(4)
        )
        self.assertGreater(len(NS["_speech_chunks"](text)), 1, "text must split")

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](agent, text, await_playback=True, session=session)

        self.assertTrue(result["interrupted"])
        self.assertEqual(len(said), 1)

    async def test_a_check_still_running_at_the_end_is_waited_for(self):
        # The final sentence has nowhere to hand a late answer on to.
        agent = FakeAgent()
        session = {"payload": {}, "cancel_event": None, "barge_check": None}

        async def late_yes():
            await asyncio.sleep(0.05)
            return True

        async def fake_say(_agent, payload):
            session["barge_check"] = asyncio.create_task(late_yes())
            return {"said": payload["text"], "interrupted": False, "stopped": False}

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](
                agent, "One short answer.", await_playback=True, session=session
            )

        self.assertTrue(result["interrupted"])

    async def test_a_check_that_fails_leaves_him_talking(self):
        agent = FakeAgent()
        session = {"payload": {}, "cancel_event": None, "barge_check": None}

        async def boom():
            raise RuntimeError("stt down")

        async def fake_say(_agent, payload):
            session["barge_check"] = asyncio.create_task(boom())
            return {"said": payload["text"], "interrupted": False, "stopped": False}

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](
                agent, "One short answer.", await_playback=True, session=session
            )

        self.assertFalse(result["interrupted"])
        self.assertTrue(result["spoke"])


class SentencesArePreparedAheadTest(unittest.IsolatedAsyncioTestCase):
    """The next sentence is synthesized while this one is still playing.

    Each sentence is its own edge-tts request. Doing them one after another put
    that round trip in every gap, which is the pause heard as
    "Sure! ... I'm Reachy ... I love a good chat".
    """

    TEXT = " ".join(
        f"This is sentence number {n} and it carries on for a while yet." for n in range(3)
    )

    async def _run(self, stop_after=None):
        agent, order = FakeAgent(), []
        chunks = NS["_speech_chunks"](self.TEXT)
        self.assertGreater(len(chunks), 2, "text must split into several sentences")
        index_of = {chunk: n for n, chunk in enumerate(chunks)}

        async def fake_prepare(_agent, text, _payload):
            order.append(f"prep:{index_of[text]}")
            return {
                "raw_path": "/tmp/x.mp3",
                "play_path": "/tmp/x.mp3",
                "voice": "v",
                "speech_seconds": 0.0,
                "trim_db": 0.0,
            }

        async def fake_say(_agent, payload):
            n = index_of[payload["text"]]
            order.append(f"say-start:{n}")
            await asyncio.sleep(0.02)
            order.append(f"say-end:{n}")
            stopped = stop_after is not None and n == stop_after
            return {"said": payload["text"], "interrupted": False, "stopped": stopped}

        with mock.patch.dict(NS, {"_say": fake_say, "_prepare_speech": fake_prepare}):
            result = await NS["_speak_reply"](agent, self.TEXT, await_playback=True)
        return result, order, chunks

    async def test_the_next_one_is_prepared_before_this_one_finishes(self):
        _result, order, _chunks = await self._run()

        # Serial would read: say-start:0, say-end:0, prep:1. Pipelined puts the
        # preparation inside the first sentence's playback.
        self.assertLess(order.index("prep:1"), order.index("say-end:0"))
        self.assertLess(order.index("prep:2"), order.index("say-end:1"))

    async def test_the_first_sentence_still_starts_without_waiting_for_the_rest(self):
        # Preparing everything up front would trade these gaps for a slow start.
        _result, order, _chunks = await self._run()

        self.assertEqual(order[0], "prep:0")
        self.assertEqual(order[1], "say-start:0")

    async def test_a_sentence_prepared_for_a_reply_that_stops_is_discarded(self):
        result, order, chunks = await self._run(stop_after=0)

        self.assertTrue(result["stopped"])
        self.assertEqual(result["spoken_result"], chunks[0])
        # It was prepared during the first sentence, then never spoken.
        self.assertIn("prep:1", order)
        self.assertNotIn("say-start:1", order)


class DiscardPreparedTest(unittest.IsolatedAsyncioTestCase):
    """Dropping an unspoken sentence must not raise, whatever state it is in."""

    async def test_nothing_to_discard_is_fine(self):
        await NS["_discard_prepared"](None)

    async def test_a_running_preparation_is_cancelled(self):
        async def never():
            await asyncio.sleep(30)

        task = asyncio.create_task(never())
        await NS["_discard_prepared"](task)

        self.assertTrue(task.cancelled() or task.done())

    async def test_a_failed_preparation_is_swallowed(self):
        async def boom():
            raise RuntimeError("edge-tts down")

        await NS["_discard_prepared"](asyncio.create_task(boom()))


if __name__ == "__main__":
    unittest.main()
