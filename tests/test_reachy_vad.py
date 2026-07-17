"""Unit tests for incremental Reachy WebRTC VAD capture."""

import sys
import threading
import types
import unittest
from unittest import mock

import numpy as np

from wactorz.catalogue_agents.reachy_vad import VADConfig, capture_utterance, drain_audio


class FakeVad:
    def __init__(self, mode):
        self.mode = mode

    def is_speech(self, pcm, _samplerate):
        frame = np.frombuffer(pcm, dtype="<i2")
        return bool(frame.size and np.max(np.abs(frame)) > 1000)


class Media:
    def __init__(self, samples):
        self.samples = list(samples)
        self.started = self.stopped = False

    def start_recording(self):
        self.started = True

    def stop_recording(self):
        self.stopped = True

    def get_audio_sample(self):
        return self.samples.pop(0) if self.samples else None

    def get_input_audio_samplerate(self):
        return 16000

    def get_input_channels(self):
        return 2


def stereo(value):
    return np.full((480, 2), value, dtype=np.float32)


class ReachyVadTest(unittest.TestCase):
    def setUp(self):
        self.vad_module = types.SimpleNamespace(Vad=FakeVad)

    def test_silence_ends_incremental_utterance(self):
        media = Media([stereo(0)] + [stereo(0.3)] * 5 + [stereo(0)] * 4)
        config = VADConfig(
            flush_s=0, speech_start_timeout_s=0.2, silence_s=0.09, min_speech_s=0.03, pre_roll_s=0
        )
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(media, config=config)
        self.assertTrue(media.started and media.stopped)
        self.assertIsNone(result.stop_reason)
        self.assertEqual((result.samplerate, result.channels), (16000, 1))
        self.assertGreater(result.audio.size, 0)
        self.assertLess(result.duration_s, 1.0)

    def test_inactivity_timeout_has_no_audio(self):
        media = Media([])
        config = VADConfig(flush_s=0, speech_start_timeout_s=0.02)
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(media, config=config)
        self.assertEqual(result.stop_reason, "inactivity_timeout")
        self.assertEqual(result.audio.size, 0)
        self.assertTrue(media.stopped)

    def test_energy_floor_rejects_quiet_frames_labeled_as_speech(self):
        media = Media([stereo(0.04)] * 10)
        config = VADConfig(flush_s=0, speech_start_timeout_s=0.02, min_rms=0.05)
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(media, config=config)
        self.assertEqual(result.stop_reason, "inactivity_timeout")
        self.assertEqual(result.audio.size, 0)


    def test_short_mechanical_burst_is_rejected_before_stt(self):
        media = Media([stereo(0.3)] * 2 + [stereo(0)] * 5)
        config = VADConfig(
            flush_s=0,
            speech_start_timeout_s=0.2,
            silence_s=0.09,
            min_speech_s=0.18,
            pre_roll_s=0.09,
        )
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(media, config=config)
        self.assertEqual(result.stop_reason, "noise_rejected")
        self.assertEqual(result.audio.size, 0)
    def test_cancellation_stops_recording(self):
        media, cancel = Media([stereo(0.3)]), threading.Event()
        cancel.set()
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(media, cancel, VADConfig(flush_s=0))
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertTrue(media.stopped)

    def test_max_utterance_bounds_continuous_speech(self):
        media = Media([stereo(0.3)] * 20)
        config = VADConfig(
            flush_s=0,
            speech_start_timeout_s=0.1,
            max_utterance_s=0.12,
            pre_roll_s=0,
            min_speech_s=0.03,
        )
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(media, config=config)
        self.assertLessEqual(result.duration_s, 0.12)
        self.assertGreater(result.duration_s, 0)

    def test_speech_start_callback_fires_once_at_confirmed_onset(self):
        media = Media([stereo(0)] + [stereo(0.3)] * 5 + [stereo(0)] * 4)
        started = mock.Mock()
        config = VADConfig(
            flush_s=0,
            speech_start_timeout_s=0.2,
            silence_s=0.09,
            min_speech_s=0.03,
            pre_roll_s=0,
        )
        with mock.patch.dict(sys.modules, {"webrtcvad": self.vad_module}):
            result = capture_utterance(
                media,
                config=config,
                on_speech_start=started,
            )
        self.assertGreater(result.audio.size, 0)
        started.assert_called_once()

    def test_drain_discards_queued_samples(self):
        media = Media([stereo(0)] * 100)
        reads = drain_audio(media, 0.01)
        self.assertGreater(reads, 0)
        self.assertLess(len(media.samples), 100)


if __name__ == "__main__":
    unittest.main()
