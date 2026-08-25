"""Provider-level tests for configurable Reachy speech-to-text backends."""

import asyncio
import os
import types
import unittest
from unittest import mock

from wactorz.catalogue_agents import reachy_stt


class STTConfigTest(unittest.TestCase):
    def test_defaults_to_local_faster_whisper(self):
        config = reachy_stt.STTConfig.resolve({}, {})
        self.assertEqual(config.backend, "faster-whisper")
        self.assertEqual(config.model, "base")

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


if __name__ == "__main__":
    unittest.main()
