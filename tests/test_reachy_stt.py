"""Provider-level tests for configurable Reachy speech-to-text backends."""

import asyncio
import os
import sys
import threading
import types
import unittest
from unittest import mock

import numpy as np

from wactorz.catalogue_agents import reachy_stt
from wactorz.catalogue_agents.reachy_vad import VADConfig


class STTConfigTest(unittest.TestCase):
    def test_defaults_to_deepgram_nova_3(self):
        config = reachy_stt.STTConfig.resolve({}, {})
        self.assertEqual(config.backend, "deepgram")
        self.assertEqual(config.model, "nova-3")
        self.assertEqual(config.timeout_s, 60.0)

    def test_payload_overrides_environment(self):
        config = reachy_stt.STTConfig.resolve(
            {
                "stt_backend": "whisper",
                "stt_model": "tiny",
                "language": "el",
                "stt_hotwords": "Reachy, Wactorz",
            },
            {"REACHY_STT_BACKEND": "openai", "REACHY_STT_MODEL": "whisper-1"},
        )
        self.assertEqual(config.backend, "whisper")
        self.assertEqual(config.model, "tiny")
        self.assertEqual(config.language, "el")
        self.assertEqual(config.hotwords, "Reachy, Wactorz")

    def test_deepgram_alias_and_timeout_are_resolved(self):
        config = reachy_stt.STTConfig.resolve({"stt_backend": "nova-3", "stt_timeout_s": 15}, {})
        self.assertEqual(config.backend, "deepgram")
        self.assertEqual(config.model, "nova-3")
        self.assertEqual(config.timeout_s, 15.0)

    def test_deepgram_prerecorded_auto_detects_when_language_is_unset(self):
        config = reachy_stt.STTConfig.resolve({}, {})
        self.assertTrue(config.detect_language)

    def test_an_explicit_language_disables_detection_by_default(self):
        config = reachy_stt.STTConfig.resolve({"stt_language": "el"}, {})
        self.assertFalse(config.detect_language)

    def test_unknown_backend_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "unknown Reachy STT backend"):
            reachy_stt.STTConfig.resolve({"stt_backend": "mystery"}, {})


