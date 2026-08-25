"""
Tests for the reachy-mini camera / microphone commands.

The reachy-mini agent ships as a recipe: its body is the ``AGENT_CODE`` string
that CatalogAgent execs at spawn time. We exec it here into a namespace and
exercise the perception commands (``camera``, ``listen``, ``doa``) against a
fake SDK media manager, plus the pure encoding helpers. No robot required.

What we guard:
- ``_encode_frame`` turns a BGR numpy frame into decodable JPEG/PNG bytes of the
  right size, and flips B<->R so colours are correct.
- ``_pcm_to_wav_b64`` produces a valid WAV parseable by stdlib ``wave``.
- ``camera`` returns a base64 image (and can save to disk / skip the blob).
- ``listen`` returns base64 WAV plus samplerate/channels and a DoA snapshot.
- ``doa`` reports the mic direction without recording.
- A ``None`` camera frame surfaces a clear error rather than crashing.
"""

import asyncio
import base64
import io
import math
import sys
import types
import unittest
import wave
from unittest import mock

import numpy as np
from PIL import Image

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE


def _load_recipe_namespace():
    ns: dict = {}
    exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), ns)
    return ns


NS = _load_recipe_namespace()


_FAKE_REACHY_SDK = None


def setUpModule():
    """Provide only the SDK metadata these unit tests exercise."""
    global _FAKE_REACHY_SDK
    try:
        __import__("reachy_mini")
    except ModuleNotFoundError:
        sdk = types.ModuleType("reachy_mini")
        sdk.__version__ = "1.8.4"
        _FAKE_REACHY_SDK = sdk
        sys.modules["reachy_mini"] = sdk


def tearDownModule():
    if _FAKE_REACHY_SDK is not None and sys.modules.get("reachy_mini") is _FAKE_REACHY_SDK:
        sys.modules.pop("reachy_mini", None)


class FakeMedia:
    """Minimal stand-in for the SDK MediaManager (mini.media)."""

    def __init__(self, frame=None, samplerate=16000, channels=1, doa=(42.0, True)):
        self._frame = frame
        self._samplerate = samplerate
        self._channels = channels
        self._doa = doa
        self.recording = False

    # --- camera ---
    def get_frame(self):
        return self._frame

    # --- audio ---
    def start_recording(self):
        self.recording = True

    def stop_recording(self):
        self.recording = False

    def get_audio_sample(self):
        # One 256-frame chunk of quiet-ish audio per poll.
        return np.full(256, 0.25, dtype=np.float32)

    def get_input_audio_samplerate(self):
        return self._samplerate

    def get_input_channels(self):
        return self._channels

    def get_DoA(self):
        return self._doa


class FakeAgent:
    """Just enough of the actor surface for the perception commands + dispatcher."""

    def __init__(self, media):
        self.state = {
            "mini": types.SimpleNamespace(media=media),
            "media_backend": "",
            "last_cmd": None,
        }
        self.published: list[tuple[str, object]] = []
        self.logs: list[str] = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def log(self, msg, level="info"):
        self.logs.append(msg)


def _run(coro):
    return asyncio.run(coro)


class EncodeHelpersTest(unittest.TestCase):
    def test_encode_frame_jpeg_roundtrips_size(self):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)  # H=48, W=64
        data, w, h = NS["_encode_frame"](frame, "jpeg", 85)
        self.assertEqual((w, h), (64, 48))
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.size, (64, 48))

    def test_encode_frame_png_and_bgr_to_rgb(self):
        # Pure blue in BGR (B=255) must decode as blue in RGB after the flip.
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        frame[..., 0] = 255  # BGR blue channel
        data, _w, _h = NS["_encode_frame"](frame, "png")
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.convert("RGB").getpixel((0, 0)), (0, 0, 255))  # RGB blue

    def test_pcm_to_wav_b64_is_valid_wav(self):
        audio = np.linspace(-1.0, 1.0, 8000, dtype=np.float32)
        b64, frames = NS["_pcm_to_wav_b64"](audio, 16000, 1)
        self.assertEqual(frames, 8000)
        raw = base64.b64decode(b64)
        with wave.open(io.BytesIO(raw), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), 16000)
            self.assertEqual(w.getnframes(), 8000)


