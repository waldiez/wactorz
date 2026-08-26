"""Provider-level tests for configurable Reachy speech-to-text backends."""

import asyncio
import os
import types
import unittest
from pathlib import Path
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


class ModelSourceTest(unittest.TestCase):
    """A settings file has to survive being carried to another machine."""

    def test_model_name_is_left_alone(self):
        self.assertEqual(reachy_stt._resolve_model_source("large-v3-turbo"), "large-v3-turbo")

    def test_repository_id_is_left_alone(self):
        self.assertEqual(
            reachy_stt._resolve_model_source("Systran/faster-whisper-base"),
            "Systran/faster-whisper-base",
        )

    def test_existing_path_is_used_as_given(
        self,
    ):
        with mock.patch.object(Path, "exists", return_value=True):
            resolved = reachy_stt._resolve_model_source("/models/faster-whisper-base")
        self.assertEqual(Path(resolved), Path("/models/faster-whisper-base"))

    def test_missing_cache_path_falls_back_to_its_repository(self):
        stale = (
            "C:/Users/someone-else/.cache/huggingface/hub/"
            "models--Infomaniak-AI--faster-whisper-large-v3-turbo/snapshots/d94d07e"
        )
        self.assertEqual(
            reachy_stt._resolve_model_source(stale),
            "Infomaniak-AI/faster-whisper-large-v3-turbo",
        )

    def test_missing_unrecoverable_path_names_what_to_set(self):
        with self.assertRaisesRegex(RuntimeError, "REACHY_STT_MODEL"):
            reachy_stt._resolve_model_source("/opt/models/my-own-whisper")

    def test_empty_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            reachy_stt._resolve_model_source("   ")


class PlacementTest(unittest.TestCase):
    """The same configuration has to run on a GPU box, a laptop and a Pi."""

    def setUp(self):
        self._restore = reachy_stt._CUDA_DISABLED
        reachy_stt._CUDA_DISABLED = False

    def tearDown(self):
        reachy_stt._CUDA_DISABLED = self._restore

    def test_auto_uses_cuda_when_it_is_usable(self):
        with mock.patch.object(reachy_stt, "_cuda_is_usable", return_value=True):
            self.assertEqual(reachy_stt._resolve_placement("auto", "float16"), ("cuda", "float16"))

    def test_auto_falls_back_to_cpu_without_cuda(self):
        with mock.patch.object(reachy_stt, "_cuda_is_usable", return_value=False):
            self.assertEqual(reachy_stt._resolve_placement("auto", "default"), ("cpu", "default"))

    def test_explicit_cuda_is_demoted_rather_than_raising(self):
        with mock.patch.object(reachy_stt, "_cuda_is_usable", return_value=False):
            device, compute_type = reachy_stt._resolve_placement("cuda", "float16")
        self.assertEqual((device, compute_type), ("cpu", "int8"))

    def test_cpu_is_never_promoted(self):
        with mock.patch.object(reachy_stt, "_cuda_is_usable", return_value=True):
            self.assertEqual(reachy_stt._resolve_placement("cpu", "int8"), ("cpu", "int8"))

    def test_cuda_is_not_retried_once_it_has_failed(self):
        reachy_stt._disable_cuda(RuntimeError("Library cublas64_12.dll is not found"))
        self.assertFalse(reachy_stt._cuda_is_usable())

    def test_missing_cuda_libraries_are_recognised(self):
        self.assertTrue(
            reachy_stt._looks_like_cuda_failure(
                RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            )
        )
        self.assertFalse(reachy_stt._looks_like_cuda_failure(RuntimeError("WAV payload is empty")))


class CudaFallbackTest(unittest.TestCase):
    """A GPU that reports itself but cannot decode must not silence the robot."""

    def setUp(self):
        self._restore = reachy_stt._CUDA_DISABLED
        reachy_stt._CUDA_DISABLED = False

    def tearDown(self):
        reachy_stt._CUDA_DISABLED = self._restore
        reachy_stt._LOCAL_MODELS.clear()

    def test_transcription_retries_on_cpu_when_cuda_cannot_decode(self):
        built = []

        class Model:
            def __init__(self, name, **kwargs):
                self.device = kwargs["device"]
                built.append((name, kwargs))

            def transcribe(self, path, **kwargs):
                if self.device == "cuda":

                    def fail():
                        raise RuntimeError("Library cublas64_12.dll is not found")
                        yield  # pragma: no cover - generator marker

                    return fail(), types.SimpleNamespace(language="en", language_probability=0.9)
                return [
                    types.SimpleNamespace(text=" cpu words ", avg_logprob=-0.1, no_speech_prob=0.05)
                ], types.SimpleNamespace(language="en", language_probability=0.9)

        module = types.SimpleNamespace(WhisperModel=Model)
        config = reachy_stt.STTConfig("faster-whisper", "base", None, "cuda", "float16")
        with (
            mock.patch.dict("sys.modules", {"faster_whisper": module}),
            mock.patch.object(reachy_stt, "_cuda_is_usable", side_effect=[True, False]),
        ):
            result = asyncio.run(reachy_stt.FasterWhisperBackend().transcribe(b"RIFFmock", config))

        self.assertEqual(result.text, "cpu words")
        self.assertEqual(
            [kwargs["device"] for _name, kwargs in built],
            ["cuda", "cpu"],
        )
        self.assertEqual(built[1][1]["compute_type"], "int8")
        self.assertTrue(reachy_stt._CUDA_DISABLED)

    def test_unrelated_failures_are_not_retried(self):
        attempts = []

        class Model:
            def __init__(self, name, **kwargs):
                attempts.append(kwargs["device"])

            def transcribe(self, path, **kwargs):
                raise ValueError("audio file is not a WAV")

        module = types.SimpleNamespace(WhisperModel=Model)
        config = reachy_stt.STTConfig("faster-whisper", "base", None, "cpu", "int8")
        with mock.patch.dict("sys.modules", {"faster_whisper": module}):
            with self.assertRaisesRegex(ValueError, "not a WAV"):
                asyncio.run(reachy_stt.FasterWhisperBackend().transcribe(b"RIFFmock", config))

        self.assertEqual(attempts, ["cpu"])


if __name__ == "__main__":
    unittest.main()
