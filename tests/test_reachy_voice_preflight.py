"""Voice input says what is missing before anyone speaks, and why it stopped.

A recognizer that cannot run is found before recording starts: otherwise a
missing key surfaces only once a visitor has spoken, as failed turns, and a
conversation ends after a few of those with nothing in chat to say why.
"""

import asyncio
import os
import types
import unittest
from unittest import mock

import numpy as np

from wactorz.catalogue_agents import reachy_stt
from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE
from wactorz.catalogue_agents.reachy_vad import VoiceCapture

NS = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)


def _installed(*names):
    """A find_spec that knows only these modules."""
    return lambda name: object() if name in names else None


class FakeAgent:
    name = "reachy-mini"

    def __init__(self):
        self.state = {
            "mini": types.SimpleNamespace(media=object()),
            "media_backend": "webrtc",
            "conversation_session": None,
            "conversation_state": "idle",
            "last_cmd": None,
        }
        self.published, self.chat, self.logs = [], [], []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def notify_user(self, text, **extra):
        self.chat.append((text, extra))

    async def log(self, text, level="info"):
        self.logs.append((level, text))

    def run_in_background(self, coro):
        return asyncio.create_task(coro)


class ConfigurationProblemTest(unittest.TestCase):
    def test_deepgram_without_a_key_names_the_key_and_the_local_alternative(self):
        problem = reachy_stt.configuration_problem({}, {}, find_spec=_installed("deepgram"))

        self.assertIsNotNone(problem)
        self.assertIn("DEEPGRAM_API_KEY", str(problem))
        self.assertIn("faster-whisper", str(problem))

    def test_a_configured_backend_has_no_problem(self):
        problem = reachy_stt.configuration_problem(
            {}, {"DEEPGRAM_API_KEY": "set"}, find_spec=_installed("deepgram")
        )

        self.assertIsNone(problem)

    def test_a_missing_package_says_how_to_install_it(self):
        problem = reachy_stt.configuration_problem(
            {"stt_backend": "faster-whisper"}, {}, find_spec=_installed()
        )

        self.assertIn("pip install faster-whisper", str(problem))

    def test_a_local_backend_needs_no_key(self):
        problem = reachy_stt.configuration_problem(
            {"stt_backend": "faster-whisper"}, {}, find_spec=_installed("faster_whisper")
        )

        self.assertIsNone(problem)

    def test_hosted_openai_needs_its_own_key(self):
        problem = reachy_stt.configuration_problem(
            {"stt_backend": "openai"}, {}, find_spec=_installed("openai")
        )

        self.assertIn("OPENAI_API_KEY", str(problem))

    def test_an_unknown_backend_is_reported_rather_than_raised(self):
        problem = reachy_stt.configuration_problem(
            {"stt_backend": "telepathy"}, {}, find_spec=_installed()
        )

        self.assertIn("telepathy", str(problem))
        self.assertIn("REACHY_STT_BACKEND", str(problem))

    def test_a_conversation_also_needs_voice_detection(self):
        environ = {"DEEPGRAM_API_KEY": "set"}

        without = reachy_stt.configuration_problem(
            {}, environ, needs_vad=True, find_spec=_installed("deepgram")
        )
        with_vad = reachy_stt.configuration_problem(
            {}, environ, needs_vad=True, find_spec=_installed("deepgram", "webrtcvad")
        )

        self.assertIn("webrtcvad-wheels", str(without))
        self.assertIsNone(with_vad)

    def test_a_module_that_cannot_be_looked_up_counts_as_missing(self):
        def broken(_name):
            raise ValueError("deepgram.__spec__ is not set")

        problem = reachy_stt.configuration_problem(
            {}, {"DEEPGRAM_API_KEY": "set"}, find_spec=broken
        )

        self.assertIn("not installed", str(problem))

    def test_an_already_imported_module_counts_as_installed(self):
        with mock.patch.dict("sys.modules", {"deepgram": types.ModuleType("deepgram")}):
            problem = reachy_stt.configuration_problem(
                {}, {"DEEPGRAM_API_KEY": "set"}, find_spec=_installed()
            )

        self.assertIsNone(problem)


class RefusedBeforeListeningTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_conversation_does_not_open_without_a_recognizer(self):
        agent = FakeAgent()
        with mock.patch.dict(
            NS, {"_voice_input_problem": lambda _p, **_k: "voice input needs an API key"}
        ):
            result = await NS["_dispatch"](agent, "conversation_start", {}, True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "stt_unavailable")
        self.assertIn("can't start a conversation yet", result["result"])
        self.assertIn("API key", result["result"])
        self.assertIsNone(agent.state["conversation_session"])

    async def test_push_to_talk_refuses_before_recording(self):
        agent = FakeAgent()
        listen = mock.AsyncMock()
        with mock.patch.dict(
            NS,
            {
                "_voice_input_problem": lambda _p, **_k: "voice input needs an API key",
                "_listen": listen,
            },
        ):
            result = await NS["_dispatch"](agent, "ask_voice", {}, True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "stt_unavailable")
        self.assertIn("can't listen yet", result["result"])
        listen.assert_not_awaited()

    async def test_the_agent_checks_the_session_settings_it_will_use(self):
        seen = {}

        def problem(payload, *, needs_vad=False, find_spec=None):
            seen.update(payload=payload, needs_vad=needs_vad)

        with mock.patch.object(reachy_stt, "configuration_problem", problem):
            self.assertIsNone(
                NS["_voice_input_problem"]({"stt_backend": "whisper"}, needs_vad=True)
            )

        self.assertEqual(seen["payload"]["stt_backend"], "whisper")
        self.assertTrue(seen["needs_vad"])


def _captured(reason=None):
    audio = np.zeros(0, np.float32) if reason else np.full(1600, 0.2, np.float32)
    return VoiceCapture(audio, 16000, 1, 0.1 if audio.size else 0.0, reason, 4)


class SessionEndNoticeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._environment = mock.patch.dict(
            os.environ,
            {
                "REACHY_STT_STREAMING": "0",
                "REACHY_CONVERSATION_STATE_MOTION": "0",
                "REACHY_CONVERSATION_IDLE_MOTION": "0",
            },
        )
        self._environment.start()

    def tearDown(self):
        self._environment.stop()

    async def _session(self, payload, clips, transcribe):
        agent, clips = FakeAgent(), iter(clips)

        async def capture(_agent, _session, _config):
            return next(clips)

        async def cooldown(*_args):
            return None

        with (
            mock.patch.dict(
                NS,
                {
                    "_voice_input_problem": lambda _p, **_k: None,
                    "_conversation_capture": capture,
                    "_conversation_cooldown": cooldown,
                    "_bridge_to_main": mock.AsyncMock(),
                },
            ),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", transcribe),
        ):
            await NS["_conversation_start"](agent, payload)
            await agent.state["conversation_session"]["task"]
        return agent

    def _notices(self, agent):
        return [text for text, extra in agent.chat if extra.get("from") == "reachy-mini"]

    async def test_a_session_that_times_out_says_so_and_how_to_restart(self):
        agent = await self._session({}, [_captured("inactivity_timeout")], mock.AsyncMock())

        (notice,) = self._notices(agent)
        self.assertIn("nobody spoke", notice)
        self.assertIn("start conversation", notice)

    async def test_repeated_failures_end_with_the_reason(self):
        failing = mock.AsyncMock(side_effect=RuntimeError("Deepgram rejected the key"))

        agent = await self._session(
            {"max_consecutive_errors": 2}, [_captured(), _captured()], failing
        )

        (notice,) = self._notices(agent)
        self.assertIn("several voice turns in a row failed", notice)
        self.assertIn("Deepgram rejected the key", notice)

    async def test_saying_goodbye_needs_no_explanation(self):
        transcribe = mock.AsyncMock(
            return_value=reachy_stt.Transcription("goodbye", "fake", "fake")
        )

        agent = await self._session({}, [_captured()], transcribe)

        self.assertEqual(agent.state["conversation_state"], "stopped")
        self.assertEqual(self._notices(agent), [])

    async def test_an_explicit_stop_needs_no_explanation(self):
        agent = FakeAgent()
        started = asyncio.Event()

        async def blocked(_agent, _session, _config):
            started.set()
            await asyncio.Future()

        with mock.patch.dict(
            NS, {"_voice_input_problem": lambda _p, **_k: None, "_conversation_capture": blocked}
        ):
            await NS["_conversation_start"](agent, {})
            await started.wait()
            await NS["_conversation_stop"](agent, {})

        self.assertEqual(self._notices(agent), [])


if __name__ == "__main__":
    unittest.main()