class CameraCommandTest(unittest.TestCase):
    def test_camera_returns_base64_jpeg(self):
        frame = np.zeros((30, 40, 3), dtype=np.uint8)
        agent = FakeAgent(FakeMedia(frame=frame))
        res = _run(NS["_dispatch"](agent, "camera", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertEqual((res["width"], res["height"]), (40, 30))
        self.assertEqual(res["format"], "jpeg")
        img = Image.open(io.BytesIO(base64.b64decode(res["image_b64"])))
        self.assertEqual(img.size, (40, 30))

    def test_camera_include_b64_false_and_publish(self):
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        agent = FakeAgent(FakeMedia(frame=frame))
        res = _run(
            NS["_dispatch"](
                agent, "camera", {"include_b64": False, "publish": True}, return_result=True
            )
        )
        self.assertNotIn("image_b64", res)
        self.assertEqual(res["published"], "custom/reachy/camera")
        topics = [t for t, _ in agent.published]
        self.assertIn("custom/reachy/camera", topics)

    def test_camera_no_frame_is_clear_error(self):
        agent = FakeAgent(FakeMedia(frame=None))
        res = _run(NS["_dispatch"](agent, "camera", {}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("no camera frame", res["error"])


class ListenCommandTest(unittest.TestCase):
    def test_listen_returns_wav_and_doa(self):
        # SDK reports DoA in RADIANS; the agent must surface it in degrees.
        agent = FakeAgent(FakeMedia(samplerate=16000, channels=1, doa=(math.radians(42.0), True)))
        res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertEqual(res["samplerate"], 16000)
        self.assertEqual(res["channels"], 1)
        self.assertEqual(res["format"], "wav")
        self.assertGreater(res["frames"], 0)
        self.assertAlmostEqual(res["doa_deg"], 42.0, places=6)
        self.assertTrue(res["voice_detected"])
        raw = base64.b64decode(res["audio_b64"])
        with wave.open(io.BytesIO(raw), "rb") as w:
            self.assertEqual(w.getframerate(), 16000)

    def test_doa_reports_direction_without_recording(self):
        media = FakeMedia(doa=(math.radians(90.0), False))  # radians in
        agent = FakeAgent(media)
        res = _run(NS["_dispatch"](agent, "doa", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["detected"])
        self.assertAlmostEqual(res["angle_deg"], 90.0, places=6)  # degrees out
        self.assertFalse(res["voice_detected"])
        self.assertFalse(media.recording)  # never started a recording

    def test_listen_result_summary_has_no_base64_blob(self):
        agent = FakeAgent(FakeMedia(samplerate=16000, channels=1, doa=(math.radians(42.0), True)))
        res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
        # The human-facing summary is a short line, and the blob lives elsewhere.
        self.assertIn("Recorded", res["result"])
        self.assertIn("42", res["result"])  # "sound from 42°" — degrees
        self.assertNotIn(res["audio_b64"], res["result"])


class RemoteDoaFallbackTest(unittest.TestCase):
    """WebRTC clients use the robot daemon when local USB has no mic array."""

    def test_doa_uses_daemon_when_sdk_returns_none(self):
        agent = FakeAgent(FakeMedia(doa=None))
        remote = mock.AsyncMock(return_value=(math.pi / 2, True))

        with mock.patch.dict(NS, {"_read_daemon_doa": remote}):
            res = _run(NS["_dispatch"](agent, "doa", {}, return_result=True))

        self.assertTrue(res["ok"])
        self.assertTrue(res["detected"])
        self.assertAlmostEqual(res["angle_deg"], 90.0, places=6)
        self.assertTrue(res["voice_detected"])
        remote.assert_awaited_once_with(agent)


class RemovedSoundFacingTest(unittest.TestCase):
    """Sound-facing commands are no longer exposed as reliable robot actions."""

    def test_removed_commands_fail_clearly(self):
        for cmd in ("turn_to_sound", "track_sound"):
            agent = FakeAgent(FakeMedia())
            res = _run(NS["_dispatch"](agent, cmd, {}, return_result=True))

            self.assertFalse(res["ok"], msg=cmd)
            self.assertEqual(res["cmd"], cmd)
            self.assertIn("unknown cmd", res["error"])
            event = [p for topic, p in agent.published if topic == "custom/reachy/events"][-1]
            self.assertFalse(event["ok"])
            self.assertEqual(event["type"], cmd)


class FakeLLM:
    """Records the messages it is asked to complete and returns a canned reply."""

    def __init__(self, reply="A desk with a laptop and a blue mug."):
        self.reply = reply
        self.calls: list[dict] = []

    async def complete(self, messages, system=""):
        self.calls.append({"messages": messages, "system": system})
        return self.reply


class DescribeCommandTest(unittest.TestCase):
    def _agent(self, frame, llm):
        agent = FakeAgent(FakeMedia(frame=frame))
        agent.llm = llm
        return agent

    def test_describe_sends_image_block_and_returns_description(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        llm = FakeLLM("A tidy desk with a laptop.")
        agent = self._agent(frame, llm)
        # say=False so we exercise vision without the TTS/audio path.
        res = _run(NS["_dispatch"](agent, "describe", {"say": False}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertEqual(res["description"], "A tidy desk with a laptop.")  # raw answer
        # A general, brief look offers more instead of monologuing.
        self.assertTrue(res["said"].startswith("A tidy desk with a laptop"))
        self.assertIn("look closer", res["said"].lower())
        self.assertFalse(res["detail"])
        # The LLM must have received a real base64 JPEG image block + the BRIEF prompt.
        content = llm.calls[0]["messages"][0]["content"]
        img = next(b for b in content if b["type"] == "image")
        self.assertEqual(img["source"]["type"], "base64")
        self.assertEqual(img["source"]["media_type"], "image/jpeg")
        self.assertTrue(img["source"]["data"])  # non-empty b64
        self.assertIn("one short", llm.calls[0]["system"].lower())  # brief by default

    def test_detail_request_uses_full_paragraph_and_skips_nudge(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        llm = FakeLLM("A long, detailed paragraph about the room.")
        agent = self._agent(frame, llm)
        res = _run(
            NS["_dispatch"](agent, "describe", {"detail": True, "say": False}, return_result=True)
        )
        self.assertTrue(res["detail"])
        self.assertEqual(res["said"], "A long, detailed paragraph about the room.")  # no nudge
        self.assertIn("paragraph", llm.calls[0]["system"].lower())  # detailed prompt

    def test_specific_question_gets_no_nudge(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        llm = FakeLLM("Two people.")
        agent = self._agent(frame, llm)
        res = _run(
            NS["_dispatch"](
                agent,
                "describe",
                {"question": "how many people?", "say": False},
                return_result=True,
            )
        )
        self.assertEqual(res["said"], "Two people.")  # no "look closer" tacked on

    def test_brief_general_look_arms_the_yes_followup(self):
        agent = self._agent(np.zeros((16, 16, 3), dtype=np.uint8), FakeLLM("A desk."))
        _run(NS["_dispatch"](agent, "describe", {"say": False}, return_result=True))
        self.assertTrue(NS["_pending_look_closer"](agent))  # 'yes' now means look closer

    def test_detail_look_disarms_the_followup(self):
        agent = self._agent(np.zeros((16, 16, 3), dtype=np.uint8), FakeLLM("Long detail."))
        agent.state["_pending_detail"] = __import__("time").time()  # armed
        _run(NS["_dispatch"](agent, "describe", {"detail": True, "say": False}, return_result=True))
        self.assertFalse(NS["_pending_look_closer"](agent))

    def test_specific_question_disarms_the_followup(self):
        agent = self._agent(np.zeros((16, 16, 3), dtype=np.uint8), FakeLLM("Two people."))
        agent.state["_pending_detail"] = __import__("time").time()
        _run(
            NS["_dispatch"](
                agent,
                "describe",
                {"question": "how many people?", "say": False},
                return_result=True,
            )
        )
        self.assertFalse(NS["_pending_look_closer"](agent))

    def test_offer_expires_after_a_minute(self):
        agent = self._agent(np.zeros((16, 16, 3), dtype=np.uint8), FakeLLM("x"))
        agent.state["_pending_detail"] = __import__("time").time() - 120  # 2 min ago
        self.assertFalse(NS["_pending_look_closer"](agent))

    def test_describe_passes_the_users_question(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        llm = FakeLLM("Two people.")
        agent = self._agent(frame, llm)
        res = _run(
            NS["_dispatch"](
                agent,
                "describe",
                {"question": "how many people?", "say": False},
                return_result=True,
            )
        )
        self.assertTrue(res["ok"])
        text_block = llm.calls[0]["messages"][0]["content"][0]
        self.assertEqual(text_block["type"], "text")
        self.assertIn("how many people", text_block["text"].lower())

    def test_describe_no_frame_is_clear_error(self):
        agent = self._agent(None, FakeLLM())
        res = _run(NS["_dispatch"](agent, "describe", {"say": False}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("no camera frame", res["error"])

    def test_describe_surfaces_llm_error_instead_of_speaking_it(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        agent = self._agent(frame, FakeLLM("[No LLM configured]"))
        res = _run(NS["_dispatch"](agent, "describe", {"say": False}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("No LLM", res["error"])


class DescribeAimTest(unittest.TestCase):
    """Head aim parsing for a look (pitch down=+, up=-; yaw left=+, right=-)."""

    def test_default_is_level_and_forward(self):
        self.assertEqual(NS["_describe_aim"]({}, "what do you see"), (0.0, 0.0))

    def test_direction_words(self):
        f = NS["_describe_aim"]
        self.assertEqual(f({}, "look down at my desk"), (22, 0.0))
        self.assertEqual(f({}, "look up"), (-22, 0.0))
        self.assertEqual(f({}, "look left"), (0.0, 32))
        self.assertEqual(f({}, "look right"), (0.0, -32))

    def test_explicit_pitch_yaw_win_over_words(self):
        self.assertEqual(NS["_describe_aim"]({"pitch": 10, "yaw": -5}, "look up"), (10.0, -5.0))


class DescribeOrientTest(unittest.TestCase):
    """describe orients the head before capturing so it frames the scene."""

    def setUp(self):
        self._orig_pose = NS["_pose"]
        self.poses = []

        async def _fake_pose(agent, payload):
            self.poses.append(payload)
            return {}

        NS["_pose"] = _fake_pose

    def tearDown(self):
        NS["_pose"] = self._orig_pose

    def _agent(self):
        agent = FakeAgent(FakeMedia(frame=np.zeros((16, 16, 3), dtype=np.uint8)))
        agent.llm = FakeLLM("A room.")
        return agent

    def test_levels_head_before_capturing(self):
        agent = self._agent()
        _run(
            NS["_dispatch"](
                agent, "describe", {"say": False, "look_duration": 0}, return_result=True
            )
        )
        self.assertTrue(self.poses)
        self.assertEqual((self.poses[0]["pitch"], self.poses[0]["yaw"]), (0.0, 0.0))

    def test_aim_words_tilt_the_head(self):
        agent = self._agent()
        _run(
            NS["_dispatch"](
                agent,
                "describe",
                {"question": "look down at my desk", "say": False, "look_duration": 0},
                return_result=True,
            )
        )
        self.assertEqual(self.poses[0]["pitch"], 22)

    def test_orient_false_captures_without_moving(self):
        agent = self._agent()
        _run(
            NS["_dispatch"](agent, "describe", {"orient": False, "say": False}, return_result=True)
        )
        self.assertEqual(self.poses, [])

    def test_rear_facing_description_preserves_body_orientation(self):
        agent = self._agent()
        agent.state["_facing_body_yaw_deg"] = 155.0

        _run(
            NS["_dispatch"](
                agent, "describe", {"say": False, "look_duration": 0}, return_result=True
            )
        )

        self.assertEqual(self.poses[0]["yaw"], 155.0)
        self.assertEqual(self.poses[0]["body_yaw"], 155.0)


class LookBehindTest(unittest.TestCase):
    def test_turns_captures_without_recentering_and_stays_rear_facing(self):
        agent = FakeAgent(FakeMedia(frame=np.zeros((16, 16, 3), dtype=np.uint8)))
        current = mock.AsyncMock(return_value=0.0)
        face = mock.AsyncMock(return_value={"body_yaw": 155.0})
        describe = mock.AsyncMock(
            return_value={"description": "Books.", "result": "Books.", "said": "Books."}
        )

        with mock.patch.dict(
            NS,
            {
                "_current_body_yaw_deg": current,
                "_face_body": face,
                "_describe": describe,
            },
        ):
            result = _run(NS["_look_behind"](agent, {}))

        face.assert_awaited_once_with(agent, 155.0, {})
        describe_payload = describe.await_args.args[1]
        self.assertFalse(describe_payload["orient"])
        self.assertEqual(describe_payload["question"], "Describe what is behind you.")
        self.assertEqual(result["facing"], "rear")
        self.assertEqual(result["body_yaw"], 155.0)


class LookAroundTest(unittest.TestCase):
    """look_around sweeps a few angles and describes the whole room in one call."""

    def setUp(self):
        self._orig_pose = NS["_pose"]
        self.poses = []

        async def _fake_pose(agent, payload):
            self.poses.append((payload.get("pitch"), payload.get("yaw")))
            return {}

        NS["_pose"] = _fake_pose

    def tearDown(self):
        NS["_pose"] = self._orig_pose

    def _agent(self, reply="A combined room overview."):
        agent = FakeAgent(FakeMedia(frame=np.zeros((16, 16, 3), dtype=np.uint8)))
        agent.llm = FakeLLM(reply)
        return agent

    def test_sweeps_default_views_combines_and_recentres(self):
        agent = self._agent()
        res = _run(
            NS["_dispatch"](
                agent, "look_around", {"say": False, "look_duration": 0}, return_result=True
            )
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["said"], "A combined room overview.")
        self.assertEqual(res["views"], 4)  # default 4 angles
        # A single multi-image vision call carrying all four frames.
        content = agent.llm.calls[0]["messages"][0]["content"]
        self.assertEqual(len([b for b in content if b["type"] == "image"]), 4)
        self.assertEqual(self.poses[-1], (0, 0))  # head re-centred at the end

    def test_custom_angles(self):
        agent = self._agent("ok")
        res = _run(
            NS["_dispatch"](
                agent,
                "look_around",
                {"angles": [[0, 30], [0, -30]], "say": False, "look_duration": 0},
                return_result=True,
            )
        )
        self.assertEqual(res["views"], 2)


class SayPlaybackPadTest(unittest.TestCase):
    def test_waits_out_speech_plus_tail_by_default(self):
        pad = NS["_say_playback_pad"]
        self.assertAlmostEqual(pad(2.0, {}), 2.55, places=3)  # default tail 0.55
        self.assertAlmostEqual(pad(2.0, {"tail_pad": 0.5}), 2.5, places=3)

    def test_no_wait_when_opted_out_or_unknown_duration(self):
        pad = NS["_say_playback_pad"]
        self.assertEqual(pad(2.0, {"await_playback": False}), 0.0)
        self.assertEqual(pad(0, {}), 0.0)
        self.assertEqual(pad(None, {}), 0.0)

    def test_measured_duration_wins_over_estimate(self):
        duration = NS["_speech_duration_seconds"]
        self.assertEqual(duration("a deliberately long sentence", 20_000_000), 2.0)

    def test_missing_metadata_gets_a_nonzero_duration_estimate(self):
        duration = NS["_speech_duration_seconds"]
        text = "Hello there. I am happy to help with anything you need today."
        self.assertGreater(duration(text, 0), 3.0)
        self.assertEqual(duration("", 0), 0.0)

    def test_requests_word_boundary_metadata(self):
        calls = []

        def communicate(text, voice, **kwargs):
            calls.append((text, voice, kwargs))
            return "communicate"

        edge_tts = types.SimpleNamespace(Communicate=communicate)
        result = NS["_edge_tts_communicate"](edge_tts, "Hello", "voice")

        self.assertEqual(result, "communicate")
        self.assertEqual(calls, [("Hello", "voice", {"boundary": "WordBoundary"})])

    def test_older_edge_tts_without_boundary_option_still_works(self):
        calls = []

        def communicate(text, voice, **kwargs):
            calls.append(kwargs)
            if kwargs:
                raise TypeError("unexpected boundary")
            return "legacy"

        edge_tts = types.SimpleNamespace(Communicate=communicate)
        result = NS["_edge_tts_communicate"](edge_tts, "Hello", "voice")

        self.assertEqual(result, "legacy")
        self.assertEqual(calls, [{"boundary": "WordBoundary"}, {}])


class ConnectionModeTest(unittest.TestCase):
    def test_normalize_aliases(self):
        norm = NS["_normalize_connection_mode"]
        for word in ("network", "wireless", "wifi", "robot", "DIRECT"):
            self.assertEqual(norm(word), "network")
        for word in ("local", "localhost", "app", "sim", "Simulator", "desktop"):
            self.assertEqual(norm(word), "local")
        for word in ("", "  ", "auto", "banana", None):
            self.assertEqual(norm(word), "")

    def test_network_mode_skips_localhost(self):
        # Wireless: every attempt forces network; none probe localhost first.
        attempts = NS["_build_connection_attempts"]("192.168.1.42", "", "network")
        self.assertTrue(attempts)
        self.assertTrue(all(a.get("connection_mode") == "network" for a in attempts))
        self.assertEqual(attempts[0]["host"], "192.168.1.42")

    def test_network_mode_without_host(self):
        attempts = NS["_build_connection_attempts"]("", "", "network")
        self.assertEqual(attempts, [{"connection_mode": "network"}])

    def test_local_mode_targets_localhost_and_never_forces_network(self):
        attempts = NS["_build_connection_attempts"]("192.168.1.42", "", "local")
        self.assertEqual(attempts[0]["host"], "localhost")  # robot_host ignored
        self.assertTrue(all("connection_mode" not in a for a in attempts))

    def test_auto_mode_preserves_pinned_then_network_fallback(self):
        attempts = NS["_build_connection_attempts"]("host.local", "", "")
        self.assertEqual(attempts[0], {"host": "host.local"})
        self.assertIn({"connection_mode": "network"}, attempts)

    def test_media_backend_threads_into_every_attempt(self):
        for mode in ("network", "local", ""):
            attempts = NS["_build_connection_attempts"]("h", "webrtc", mode)
            self.assertTrue(
                all(a.get("media_backend") == "webrtc" for a in attempts),
                msg=f"mode={mode!r} dropped media_backend",
            )


class NotConnectedMessageTest(unittest.TestCase):
    """The 'not connected' reason must be actionable: surface the real connect
    error and how to run without the control app."""

    def test_surfaces_last_connect_error_and_network_hint(self):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = None
        agent.state["last_connect_error"] = "mDNS lookup failed for reachy-mini.local"
        ok, reason = NS["_is_connected"](agent)
        self.assertFalse(ok)
        self.assertIn("mDNS lookup failed", reason)
        self.assertIn("REACHY_CONNECTION_MODE=network", reason)
        self.assertIn("REACHY_ROBOT_HOST", reason)

    def test_no_handle_without_error_still_hints_network(self):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = None
        ok, reason = NS["_is_connected"](agent)
        self.assertFalse(ok)
        self.assertIn("no SDK handle", reason)
        self.assertIn("REACHY_ROBOT_HOST", reason)


class MotorsCommandTest(unittest.TestCase):
    """Motor torque must be enabled or the robot 'talks but won't move'."""

    def _agent(self):
        calls = []
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = types.SimpleNamespace(
            enable_motors=lambda *a, **k: calls.append("enable"),
            disable_motors=lambda *a, **k: calls.append("disable"),
        )
        agent.calls = calls
        return agent

    def test_motors_on_enables_torque(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "motors", {"on": True}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["motors_enabled"])
        self.assertEqual(agent.calls, ["enable"])
        self.assertTrue(agent.state["motors_enabled"])

    def test_motors_off_disables_torque(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "motors", {"on": False}, return_result=True))
        self.assertFalse(res["motors_enabled"])
        self.assertEqual(agent.calls, ["disable"])

    def test_ensure_motors_enabled_is_called_and_best_effort(self):
        agent = self._agent()
        self.assertTrue(_run(NS["_ensure_motors_enabled"](agent)))
        self.assertEqual(agent.calls, ["enable"])
        self.assertTrue(agent.state["motors_enabled"])

    def test_ensure_motors_enabled_skips_when_sdk_lacks_it(self):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = types.SimpleNamespace()  # no enable_motors method
        self.assertFalse(_run(NS["_ensure_motors_enabled"](agent)))

    def test_wake_enables_motors_before_moving(self):
        agent = self._agent()
        order = []
        mini = agent.state["mini"]
        mini.enable_motors = lambda *a, **k: order.append("enable")
        mini.wake_up = lambda *a, **k: order.append("wake")
        agent.state["motion_lock"] = asyncio.Lock()
        agent.state["busy"] = False
        _run(NS["_dispatch"](agent, "wake", {}, return_result=True))
        self.assertEqual(order, ["enable", "wake"])  # torque on, THEN move


class ShutupTest(unittest.TestCase):
    """'shut up' must cut the current utterance immediately."""

    def _agent(self):
        calls = []
        agent = FakeAgent(FakeMedia())
        audio = types.SimpleNamespace(stop_playing=lambda: calls.append("stop_playing"))
        agent.state["mini"] = types.SimpleNamespace(media=types.SimpleNamespace(audio=audio))
        agent.calls = calls
        return agent

    def test_shutup_stops_playback_and_sets_flag(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "shutup", {}, return_result=True))
        self.assertTrue(res["stopped_speaking"])
        self.assertEqual(agent.calls, ["stop_playing"])
        self.assertTrue(agent.state["stop_speaking"])  # in-flight say will bail
        self.assertFalse(agent.state["_speaking"])

    def test_stop_command_also_cuts_audio(self):
        agent = self._agent()
        agent.state["mini"].stop = lambda: None  # SDK motion-stop present
        res = _run(NS["_dispatch"](agent, "stop", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertIn("stop_playing", agent.calls)  # speech cut too

    def test_robot_backend_prefers_direct_daemon_stop(self):
        agent = self._agent()
        direct = mock.AsyncMock(return_value=True)
        with mock.patch.dict(NS, {"_stop_daemon_sound": direct}):
            res = _run(NS["_dispatch"](agent, "shutup", {}, return_result=True))
        self.assertTrue(res["stopped_speaking"])
        direct.assert_awaited_once_with(agent)
        self.assertEqual(agent.calls, [])

    def test_shutup_when_nothing_playing_is_graceful(self):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = types.SimpleNamespace(media=types.SimpleNamespace(audio=None))
        res = _run(NS["_dispatch"](agent, "shutup", {}, return_result=True))
        self.assertFalse(res["stopped_speaking"])
        self.assertTrue(agent.state["stop_speaking"])


class DiagCommandTest(unittest.TestCase):
    """`diag` turns 'he didn't move' into signal: daemon version vs SDK, and a
    real move-then-recheck of joint angles."""

    def _agent(self, daemon_version, positions_before, positions_after):
        agent = FakeAgent(FakeMedia())
        seq = [positions_before, positions_after]

        def get_positions():
            return (seq.pop(0) if len(seq) > 1 else seq[0], None)

        mini = types.SimpleNamespace(
            client=types.SimpleNamespace(
                get_status=lambda: types.SimpleNamespace(
                    version=daemon_version, wlan_ip="10.0.0.5", no_media=False
                )
            ),
            enable_motors=lambda *a, **k: None,
            get_current_joint_positions=get_positions,
            goto_target=lambda **k: None,
        )
        agent.state["mini"] = mini
        agent.state["create_head_pose"] = lambda **k: "HEAD"
        agent.state["np"] = np
        agent.state["connection_mode"] = "network"
        return agent

    def test_flags_version_mismatch(self):
        import reachy_mini as rm

        other = "9.9.9" if rm.__version__ != "9.9.9" else "0.0.0"
        agent = self._agent(other, [0.0] * 7, [0.0] * 7)
        res = _run(NS["_dispatch"](agent, "diag", {}, return_result=True))
        self.assertTrue(res["version_mismatch"])
        self.assertEqual(res["daemon_version"], other)
        self.assertIn("does not match", res["result"])

    def test_reports_no_movement_when_angles_unchanged(self):
        import reachy_mini as rm

        agent = self._agent(rm.__version__, [0.0] * 7, [0.0] * 7)  # matched version
        res = _run(NS["_dispatch"](agent, "diag", {}, return_result=True))
        self.assertFalse(res["version_mismatch"])
        self.assertFalse(res["moved"])
        self.assertIn("did not change", res["result"])

    def test_reports_movement_when_angles_change(self):
        import reachy_mini as rm

        agent = self._agent(rm.__version__, [0.0] * 7, [0.0, 0.3, 0.0, 0, 0, 0, 0])
        res = _run(NS["_dispatch"](agent, "diag", {}, return_result=True))
        self.assertTrue(res["moved"])
        self.assertGreater(res["joint_delta_max_rad"], 0.01)


class CommandEventPayloadTest(unittest.TestCase):
    """The shared MQTT event retains each handler's command-specific result."""

    @staticmethod
    def _event(agent):
        events = [payload for topic, payload in agent.published if topic == "custom/reachy/events"]
        if len(events) != 1:
            raise AssertionError(f"expected one command event, got {events!r}")
        return events[0]

    def test_doa_event_preserves_handler_result(self):
        agent = FakeAgent(FakeMedia(doa=(math.pi / 2, True)))

        _run(NS["_dispatch"](agent, "doa", {}))

        event = self._event(agent)
        self.assertEqual(event["type"], "doa")
        self.assertTrue(event["ok"])
        self.assertIsInstance(event["ts"], float)
        self.assertTrue(event["detected"])
        self.assertAlmostEqual(event["angle_deg"], 90.0, places=6)
        self.assertTrue(event["voice_detected"])
        self.assertIn("Sound from", event["result"])

    def test_listen_event_preserves_handler_result(self):
        agent = FakeAgent(FakeMedia(samplerate=16000, channels=1, doa=(math.radians(42.0), True)))

        _run(NS["_dispatch"](agent, "listen", {"duration": 0.1, "include_b64": False}))

        event = self._event(agent)
        self.assertEqual(event["type"], "listen")
        self.assertTrue(event["ok"])
        self.assertIsInstance(event["ts"], float)
        self.assertEqual(event["samplerate"], 16000)
        self.assertEqual(event["channels"], 1)
        self.assertEqual(event["format"], "wav")
        self.assertGreater(event["frames"], 0)
        self.assertAlmostEqual(event["doa_deg"], 42.0, places=6)
        self.assertNotIn("audio_b64", event)

    def test_diag_event_preserves_handler_result(self):
        import reachy_mini as rm

        agent = DiagCommandTest()._agent(
            rm.__version__, [0.0] * 7, [0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0]
        )
        with mock.patch.object(NS["asyncio"], "sleep", new=mock.AsyncMock()):
            _run(NS["_dispatch"](agent, "diag", {}))

        event = self._event(agent)
        self.assertEqual(event["type"], "diag")
        self.assertTrue(event["ok"])
        self.assertIsInstance(event["ts"], float)
        self.assertEqual(event["sdk_version"], rm.__version__)
        self.assertEqual(event["daemon_version"], rm.__version__)
        self.assertTrue(event["moved"])
        self.assertGreater(event["joint_delta_max_rad"], 0.01)
        self.assertIn("Robot moved", event["result"])

    def test_failure_event_keeps_error_envelope(self):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = types.SimpleNamespace(media=NoMicMedia())

        _run(NS["_dispatch"](agent, "doa", {}))

        event = self._event(agent)
        self.assertEqual(event["type"], "doa")
        self.assertFalse(event["ok"])
        self.assertIsInstance(event["ts"], float)
        self.assertIn("microphone array is unavailable", event["error"].lower())


class BridgeToMainTest(unittest.TestCase):
    """Reachy-as-interface: input that isn't a robot/HA command is piped to the
    main orchestrator and the answer comes back. Robot offline here, so we
    exercise the routing without the TTS/audio path."""

    def _agent(self, send_to_impl):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = None  # disconnected -> no speech, text still returned
        agent.sent = []

        async def send_to(name, payload, timeout=60.0):
            agent.sent.append((name, payload))
            return send_to_impl(payload)

        agent.send_to = send_to
        return agent

    def test_bridges_unhandled_text_and_returns_orchestrator_reply(self):
        agent = self._agent(lambda _p: {"text": "It is sunny in Paris."})
        res = _run(NS["_bridge_to_main"](agent, "what's the weather in Paris?", "tid1"))
        self.assertTrue(res["ok"])
        self.assertTrue(res["bridged"])
        self.assertFalse(res["spoke"])  # robot offline -> not spoken aloud
        self.assertEqual(res["result"], "It is sunny in Paris.")
        # It routed to main, via the interface flag, with the user's text.
        name, payload = agent.sent[0]
        self.assertEqual(name, "main")
        self.assertTrue(payload["_via_interface"])
        self.assertEqual(payload["_interface_source"], "reachy-mini")
        self.assertEqual(payload["text"], "what's the weather in Paris?")

    def test_bridge_keeps_current_request_plain_and_history_structured(self):
        agent = self._agent(lambda _p: {"text": "Okay."})
        history = [
            {
                "transcript": "turn off the light",
                "response": "Okay, the light is off.",
            }
        ]

        _run(NS["_bridge_to_main"](agent, "Yes", "tid-context", conversation_history=history))

        payload = agent.sent[0][1]
        self.assertEqual(payload["text"], "Yes")

    def test_handle_task_rejects_invented_say_and_bridges_to_main(self):
        agent = self._agent(lambda _p: {"text": "It is 8:45 PM in Athens."})

        async def invented_say(_agent, _text):
            return [{"cmd": "say", "text": "It is 8:45 PM in Athens."}]

        with mock.patch.dict(NS, {"_nl_to_commands": invented_say}):
            res = _run(
                NS["handle_task"](
                    agent,
                    {"text": "What time is it in Athens?", "_task_id": "tid2"},
                )
            )

        self.assertTrue(res["bridged"])
        self.assertEqual(res["cmd"], "bridge")
        self.assertEqual(res["result"], "It is 8:45 PM in Athens.")
        self.assertEqual(agent.sent[0][0], "main")
        self.assertEqual(agent.sent[0][1]["text"], "What time is it in Athens?")

    def test_only_explicit_speech_requests_keep_a_say_plan(self):
        f = NS["_is_invented_say_plan"]
        plan = [{"cmd": "say", "text": "hello"}]

        self.assertFalse(f(plan, "say hello"))
        self.assertFalse(f(plan, "Can you please announce dinner is ready?"))
        self.assertTrue(f(plan, "What time is it in Athens?"))
        self.assertTrue(f(plan, "What did you say yesterday?"))

    def test_ask_wactorz_prefix_bypasses_robot_planner(self):
        agent = self._agent(lambda _p: {"text": "Four."})

        async def must_not_plan(_agent, _text):
            self.fail("explicit interface request reached the robot planner")

        with mock.patch.dict(NS, {"_nl_to_commands": must_not_plan}):
            res = _run(
                NS["handle_task"](
                    agent,
                    {"text": "ask Wactorz what is two plus two?", "_task_id": "tid3"},
                )
            )

        self.assertTrue(res["bridged"])
        self.assertEqual(res["result"], "Four.")
        self.assertEqual(agent.sent[0][1]["text"], "what is two plus two?")

    def test_explicit_interface_prefix_variants(self):
        parse = NS["_explicit_interface_request"]
        self.assertEqual(parse("Wactorz, check my calendar"), "check my calendar")
        self.assertEqual(parse("use Wactorz to check the weather"), "check the weather")
        self.assertIsNone(parse("wiggle your antennas"))

    def test_returns_none_when_main_unreachable(self):
        agent = self._agent(lambda _p: {"error": "Agent 'main' not found"})
        self.assertIsNone(_run(NS["_bridge_to_main"](agent, "hi", "t")))

    def test_returns_none_on_empty_reply(self):
        agent = self._agent(lambda _p: {"text": "   "})
        self.assertIsNone(_run(NS["_bridge_to_main"](agent, "hi", "t")))


class NoMicMedia:
    """A media manager with a camera but NO mic array — models a 'no_media'-style
    backend: get_frame works, but recording / DoA methods are absent."""

    def __init__(self, frame=None):
        self._frame = frame

    def get_frame(self):
        return self._frame


class MicBackendGuardTest(unittest.TestCase):
    """listen and doa must refuse with an actionable
    message when the media backend has no mic array — never an opaque
    AttributeError or a silent success."""

    def _agent(self):
        agent = FakeAgent(FakeMedia())
        agent.state["mini"] = types.SimpleNamespace(media=NoMicMedia(frame=None))
        return agent

    def test_listen_without_mic_backend_is_actionable(self):
        res = _run(NS["_dispatch"](self._agent(), "listen", {"duration": 0.1}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("microphone array is unavailable", res["error"].lower())
        self.assertIn("get_audio_sample", res["error"])  # names the missing capability

    def test_doa_without_mic_backend_is_actionable(self):
        res = _run(NS["_dispatch"](self._agent(), "doa", {}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("microphone array is unavailable", res["error"].lower())
        self.assertIn("get_DoA", res["error"])

    def test_require_mic_ignores_output_audio_object(self):
        # A working-mic backend can still have media.audio (playback) == None;
        # mic input capability must be judged only by the input methods present.
        media = FakeMedia()
        media.audio = None
        agent = FakeAgent(media)
        self.assertIs(NS["_require_mic"](agent, record=True, doa=True), media)


class ListenNoSamplesTest(unittest.TestCase):
    """A live-but-silent mic (get_audio_sample never yields) must fail loudly, not
    return ok:true with a 0-second WAV."""

    def test_no_samples_is_clear_failure(self):
        media = FakeMedia(samplerate=16000, channels=1)
        media.get_audio_sample = lambda: None  # never produces a sample
        agent = FakeAgent(media)
        res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("no audio", res["error"].lower())
        self.assertNotIn("audio_b64", res)  # no bogus zero-length blob handed back


class DoaExceptionTest(unittest.TestCase):
    """get_DoA failures are logged with context and surfaced — never swallowed."""

    def _agent(self):
        media = FakeMedia(doa=(0.0, True))

        def boom():
            raise RuntimeError("array offline")

        media.get_DoA = boom
        return FakeAgent(media)

    def test_doa_command_surfaces_and_logs_exception(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "doa", {}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("direction-of-arrival", res["error"].lower())
        self.assertTrue(any("get_DoA failed" in m for m in agent.logs))

    def test_listen_keeps_audio_when_doa_read_fails(self):
        # DoA is a best-effort supplement to a listen clip: its failure is logged
        # and reported, but the recorded audio is still returned.
        media = FakeMedia(samplerate=16000, channels=1)

        def boom():
            raise RuntimeError("array offline")

        media.get_DoA = boom
        agent = FakeAgent(media)
        res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertIn("audio_b64", res)
        self.assertIn("doa_error", res)
        self.assertTrue(any("DoA read failed" in m for m in agent.logs))


class EmptyDoaTest(unittest.TestCase):
    """None / empty tuple / empty list from get_DoA all mean 'no sound localized'
    and must never be indexed (doa[0]) across any mic command."""

    def test_doa_command_handles_empty(self):
        for empty in (None, (), []):
            agent = FakeAgent(FakeMedia(doa=empty))
            res = _run(NS["_dispatch"](agent, "doa", {}, return_result=True))
            self.assertTrue(res["ok"], msg=f"doa={empty!r}")
            self.assertFalse(res["detected"])

    def test_listen_handles_empty_doa(self):
        for empty in (None, (), []):
            agent = FakeAgent(FakeMedia(samplerate=16000, channels=1, doa=empty))
            res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
            self.assertTrue(res["ok"], msg=f"doa={empty!r}")
            self.assertNotIn("doa_deg", res)  # nothing localized
            self.assertNotIn("doa_error", res)  # and it wasn't an error, just empty


class DoaAngleDegTest(unittest.TestCase):
    """The single DoA-unit boundary: SDK radians -> degrees, with validation.

    reachy_mini 1.8.1 reports DoA in radians (ReSpeaker DOA_VALUE_RADIANS); this
    helper is the ONLY place that conversion happens."""

    def setUp(self):
        self.f = NS["_doa_angle_deg"]

    def test_zero(self):
        self.assertAlmostEqual(self.f((0.0, True)), 0.0, places=9)

    def test_half_pi_is_ninety(self):
        self.assertAlmostEqual(self.f((math.pi / 2, True)), 90.0, places=9)

    def test_negative_half_pi_is_minus_ninety(self):
        self.assertAlmostEqual(self.f((-math.pi / 2, False)), -90.0, places=9)

    def test_pi_is_one_eighty(self):
        self.assertAlmostEqual(self.f((math.pi, True)), 180.0, places=9)

    def test_nan_raises(self):
        with self.assertRaises(ValueError):
            self.f((float("nan"), True))

    def test_positive_infinity_raises(self):
        with self.assertRaises(ValueError):
            self.f((float("inf"), True))

    def test_negative_infinity_raises(self):
        with self.assertRaises(ValueError):
            self.f((float("-inf"), True))

    def test_unreadable_value_raises(self):
        for bad in (("north", True), (None, True), (), object()):
            with self.assertRaises(ValueError, msg=f"doa={bad!r}"):
                self.f(bad)


class AudioCaptureWindowTest(unittest.TestCase):
    def test_trim_keeps_newest_requested_window_for_interleaved_stereo(self):
        audio = np.arange(40, dtype=np.float32)
        trimmed = NS["_trim_audio_window"](audio, samplerate=10, channels=2, duration=1)
        np.testing.assert_array_equal(trimmed, np.arange(20, 40, dtype=np.float32))

    def test_sample_cap_does_not_end_capture_window_early(self):
        media = FakeMedia(samplerate=16000, channels=1)
        started = __import__("time").monotonic()
        audio, _sr, _ch = NS["_record_audio_blocking"](media, 0.05, 64)
        elapsed = __import__("time").monotonic() - started

        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLessEqual(len(audio), 64)
        self.assertFalse(media.recording)


class AskVoiceCommandTest(unittest.TestCase):
    @staticmethod
    def _clip():
        b64, frames = NS["_pcm_to_wav_b64"](np.full(1600, 0.1, dtype=np.float32), 16000, 1)
        return {
            "audio_b64": b64,
            "duration_s": frames / 16000,
            "capture_duration_s": 0.12,
        }

    @staticmethod
    def _event(agent):
        return [payload for topic, payload in agent.published if topic == "custom/reachy/events"][
            -1
        ]

    def test_integration_wav_to_transcript_to_main_to_spoken_answer(self):
        agent = FakeAgent(FakeMedia())
        agent.name = "reachy-mini"
        agent.sent = []

        async def send_to(name, payload, timeout=60.0):
            agent.sent.append((name, payload, timeout))
            return {"text": "The living-room light is on."}

        async def fake_listen(_agent, payload):
            self.assertEqual(payload["duration"], 5)
            return self._clip()

        async def fake_transcribe(wav_bytes, payload):
            self.assertTrue(wav_bytes.startswith(b"RIFF"))
            self.assertEqual(payload["stt_backend"], "whisper")
            from wactorz.catalogue_agents.reachy_stt import Transcription

            return Transcription("turn on the living-room light", "whisper", "tiny")

        spoken = mock.AsyncMock(return_value={"said": "The living-room light is on."})
        agent.send_to = send_to
        with (
            mock.patch.dict(NS, {"_listen": fake_listen, "_say": spoken}),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", new=fake_transcribe),
        ):
            res = _run(
                NS["_dispatch"](
                    agent,
                    "ask_voice",
                    {"duration": 5, "stt_backend": "whisper", "stt_model": "tiny"},
                    return_result=True,
                )
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["transcript"], "turn on the living-room light")
        self.assertEqual(res["response_text"], "The living-room light is on.")
        self.assertEqual(res["stt_backend"], "whisper")
        self.assertEqual(agent.sent[0][0], "main")
        self.assertTrue(agent.sent[0][1]["_via_interface"])
        self.assertTrue(agent.sent[0][1]["_interface_voice"])
        spoken.assert_awaited_once()
        self.assertEqual(spoken.await_args.args[1]["text"], "The living-room light is on.")
        event = self._event(agent)
        for field in (
            "transcript",
            "capture_duration_s",
            "transcription_duration_s",
            "response_text",
            "total_duration_s",
            "ok",
            "error",
            "type",
            "ts",
        ):
            self.assertIn(field, event)
        self.assertEqual(event["type"], "ask_voice")
        self.assertTrue(event["ok"])

    def test_capture_failure_is_structured(self):
        async def fail_capture(_agent, _payload):
            raise RuntimeError("microphone unavailable")

        agent = FakeAgent(FakeMedia())
        with mock.patch.dict(NS, {"_listen": fail_capture}):
            res = _run(NS["_dispatch"](agent, "ask_voice", {}, return_result=True))

        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "capture_failed")
        self.assertIn("capture_failed", res["error"])
        self.assertIn("microphone unavailable", res["error"])
        self.assertEqual(self._event(agent)["type"], "ask_voice")

    def test_transcription_failure_is_structured(self):
        async def fake_listen(_agent, _payload):
            return self._clip()

        async def fail_transcription(_wav, _payload):
            raise RuntimeError("model missing")

        agent = FakeAgent(FakeMedia())
        with (
            mock.patch.dict(NS, {"_listen": fake_listen}),
            mock.patch(
                "wactorz.catalogue_agents.reachy_stt.transcribe_wav", new=fail_transcription
            ),
        ):
            res = _run(NS["_dispatch"](agent, "ask_voice", {}, return_result=True))

        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "transcription_failed")
        self.assertIn("model missing", res["error"])
        self.assertEqual(res["capture_duration_s"], 0.12)

    def test_empty_transcript_is_structured(self):
        async def fake_listen(_agent, _payload):
            return self._clip()

        async def empty(_wav, _payload):
            from wactorz.catalogue_agents.reachy_stt import Transcription

            return Transcription("   ", "faster-whisper", "base")

        agent = FakeAgent(FakeMedia())
        with (
            mock.patch.dict(NS, {"_listen": fake_listen}),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", new=empty),
        ):
            res = _run(NS["_dispatch"](agent, "ask_voice", {}, return_result=True))

        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "empty_transcript")

    def test_routing_failure_preserves_transcript(self):
        async def fake_listen(_agent, _payload):
            return self._clip()

        async def transcribe(_wav, _payload):
            from wactorz.catalogue_agents.reachy_stt import Transcription

            return Transcription("what time is it", "faster-whisper", "base")

        async def no_main(_agent, _text, _task_id):
            return None

        agent = FakeAgent(FakeMedia())
        with (
            mock.patch.dict(NS, {"_listen": fake_listen, "_bridge_to_main": no_main}),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", new=transcribe),
        ):
            res = _run(NS["_dispatch"](agent, "ask_voice", {}, return_result=True))

        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "routing_failed")
        self.assertEqual(res["transcript"], "what time is it")

    def test_speech_failure_preserves_main_response(self):
        async def fake_listen(_agent, _payload):
            return self._clip()

        async def transcribe(_wav, _payload):
            from wactorz.catalogue_agents.reachy_stt import Transcription

            return Transcription("hello", "faster-whisper", "base")

        async def bridge(_agent, _text, _task_id, **_kwargs):
            return {"result": "Hello there", "spoke": False, "speech_error": "speaker offline"}

        agent = FakeAgent(FakeMedia())
        with (
            mock.patch.dict(NS, {"_listen": fake_listen, "_bridge_to_main": bridge}),
            mock.patch("wactorz.catalogue_agents.reachy_stt.transcribe_wav", new=transcribe),
        ):
            res = _run(NS["_dispatch"](agent, "ask_voice", {}, return_result=True))

        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "speech_failed")
        self.assertEqual(res["response_text"], "Hello there")
        self.assertEqual(res["result"], "Hello there")
        self.assertIn("speaker offline", res["error"])

    def test_chat_phrase_dispatches_push_to_talk_without_planner(self):
        agent = FakeAgent(FakeMedia())

        async def fake_ask(_agent, _payload):
            return {"response_text": "Done", "result": "Done", "spoke": True}

        async def must_not_plan(_agent, _text):
            self.fail("push-to-talk phrase reached the robot planner")

        with mock.patch.dict(NS, {"_ask_voice": fake_ask, "_nl_to_commands": must_not_plan}):
            res = _run(
                NS["handle_task"](agent, {"text": "listen and ask Wactorz", "_task_id": "voice1"})
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["cmd"], "ask_voice")
        self.assertEqual(res["response_text"], "Done")


if __name__ == "__main__":
    unittest.main()
