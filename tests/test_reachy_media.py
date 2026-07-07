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
        llm = FakeLLM("I see a dim room with a chair.")
        agent = self._agent(frame, llm)
        # say=False so we exercise vision without the TTS/audio path.
        res = _run(NS["_dispatch"](agent, "describe", {"say": False}, return_result=True))
        self.assertTrue(res["ok"])
        self.assertEqual(res["description"], "I see a dim room with a chair.")
        self.assertEqual(res["said"], "I see a dim room with a chair.")
        # The LLM must have received a real base64 JPEG image block.
        content = llm.calls[0]["messages"][0]["content"]
        img = next(b for b in content if b["type"] == "image")
        self.assertEqual(img["source"]["type"], "base64")
        self.assertEqual(img["source"]["media_type"], "image/jpeg")
        self.assertTrue(img["source"]["data"])  # non-empty b64

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


if __name__ == "__main__":
    unittest.main()
