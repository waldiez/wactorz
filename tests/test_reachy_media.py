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
import types
import unittest
import wave

import numpy as np
from PIL import Image

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE


def _load_recipe_namespace():
    ns: dict = {}
    exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), ns)
    return ns


NS = _load_recipe_namespace()


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
        res = _run(NS["_dispatch"](
            agent, "camera", {"include_b64": False, "publish": True},
            return_result=True))
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
        agent = FakeAgent(FakeMedia(samplerate=16000, channels=1, doa=(42.0, True)))
        res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertEqual(res["samplerate"], 16000)
        self.assertEqual(res["channels"], 1)
        self.assertEqual(res["format"], "wav")
        self.assertGreater(res["frames"], 0)
        self.assertEqual(res["doa_deg"], 42.0)
        self.assertTrue(res["voice_detected"])
        raw = base64.b64decode(res["audio_b64"])
        with wave.open(io.BytesIO(raw), "rb") as w:
            self.assertEqual(w.getframerate(), 16000)

    def test_doa_reports_direction_without_recording(self):
        media = FakeMedia(doa=(90.0, False))
        agent = FakeAgent(media)
        res = _run(NS["_dispatch"](agent, "doa", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["detected"])
        self.assertEqual(res["angle_deg"], 90.0)
        self.assertFalse(res["voice_detected"])
        self.assertFalse(media.recording)  # never started a recording

    def test_listen_result_summary_has_no_base64_blob(self):
        agent = FakeAgent(FakeMedia(samplerate=16000, channels=1, doa=(42.0, True)))
        res = _run(NS["_dispatch"](agent, "listen", {"duration": 0.1}, return_result=True))
        # The human-facing summary is a short line, and the blob lives elsewhere.
        self.assertIn("Recorded", res["result"])
        self.assertIn("42", res["result"])
        self.assertNotIn(res["audio_b64"], res["result"])


class DoaToYawTest(unittest.TestCase):
    def test_passthrough_and_clamp(self):
        f = NS["_doa_to_yaw"]
        self.assertEqual(f(0), 0)
        self.assertEqual(f(45), 45)
        self.assertEqual(f(120, max_yaw=90), 90)
        self.assertEqual(f(-120, max_yaw=90), -90)

    def test_normalises_wraparound(self):
        f = NS["_doa_to_yaw"]
        self.assertEqual(f(200, max_yaw=180), -160)
        self.assertEqual(f(-200, max_yaw=180), 160)

    def test_offset_and_invert(self):
        f = NS["_doa_to_yaw"]
        self.assertEqual(f(10, offset_deg=5), 15)
        self.assertEqual(f(30, invert=True), -30)


class TurnToSoundTest(unittest.TestCase):
    def _agent(self, doa):
        media = FakeMedia(doa=doa)
        calls: list[dict] = []
        agent = FakeAgent(media)
        agent.state["mini"] = types.SimpleNamespace(
            media=media, goto_target=lambda **kw: calls.append(kw))
        agent.state["np"] = np
        agent.state["create_head_pose"] = lambda **kw: ("HEAD", kw)
        agent.state["motion_lock"] = asyncio.Lock()
        agent.state["busy"] = False
        agent.calls = calls
        return agent

    def test_turns_toward_localized_voice(self):
        agent = self._agent((42.0, True))
        res = _run(NS["_dispatch"](agent, "turn_to_sound", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["turned"])
        self.assertAlmostEqual(res["yaw"], 42.0, places=3)
        self.assertEqual(res["angle_deg"], 42.0)
        self.assertTrue(agent.calls)          # goto_target actually called
        self.assertIn("head", agent.calls[0])

    def test_no_doa_does_not_turn(self):
        agent = self._agent(None)
        res = _run(NS["_dispatch"](agent, "turn_to_sound", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertFalse(res["turned"])
        self.assertFalse(agent.calls)         # no motion issued

    def test_require_voice_skips_non_voice(self):
        agent = self._agent((30.0, False))
        res = _run(NS["_dispatch"](
            agent, "turn_to_sound", {"require_voice": True}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertFalse(res["turned"])
        self.assertEqual(res["angle_deg"], 30.0)
        self.assertFalse(agent.calls)


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
        res = _run(NS["_dispatch"](
            agent, "describe", {"detail": True, "say": False}, return_result=True))
        self.assertTrue(res["detail"])
        self.assertEqual(res["said"], "A long, detailed paragraph about the room.")  # no nudge
        self.assertIn("paragraph", llm.calls[0]["system"].lower())  # detailed prompt

    def test_specific_question_gets_no_nudge(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        llm = FakeLLM("Two people.")
        agent = self._agent(frame, llm)
        res = _run(NS["_dispatch"](
            agent, "describe", {"question": "how many people?", "say": False},
            return_result=True))
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
        _run(NS["_dispatch"](
            agent, "describe", {"question": "how many people?", "say": False},
            return_result=True))
        self.assertFalse(NS["_pending_look_closer"](agent))

    def test_offer_expires_after_a_minute(self):
        agent = self._agent(np.zeros((16, 16, 3), dtype=np.uint8), FakeLLM("x"))
        agent.state["_pending_detail"] = __import__("time").time() - 120  # 2 min ago
        self.assertFalse(NS["_pending_look_closer"](agent))

    def test_describe_passes_the_users_question(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        llm = FakeLLM("Two people.")
        agent = self._agent(frame, llm)
        res = _run(NS["_dispatch"](
            agent, "describe", {"question": "how many people?", "say": False},
            return_result=True))
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
        _run(NS["_dispatch"](agent, "describe", {"say": False, "look_duration": 0},
                             return_result=True))
        self.assertTrue(self.poses)
        self.assertEqual((self.poses[0]["pitch"], self.poses[0]["yaw"]), (0.0, 0.0))

    def test_aim_words_tilt_the_head(self):
        agent = self._agent()
        _run(NS["_dispatch"](
            agent, "describe",
            {"question": "look down at my desk", "say": False, "look_duration": 0},
            return_result=True))
        self.assertEqual(self.poses[0]["pitch"], 22)

    def test_orient_false_captures_without_moving(self):
        agent = self._agent()
        _run(NS["_dispatch"](agent, "describe", {"orient": False, "say": False},
                             return_result=True))
        self.assertEqual(self.poses, [])


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
        res = _run(NS["_dispatch"](
            agent, "look_around", {"say": False, "look_duration": 0}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertEqual(res["said"], "A combined room overview.")
        self.assertEqual(res["views"], 4)  # default 4 angles
        # A single multi-image vision call carrying all four frames.
        content = agent.llm.calls[0]["messages"][0]["content"]
        self.assertEqual(len([b for b in content if b["type"] == "image"]), 4)
        self.assertEqual(self.poses[-1], (0, 0))  # head re-centred at the end

    def test_custom_angles(self):
        agent = self._agent("ok")
        res = _run(NS["_dispatch"](
            agent, "look_around",
            {"angles": [[0, 30], [0, -30]], "say": False, "look_duration": 0},
            return_result=True))
        self.assertEqual(res["views"], 2)


class SayPlaybackPadTest(unittest.TestCase):
    def test_waits_out_speech_plus_tail_by_default(self):
        pad = NS["_say_playback_pad"]
        self.assertAlmostEqual(pad(2.0, {}), 2.35, places=3)  # default tail 0.35
        self.assertAlmostEqual(pad(2.0, {"tail_pad": 0.5}), 2.5, places=3)

    def test_no_wait_when_opted_out_or_unknown_duration(self):
        pad = NS["_say_playback_pad"]
        self.assertEqual(pad(2.0, {"await_playback": False}), 0.0)
        self.assertEqual(pad(0, {}), 0.0)
        self.assertEqual(pad(None, {}), 0.0)


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


class TrackSoundYawSplitTest(unittest.TestCase):
    """Pure geometry: how a facing angle is split into body_yaw + head_yaw."""

    def test_small_angle_body_first_head_centered(self):
        body, head = NS["_split_track_yaw"](30.0)
        self.assertAlmostEqual(body, 30.0)
        self.assertAlmostEqual(head, 0.0)  # body covers it; head stays centred

    def test_beyond_body_limit_head_takes_residual(self):
        body, head = NS["_split_track_yaw"](170.0, max_head_yaw=45.0, max_body_yaw=150.0)
        self.assertAlmostEqual(body, 150.0)  # body maxes out
        self.assertAlmostEqual(head, 20.0)   # head covers the remaining 20°

    def test_residual_past_reach_is_clamped(self):
        # 210° normalises to -150°; body maxes at -150, head residual 0.
        body, head = NS["_split_track_yaw"](210.0, max_head_yaw=45.0, max_body_yaw=150.0)
        self.assertAlmostEqual(body, -150.0)
        self.assertAlmostEqual(head, 0.0)


class TrackDecisionTest(unittest.TestCase):
    """Pure planner: when to turn and to what, with deadband + calibration."""

    def test_turns_when_no_prior_target(self):
        d = NS["_track_decision"](60.0, None, 15.0)
        self.assertTrue(d["turn"])
        self.assertAlmostEqual(d["target"], 60.0)

    def test_deadband_suppresses_small_change(self):
        d = NS["_track_decision"](40.0, 35.0, 15.0)  # only 5° away
        self.assertFalse(d["turn"])

    def test_turns_past_deadband(self):
        d = NS["_track_decision"](60.0, 35.0, 15.0)  # 25° away
        self.assertTrue(d["turn"])

    def test_normalises_wrapped_angle(self):
        d = NS["_track_decision"](190.0, None, 15.0)
        self.assertAlmostEqual(d["target"], -170.0)

    def test_offset_and_invert_calibration(self):
        d = NS["_track_decision"](30.0, None, 0.0, offset_deg=10.0, invert=True)
        self.assertAlmostEqual(d["target"], -40.0)  # (30+10) then inverted

    def test_deadband_uses_circular_distance(self):
        # 179 vs -179 is 2° apart on the circle, not 358°.
        d = NS["_track_decision"](179.0, -179.0, 15.0)
        self.assertFalse(d["turn"])


class _TrackAgent(FakeAgent):
    """FakeAgent plus the surface track_sound touches: motion lock + bg tasks."""

    def __init__(self, media):
        super().__init__(media)
        self.state["motion_lock"] = asyncio.Lock()
        self.state["tracking"] = False
        self.state["track_cfg"] = {}
        self.state["track_last_target"] = None
        self.bg: list = []

    def run_in_background(self, coro):
        # Record the scheduled loop but don't run the infinite coroutine here;
        # closing it avoids an "un-awaited coroutine" warning in the test.
        self.bg.append(coro)
        coro.close()
        return None


class TrackStepTest(unittest.TestCase):
    """One tracking iteration against a fake mic, with _pose stubbed to a recorder."""

    def setUp(self):
        self._orig_pose = NS["_pose"]
        self.pose_calls: list = []

        async def _fake_pose(agent, payload):
            self.pose_calls.append(payload)
            return {}

        NS["_pose"] = _fake_pose

    def tearDown(self):
        NS["_pose"] = self._orig_pose

    def _agent(self, doa, cfg=None):
        agent = _TrackAgent(FakeMedia(doa=doa))
        agent.state["track_cfg"] = cfg or {"require_voice": True, "deadband_deg": 15.0}
        return agent

    def test_turns_toward_a_voice(self):
        agent = self._agent((60.0, True))
        d = _run(NS["_track_step"](agent))
        self.assertTrue(d["turn"])
        self.assertEqual(len(self.pose_calls), 1)
        self.assertIn("body_yaw", self.pose_calls[0])
        self.assertAlmostEqual(agent.state["track_last_target"], 60.0)

    def test_ignores_non_voice_when_require_voice(self):
        agent = self._agent((60.0, False))
        d = _run(NS["_track_step"](agent))
        self.assertFalse(d["turn"])
        self.assertEqual(len(self.pose_calls), 0)  # no motor command

    def test_holds_still_within_deadband(self):
        agent = self._agent((60.0, True))
        agent.state["track_last_target"] = 55.0  # already facing ~here
        d = _run(NS["_track_step"](agent))
        self.assertFalse(d["turn"])
        self.assertEqual(len(self.pose_calls), 0)


class TrackSoundToggleTest(unittest.TestCase):
    """The command that turns the continuous behaviour on and off."""

    def _agent(self):
        return _TrackAgent(FakeMedia(doa=(30.0, True)))

    def test_start_enables_and_schedules_one_loop(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "track_sound", {"on": True}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["tracking"])
        self.assertTrue(agent.state["tracking"])
        self.assertEqual(len(agent.bg), 1)  # exactly one background loop

    def test_bare_command_starts_tracking(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "track_sound", {}, return_result=True))
        self.assertTrue(agent.state["tracking"])
        self.assertTrue(res["tracking"])

    def test_second_start_updates_cfg_without_new_loop(self):
        agent = self._agent()
        _run(NS["_dispatch"](agent, "track_sound", {"on": True}, return_result=True))
        _run(NS["_dispatch"](agent, "track_sound", {"on": True, "interval": 0.2}, return_result=True))
        self.assertTrue(agent.state["tracking"])
        self.assertEqual(len(agent.bg), 1)  # no second loop spawned
        self.assertEqual(agent.state["track_cfg"]["interval"], 0.2)

    def test_off_stops_tracking(self):
        agent = self._agent()
        _run(NS["_dispatch"](agent, "track_sound", {"on": True}, return_result=True))
        res = _run(NS["_dispatch"](agent, "track_sound", {"on": False}, return_result=True))
        self.assertFalse(res["tracking"])
        self.assertFalse(agent.state["tracking"])

    def test_stop_alias_stops_tracking(self):
        agent = self._agent()
        _run(NS["_dispatch"](agent, "track_sound", {"on": True}, return_result=True))
        _run(NS["_dispatch"](agent, "track_sound", {"stop": True}, return_result=True))
        self.assertFalse(agent.state["tracking"])

    def test_stop_command_halts_tracking(self):
        # The generic {"cmd":"stop"} abort must also cancel sound tracking.
        agent = self._agent()
        agent.state["mini"] = types.SimpleNamespace(
            media=agent.state["mini"].media, stop=lambda: None
        )
        _run(NS["_dispatch"](agent, "track_sound", {"on": True}, return_result=True))
        self.assertTrue(agent.state["tracking"])
        _run(NS["_dispatch"](agent, "stop", {}, return_result=True))
        self.assertFalse(agent.state["tracking"])


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
        agent.state["tracking"] = True
        res = _run(NS["_dispatch"](agent, "stop", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertIn("stop_playing", agent.calls)   # speech cut too
        self.assertFalse(agent.state["tracking"])    # and tracking halted

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
                    version=daemon_version, wlan_ip="10.0.0.5", no_media=False)),
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
        self.assertEqual(payload["text"], "what's the weather in Paris?")

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
    """listen / doa / turn_to_sound / track_sound must refuse with an actionable
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

    def test_turn_to_sound_without_mic_backend_is_actionable(self):
        res = _run(NS["_dispatch"](self._agent(), "turn_to_sound", {}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("microphone array is unavailable", res["error"].lower())

    def test_track_sound_start_without_mic_backend_refuses(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "track_sound", {"on": True}, return_result=True))
        self.assertFalse(res["ok"])  # a failed START reports ok:false, like its siblings
        self.assertIn("microphone array is unavailable", res["error"].lower())
        self.assertFalse(agent.state.get("tracking"))  # no background loop armed

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

    def test_turn_to_sound_surfaces_doa_exception(self):
        agent = self._agent()
        res = _run(NS["_dispatch"](agent, "turn_to_sound", {}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("direction-of-arrival", res["error"].lower())

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


class TurnToSoundDoaValidationTest(unittest.TestCase):
    """turn_to_sound must validate the DoA before commanding a yaw: None / NaN /
    unreadable readings must not reach the motors."""

    def _agent(self, doa):
        media = FakeMedia(doa=doa)
        agent = FakeAgent(media)
        agent.state["mini"] = types.SimpleNamespace(
            media=media, goto_target=lambda **kw: None)
        agent.state["np"] = np
        agent.state["create_head_pose"] = lambda **kw: ("HEAD", kw)
        agent.state["motion_lock"] = asyncio.Lock()
        agent.state["busy"] = False
        return agent

    def test_none_doa_does_not_turn(self):
        res = _run(NS["_dispatch"](self._agent(None), "turn_to_sound", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertFalse(res["turned"])

    def test_nan_angle_is_indeterminate_not_a_turn(self):
        res = _run(NS["_dispatch"](
            self._agent((float("nan"), True)), "turn_to_sound", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertFalse(res["turned"])
        self.assertIn("indeterminate", res["result"].lower())

    def test_unreadable_doa_is_clear_error(self):
        res = _run(NS["_dispatch"](
            self._agent(("north", True)), "turn_to_sound", {}, return_result=True))
        self.assertFalse(res["ok"])
        self.assertIn("unreadable", res["error"].lower())

    def test_valid_doa_turns_and_reports_angle(self):
        res = _run(NS["_dispatch"](
            self._agent((42.0, True)), "turn_to_sound", {}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["turned"])
        self.assertEqual(res["angle_deg"], 42.0)


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
            self.assertNotIn("doa_deg", res)      # nothing localized
            self.assertNotIn("doa_error", res)    # and it wasn't an error, just empty

    def test_turn_to_sound_handles_empty_doa(self):
        for empty in (None, (), []):
            agent = FakeAgent(FakeMedia(doa=empty))
            res = _run(NS["_dispatch"](agent, "turn_to_sound", {}, return_result=True))
            self.assertTrue(res["ok"], msg=f"doa={empty!r}")
            self.assertFalse(res["turned"])

    def test_track_step_skips_on_empty_doa(self):
        for empty in (None, (), []):
            agent = _TrackAgent(FakeMedia(doa=empty))
            agent.state["track_cfg"] = {"require_voice": True, "deadband_deg": 15.0}
            self.assertIsNone(_run(NS["_track_step"](agent)), msg=f"doa={empty!r}")


if __name__ == "__main__":
    unittest.main()