class ProviderAbstractionTest(unittest.TestCase):
    def tearDown(self):
        reachy_stt._LOCAL_MODELS.clear()

    def test_transcribe_wav_dispatches_to_selected_provider(self):
        backend = mock.AsyncMock()
        backend.transcribe.return_value = " hello from reachy "
        with mock.patch.dict(reachy_stt._BACKENDS, {"whisper": backend}):
            result = asyncio.run(
                reachy_stt.transcribe_wav(
                    b"RIFFmock", {"stt_backend": "whisper", "stt_model": "tiny"}
                )
            )

        self.assertEqual(result.text, "hello from reachy")
        self.assertEqual(result.backend, "whisper")
        self.assertEqual(result.model, "tiny")
        backend.transcribe.assert_awaited_once()

    def test_faster_whisper_backend_is_lazy_and_local(self):
        calls = []

        transcribe_calls = []

        class Model:
            def __init__(self, name, **kwargs):
                calls.append((name, kwargs))

            def transcribe(self, path, **kwargs):
                transcribe_calls.append((path, kwargs))
                return [
                    types.SimpleNamespace(
                        text=" local words ",
                        avg_logprob=-0.1,
                        no_speech_prob=0.05,
                    )
                ], types.SimpleNamespace(language="el", language_probability=0.98)

        module = types.SimpleNamespace(WhisperModel=Model)
        config = reachy_stt.STTConfig(
            "faster-whisper", "tiny", "el", "cpu", "int8", "Reachy, Wactorz"
        )
        with mock.patch.dict("sys.modules", {"faster_whisper": module}):
            result = asyncio.run(reachy_stt.FasterWhisperBackend().transcribe(b"RIFFmock", config))

        self.assertEqual(result.text, "local words")
        self.assertAlmostEqual(result.confidence, 0.9048, places=3)
        self.assertEqual(result.no_speech_probability, 0.05)
        self.assertEqual(result.language, "el")
        self.assertIsNone(result.language_probability)
        self.assertEqual(
            transcribe_calls[0][1],
            {
                "language": "el",
                "vad_filter": True,
                "condition_on_previous_text": False,
                "hotwords": "Reachy, Wactorz",
            },
        )
        self.assertEqual(calls, [("tiny", {"device": "cpu", "compute_type": "int8"})])

    def test_whisper_backend_is_lazy_and_local(self):
        model = types.SimpleNamespace(
            transcribe=mock.Mock(return_value={"text": " classic whisper "})
        )
        module = types.SimpleNamespace(load_model=mock.Mock(return_value=model))
        config = reachy_stt.STTConfig("whisper", "base", None, "cpu")
        with mock.patch.dict("sys.modules", {"whisper": module}):
            text = asyncio.run(reachy_stt.WhisperBackend().transcribe(b"RIFFmock", config))

        self.assertEqual(text, "classic whisper")
        module.load_model.assert_called_once_with("base", device="cpu")

    def test_openai_backend_requires_key_without_hard_coding_one(self):
        config = reachy_stt.STTConfig("openai", "whisper-1")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                asyncio.run(reachy_stt.OpenAIBackend().transcribe(b"RIFFmock", config))

    def test_openai_backend_posts_named_wav(self):
        create = mock.AsyncMock(return_value=types.SimpleNamespace(text=" hosted words "))
        client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=create))
        )
        module = types.SimpleNamespace(AsyncOpenAI=mock.Mock(return_value=client))
        config = reachy_stt.STTConfig("openai", "whisper-1", "en")
        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}),
            mock.patch.dict("sys.modules", {"openai": module}),
        ):
            text = asyncio.run(reachy_stt.OpenAIBackend().transcribe(b"RIFFmock", config))

        self.assertEqual(text, "hosted words")
        module.AsyncOpenAI.assert_called_once_with(api_key="test-only")
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["file"], ("reachy.wav", b"RIFFmock", "audio/wav"))
        self.assertEqual(kwargs["model"], "whisper-1")
        self.assertEqual(kwargs["language"], "en")

    def test_deepgram_backend_requires_key(self):
        config = reachy_stt.STTConfig("deepgram", "nova-3")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEEPGRAM_API_KEY"):
                asyncio.run(reachy_stt.DeepgramBackend().transcribe(b"RIFFmock", config))

    def test_deepgram_backend_posts_wav_with_nova_options(self):
        option_calls = []

        class PrerecordedOptions:
            def __init__(self, **kwargs):
                option_calls.append(kwargs)

        transcribe_file = mock.Mock(
            return_value=types.SimpleNamespace(
                results=types.SimpleNamespace(
                    channels=[
                        types.SimpleNamespace(
                            detected_language="el",
                            language_confidence=0.93,
                            alternatives=[
                                types.SimpleNamespace(
                                    transcript=" Γεια σου Reachy ", confidence=0.97
                                )
                            ],
                        )
                    ]
                )
            )
        )
        prerecorded = types.SimpleNamespace(
            v=mock.Mock(return_value=types.SimpleNamespace(transcribe_file=transcribe_file))
        )
        client = types.SimpleNamespace(listen=types.SimpleNamespace(prerecorded=prerecorded))
        deepgram_module = types.SimpleNamespace(
            DeepgramClient=mock.Mock(return_value=client),
            PrerecordedOptions=PrerecordedOptions,
        )
        timeout = object()
        httpx_module = types.SimpleNamespace(Timeout=mock.Mock(return_value=timeout))
        config = reachy_stt.STTConfig(
            "deepgram",
            "nova-3",
            "el",
            hotwords="Reachy, Wactorz, Home Assistant",
            timeout_s=45,
        )
        with (
            mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-only"}),
            mock.patch.dict("sys.modules", {"deepgram": deepgram_module, "httpx": httpx_module}),
        ):
            result = asyncio.run(reachy_stt.DeepgramBackend().transcribe(b"RIFFmock", config))

        self.assertEqual(result.text, "Γεια σου Reachy")
        self.assertEqual(result.confidence, 0.97)
        self.assertEqual(result.language, "el")
        self.assertEqual(result.language_probability, 0.93)
        deepgram_module.DeepgramClient.assert_called_once_with("test-only")
        prerecorded.v.assert_called_once_with("1")
        self.assertEqual(
            option_calls,
            [
                {
                    "model": "nova-3",
                    "punctuate": True,
                    "smart_format": True,
                    "language": "el",
                    "keyterm": ["Reachy", "Wactorz", "Home Assistant"],
                }
            ],
        )
        httpx_module.Timeout.assert_called_once_with(45, connect=10.0)
        transcribe_file.assert_called_once_with(
            {"buffer": b"RIFFmock", "mimetype": "audio/wav"},
            mock.ANY,
            timeout=timeout,
        )

    def test_deepgram_empty_response_fails_clearly(self):
        response = types.SimpleNamespace(results=types.SimpleNamespace(channels=[]))
        with self.assertRaisesRegex(RuntimeError, "no transcription channel"):
            reachy_stt._deepgram_result(response)

    def test_deepgram_batch_request_enables_language_detection(self):
        option_calls = []
        response = types.SimpleNamespace(
            results=types.SimpleNamespace(
                channels=[
                    types.SimpleNamespace(
                        detected_language="el",
                        language_confidence=0.91,
                        alternatives=[types.SimpleNamespace(transcript="γεια", confidence=0.94)],
                    )
                ]
            )
        )
        transcribe_file = mock.Mock(return_value=response)
        client = types.SimpleNamespace(
            listen=types.SimpleNamespace(
                prerecorded=types.SimpleNamespace(
                    v=lambda _version: types.SimpleNamespace(transcribe_file=transcribe_file)
                )
            )
        )
        config = reachy_stt.STTConfig.resolve({}, {})

        reachy_stt._transcribe_deepgram_sync(
            b"RIFFmock",
            config,
            "test-only",
            lambda _key: client,
            lambda **kwargs: option_calls.append(kwargs) or kwargs,
            types.SimpleNamespace(Timeout=lambda *_args, **_kwargs: object()),
        )

        self.assertTrue(option_calls[0]["detect_language"])
        self.assertNotIn("language", option_calls[0])

    def test_deepgram_streams_reachy_pcm_and_stops_on_speech_final(self):
        callbacks = {}
        option_calls = []

        class Events:
            Transcript = "transcript"
            UtteranceEnd = "utterance_end"
            Error = "error"

        class LiveOptions:
            def __init__(self, **kwargs):
                option_calls.append(kwargs)

        class Connection:
            def __init__(self):
                self.frames = []
                self.finished = False

            def on(self, event, callback):
                callbacks[event] = callback

            def start(self, _options):
                return True

            def send(self, frame):
                self.frames.append(frame)
                if len(self.frames) == 4:
                    callbacks[Events.Transcript](
                        types.SimpleNamespace(
                            is_final=True,
                            speech_final=True,
                            channel=types.SimpleNamespace(
                                detected_language="en",
                                language_confidence=0.98,
                                alternatives=[
                                    types.SimpleNamespace(
                                        transcript=" hello Reachy ", confidence=0.96
                                    )
                                ],
                            ),
                        )
                    )

            def finish(self):
                self.finished = True

        class Media:
            def __init__(self):
                self.samples = [np.full((480, 2), 0.3, np.float32)] * 20

            def start_recording(self):
                pass

            def stop_recording(self):
                pass

            def get_audio_sample(self):
                return self.samples.pop(0) if self.samples else None

            def get_input_audio_samplerate(self):
                return 16000

            def get_input_channels(self):
                return 2

        class Vad:
            def __init__(self, _mode):
                pass

            def is_speech(self, _pcm, _samplerate):
                return True

        connection = Connection()
        deepgram_module = types.SimpleNamespace(
            DeepgramClient=mock.Mock(
                return_value=types.SimpleNamespace(
                    listen=types.SimpleNamespace(
                        websocket=types.SimpleNamespace(v=mock.Mock(return_value=connection))
                    )
                )
            ),
            LiveOptions=LiveOptions,
            LiveTranscriptionEvents=Events,
        )
        media = Media()
        with (
            mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-only"}),
            mock.patch.dict(
                sys.modules,
                {"deepgram": deepgram_module, "webrtcvad": types.SimpleNamespace(Vad=Vad)},
            ),
        ):
            turn = reachy_stt.capture_deepgram_turn(
                media,
                threading.Event(),
                VADConfig(flush_s=0, min_speech_s=0.03, pre_roll_s=0),
                {
                    "stt_backend": "deepgram",
                    "stt_language": "en",
                    "stt_endpointing_ms": 500,
                    "stt_utterance_end_ms": 1200,
                },
            )

        self.assertEqual(turn.transcription.text, "hello Reachy")
        self.assertEqual(turn.transcription.backend, "deepgram-streaming")
        self.assertEqual(turn.transcription.confidence, 0.96)
        self.assertIsNone(turn.error)
        self.assertEqual(len(connection.frames), 4)
        self.assertTrue(connection.finished)
        self.assertEqual(option_calls[0]["encoding"], "linear16")
        self.assertEqual(option_calls[0]["endpointing"], 500)
        self.assertEqual(option_calls[0]["utterance_end_ms"], "1200")

    def test_deepgram_finalizes_and_waits_for_a_late_final_result(self):
        callbacks = {}
        option_calls = []

        class Events:
            Transcript = "transcript"
            UtteranceEnd = "utterance_end"
            Error = "error"

        class Connection:
            def __init__(self):
                self.finalized = False
                self.finished = False

            def on(self, event, callback):
                callbacks[event] = callback

            def start(self, _options):
                return True

            def send(self, _frame):
                pass

            def finalize(self):
                self.finalized = True
                callbacks[Events.Transcript](
                    types.SimpleNamespace(
                        is_final=True,
                        speech_final=False,
                        channel=types.SimpleNamespace(
                            detected_language="el",
                            language_confidence=0.97,
                            alternatives=[
                                types.SimpleNamespace(
                                    transcript="κλείσε μου τα φώτα", confidence=0.95
                                )
                            ],
                        ),
                    )
                )
                return True

            def finish(self):
                self.finished = True

        class Media:
            def __init__(self):
                self.samples = [np.full((480, 1), 0.3, np.float32)] * 4 + [
                    np.zeros((480, 1), np.float32)
                ] * 4

            def start_recording(self):
                pass

            def stop_recording(self):
                pass

            def get_audio_sample(self):
                return self.samples.pop(0) if self.samples else None

            def get_input_audio_samplerate(self):
                return 16000

            def get_input_channels(self):
                return 1

        class Vad:
            def __init__(self, _mode):
                pass

            def is_speech(self, pcm, _samplerate):
                return bool(np.max(np.abs(np.frombuffer(pcm, dtype="<i2"))) > 1000)

        connection = Connection()
        module = types.SimpleNamespace(
            DeepgramClient=mock.Mock(
                return_value=types.SimpleNamespace(
                    listen=types.SimpleNamespace(
                        websocket=types.SimpleNamespace(v=mock.Mock(return_value=connection))
                    )
                )
            ),
            LiveOptions=lambda **kwargs: option_calls.append(kwargs) or kwargs,
            LiveTranscriptionEvents=Events,
        )
        with (
            mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-only"}),
            mock.patch.dict(
                sys.modules,
                {"deepgram": module, "webrtcvad": types.SimpleNamespace(Vad=Vad)},
            ),
        ):
            turn = reachy_stt.capture_deepgram_turn(
                Media(),
                threading.Event(),
                VADConfig(flush_s=0, silence_s=0.09, min_speech_s=0.03, pre_roll_s=0),
                {"stt_backend": "deepgram"},
            )

        self.assertTrue(connection.finalized)
        self.assertTrue(connection.finished)
        self.assertEqual(option_calls[0]["language"], "multi")
        self.assertEqual(turn.transcription.text, "κλείσε μου τα φώτα")
        self.assertEqual(turn.transcription.backend, "deepgram-streaming")

    def test_stream_failure_keeps_capture_for_prerecorded_fallback(self):
        callbacks = {}

        class Events:
            Transcript = "transcript"
            UtteranceEnd = "utterance_end"
            Error = "error"

        class Connection:
            def on(self, event, callback):
                callbacks[event] = callback

            def start(self, _options):
                return True

            def send(self, _frame):
                callbacks[Events.Error](RuntimeError("socket lost"))

            def finish(self):
                pass

        class Media:
            def __init__(self):
                self.samples = [np.full((480, 2), 0.3, np.float32)] * 5 + [
                    np.zeros((480, 2), np.float32)
                ] * 4

            def start_recording(self):
                pass

            def stop_recording(self):
                pass

            def get_audio_sample(self):
                return self.samples.pop(0) if self.samples else None

            def get_input_audio_samplerate(self):
                return 16000

            def get_input_channels(self):
                return 2

        class Vad:
            def __init__(self, _mode):
                pass

            def is_speech(self, pcm, _samplerate):
                return bool(np.max(np.abs(np.frombuffer(pcm, dtype="<i2"))) > 1000)

        connection = Connection()
        module = types.SimpleNamespace(
            DeepgramClient=mock.Mock(
                return_value=types.SimpleNamespace(
                    listen=types.SimpleNamespace(
                        websocket=types.SimpleNamespace(v=mock.Mock(return_value=connection))
                    )
                )
            ),
            LiveOptions=lambda **kwargs: kwargs,
            LiveTranscriptionEvents=Events,
        )
        with (
            mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test-only"}),
            mock.patch.dict(
                sys.modules,
                {"deepgram": module, "webrtcvad": types.SimpleNamespace(Vad=Vad)},
            ),
        ):
            turn = reachy_stt.capture_deepgram_turn(
                Media(),
                threading.Event(),
                VADConfig(
                    flush_s=0,
                    silence_s=0.09,
                    min_speech_s=0.03,
                    pre_roll_s=0,
                ),
                {"stt_backend": "deepgram", "stt_language": "en"},
            )

        self.assertGreater(turn.capture.audio.size, 0)
        self.assertIsNone(turn.transcription)
        self.assertIn("socket lost", turn.error)


if __name__ == "__main__":
    unittest.main()
