"""Stopping, offline commands, health and speech synthesis against the real SDK's shape.

The fakes here have the attributes the installed Reachy Mini SDK actually
exposes (`cancel_move`, `get_current_head_pose`, the `imu` property), so a
behaviour that only works against an attribute the SDK lacks fails here.
"""

import asyncio
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)


class FakeAgent:
    name = "reachy-mini"

    def __init__(self, mini=None):
        self.state = {"mini": mini, "create_head_pose": lambda **_k: "NEUTRAL"}
        self.published, self.logs = [], []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def log(self, text, level="info"):
        self.logs.append((level, text))

    def persist(self, key, value):
        del key, value

    def run_in_background(self, coro):
        return asyncio.create_task(coro)


class RobotMini:
    """The motion surface of the SDK's ReachyMini, recording what it is asked."""

    def __init__(self, *, readable_pose=True, cancel_fails=False):
        self.calls = []
        self._cancel_fails = cancel_fails
        if readable_pose:
            self.get_current_head_pose = lambda: "CURRENT"

    def cancel_move(self):
        self.calls.append(("cancel_move",))
        if self._cancel_fails:
            raise AttributeError("'NoneType' object has no attribute 'stop_playing'")

    def goto_target(self, **kwargs):
        self.calls.append(("goto_target", kwargs))


def _stop(agent):
    with mock.patch.dict(NS, {"_stop_audio": mock.AsyncMock(return_value=True)}):
        return asyncio.run(NS["_dispatch"](agent, "stop", {}, True))


class StopHoldsWhereItIsTest(unittest.TestCase):
    def test_stop_cancels_a_recorded_move_and_holds_the_current_pose(self):
        mini = RobotMini()

        result = _stop(FakeAgent(mini))

        self.assertTrue(result["ok"])
        self.assertIn(("cancel_move",), mini.calls)
        (target,) = [
            kwargs for name, *rest in mini.calls if name == "goto_target" for kwargs in rest
        ]
        self.assertEqual(target["head"], "CURRENT")
        self.assertIsNone(target["body_yaw"])

    def test_stop_never_sends_the_head_to_neutral(self):
        mini = RobotMini()

        _stop(FakeAgent(mini))

        heads = [
            kwargs.get("head")
            for name, *rest in mini.calls
            if name == "goto_target"
            for kwargs in rest
        ]
        self.assertNotIn("NEUTRAL", heads)

    def test_without_a_readable_pose_stop_does_not_move_at_all(self):
        mini = RobotMini(readable_pose=False)

        result = _stop(FakeAgent(mini))

        self.assertTrue(result["ok"])
        self.assertFalse(result["held"])
        self.assertNotIn("goto_target", [call[0] for call in mini.calls])

    def test_a_failed_cancel_still_holds_the_pose(self):
        mini = RobotMini(cancel_fails=True)

        result = _stop(FakeAgent(mini))

        self.assertTrue(result["ok"])
        self.assertTrue(result["held"])


class OfflineCommandsTest(unittest.TestCase):
    def test_a_motion_command_over_mqtt_says_the_robot_is_not_connected(self):
        agent = FakeAgent(mini=None)

        for cmd in ("wake", "pose", "stop", "look_at", "gesture"):
            with self.subTest(cmd=cmd):
                result = asyncio.run(NS["_dispatch"](agent, cmd, {}, True))

                self.assertFalse(result["ok"])
                self.assertIn("reachy not connected", result["error"])
                self.assertNotIn("NoneType", result["error"])

    def test_the_refusal_is_published_as_a_failed_event(self):
        agent = FakeAgent(mini=None)

        asyncio.run(NS["_dispatch"](agent, "wake", {"id": "abc"}, False))

        topics = dict(agent.published)
        self.assertFalse(topics["custom/reachy/cmd_result/abc"]["ok"])
        self.assertFalse(topics["custom/reachy/events"]["ok"])

    def test_commands_that_need_no_robot_still_answer(self):
        agent = FakeAgent(mini=None)

        result = asyncio.run(NS["_dispatch"](agent, "help", {}, True))

        self.assertTrue(result["ok"])


class ImuTemperatureTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_sdk_imu_property_is_read(self):
        agent = FakeAgent(types.SimpleNamespace(imu={"temperature": 38.04, "gyroscope": [0, 0, 0]}))

        self.assertEqual(await NS["_imu_temperature"](agent), 38.0)

    async def test_a_lite_without_an_imu_reports_no_temperature(self):
        agent = FakeAgent(types.SimpleNamespace(imu=None))

        self.assertIsNone(await NS["_imu_temperature"](agent))


class _StalledCommunicate:
    """A speech service that sends one chunk and then nothing more."""

    async def stream(self):
        yield {"type": "audio", "data": b"\x00"}
        await asyncio.Future()


class SpeechSynthesisTimeoutTest(unittest.TestCase):
    def _prepare(self, communicate):
        made = []
        real_join = os.path.join

        def remember(*parts):
            path = real_join(*parts)
            if str(parts[-1]).startswith("reachy_say_"):
                made.append(path)
            return path

        async def no_boost(_agent, _path, _trim=0.0):
            return None

        with (
            mock.patch.dict(sys.modules, {"edge_tts": types.SimpleNamespace()}),
            mock.patch.dict(
                NS,
                {
                    "_edge_tts_communicate": lambda *_a, **_k: communicate,
                    "_ffmpeg_path": lambda: None,
                    "_boost_audio": no_boost,
                    "_TTS_STALL_TIMEOUT_S": 0.05,
                },
            ),
            mock.patch.object(os.path, "join", remember),
        ):
            try:
                return asyncio.run(NS["_prepare_speech"](FakeAgent(), "Hello there.", {})), made
            except Exception as error:
                return error, made

    def test_a_stalled_speech_service_ends_the_sentence_with_a_reason(self):
        outcome, made = self._prepare(_StalledCommunicate())

        self.assertIsInstance(outcome, RuntimeError)
        self.assertIn("internet connection", str(outcome))
        self.assertTrue(made)
        self.assertFalse(any(os.path.exists(path) for path in made))

    def test_a_cancelled_synthesis_leaves_no_file_behind(self):
        made = []
        real_join = os.path.join

        def remember(*parts):
            path = real_join(*parts)
            if str(parts[-1]).startswith("reachy_say_"):
                made.append(path)
            return path

        async def scenario():
            task = asyncio.create_task(NS["_prepare_speech"](FakeAgent(), "Hello there.", {}))
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        with (
            mock.patch.dict(sys.modules, {"edge_tts": types.SimpleNamespace()}),
            mock.patch.dict(
                NS,
                {
                    "_edge_tts_communicate": lambda *_a, **_k: _StalledCommunicate(),
                    "_ffmpeg_path": lambda: None,
                    "_TTS_STALL_TIMEOUT_S": 30.0,
                },
            ),
            mock.patch.object(os.path, "join", remember),
        ):
            asyncio.run(scenario())

        self.assertTrue(made)
        self.assertFalse(any(os.path.exists(path) for path in made))

    def test_a_healthy_stream_is_written_in_full(self):
        class Healthy:
            async def stream(self):
                yield {"type": "audio", "data": b"ab"}
                yield {"type": "WordBoundary", "offset": 0, "duration": 5_000_000}
                yield {"type": "audio", "data": b"cd"}

        outcome, _made = self._prepare(Healthy())

        assert isinstance(outcome, dict), outcome
        with open(outcome["play_path"], "rb") as handle:
            self.assertEqual(handle.read(), b"abcd")
        os.unlink(outcome["play_path"])
        self.assertTrue(outcome["play_path"].startswith(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()


class CleanupTest(unittest.TestCase):
    def test_the_sdk_handle_is_closed_off_the_event_loop(self):
        loop_threads = []
        closed_on = []

        class Handle:
            def __exit__(self, *_exc):
                closed_on.append(threading.get_ident())

        agent = FakeAgent(Handle())

        async def run():
            loop_threads.append(threading.get_ident())
            with mock.patch.dict(NS, {"_conversation_stop": mock.AsyncMock()}):
                await NS["cleanup"](agent)

        asyncio.run(run())

        self.assertEqual(len(closed_on), 1)
        self.assertNotEqual(closed_on[0], loop_threads[0])
