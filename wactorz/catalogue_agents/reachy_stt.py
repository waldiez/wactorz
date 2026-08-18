"""Configurable speech-to-text backends for the Reachy Mini voice interface.

All optional dependencies are imported lazily. Selecting a hosted backend is
explicit: the default is local ``faster-whisper`` and audio is never uploaded
merely because an API key happens to exist in the environment.
"""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_DEFAULT_MODELS = {
    "faster-whisper": "base",
    "whisper": "base",
    "openai": "gpt-4o-transcribe",
}
_BACKEND_ALIASES = {
    "faster_whisper": "faster-whisper",
    "fasterwhisper": "faster-whisper",
    "local": "faster-whisper",
    "openai-whisper": "openai",
    "hosted": "openai",
}
_LOCAL_MODELS: dict[tuple[str, str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()


@dataclass(frozen=True)
class STTConfig:
    """Resolved backend settings for one transcription request."""

    backend: str
    model: str
    language: str | None = None
    device: str = "auto"
    compute_type: str = "default"
    hotwords: str | None = None
    #: Run the recogniser's own VAD before decoding. On by default, because a
    #: clip recorded from an open microphone is mostly silence. Callers that
    #: already gated the audio themselves turn it off: a second VAD, applied to
    #: a recording made while the loudspeaker was playing, discards the speech
    #: it was meant to protect and returns an empty transcript.
    vad_filter: bool = True

    @classmethod
    def resolve(
        cls,
        payload: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> STTConfig:
        payload = payload or {}
        environ = os.environ if environ is None else environ
        raw_backend = (
            str(payload.get("stt_backend") or environ.get("REACHY_STT_BACKEND") or "faster-whisper")
            .strip()
            .lower()
        )
        backend = _BACKEND_ALIASES.get(raw_backend, raw_backend)
        if backend not in _DEFAULT_MODELS:
            choices = ", ".join(sorted(_DEFAULT_MODELS))
            raise ValueError(f"unknown Reachy STT backend {raw_backend!r}; choose {choices}")
        model = str(
            payload.get("stt_model") or environ.get("REACHY_STT_MODEL") or _DEFAULT_MODELS[backend]
        ).strip()
        language = (
            str(
                payload.get("language")
                or payload.get("stt_language")
                or environ.get("REACHY_STT_LANGUAGE")
                or ""
            ).strip()
            or None
        )
        device = str(
            payload.get("stt_device") or environ.get("REACHY_STT_DEVICE") or "auto"
        ).strip()
        compute_type = str(
            payload.get("stt_compute_type") or environ.get("REACHY_STT_COMPUTE_TYPE") or "default"
        ).strip()
        hotwords = (
            str(payload.get("stt_hotwords") or environ.get("REACHY_STT_HOTWORDS") or "").strip()
            or None
        )
        vad_filter = bool(payload.get("stt_vad_filter", True))
        return cls(backend, model, language, device, compute_type, hotwords, vad_filter)


@dataclass(frozen=True)
class Transcription:
    """Provider-neutral transcription result."""

    text: str
    backend: str
    model: str
    confidence: float | None = None
    no_speech_probability: float | None = None
    language: str | None = None
    language_probability: float | None = None


@dataclass(frozen=True)
class _BackendResult:
    text: str
    confidence: float | None = None
    no_speech_probability: float | None = None
    language: str | None = None
    language_probability: float | None = None


class STTBackend(Protocol):
    async def transcribe(self, wav_bytes: bytes, config: STTConfig) -> str | _BackendResult: ...


def _temporary_wav(wav_bytes: bytes) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(wav_bytes)
        return Path(handle.name)


class FasterWhisperBackend:
    """Local transcription using the optional ``faster-whisper`` package."""

    async def transcribe(self, wav_bytes: bytes, config: STTConfig) -> _BackendResult:
        def run() -> _BackendResult:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed; run: pip install faster-whisper"
                ) from exc
            key = ("faster-whisper", config.model, config.device, config.compute_type)
            with _MODEL_LOCK:
                model = _LOCAL_MODELS.get(key)
                if model is None:
                    model = WhisperModel(
                        config.model,
                        device=config.device,
                        compute_type=config.compute_type,
                    )
                    _LOCAL_MODELS[key] = model
            path = _temporary_wav(wav_bytes)
            try:
                kwargs = {"language": config.language} if config.language else {}
                kwargs.update(
                    {"vad_filter": config.vad_filter, "condition_on_previous_text": False}
                )
                if config.hotwords:
                    kwargs["hotwords"] = config.hotwords
                segments, info = model.transcribe(str(path), **kwargs)
                segment_list = list(segments)
                text = " ".join(str(segment.text).strip() for segment in segment_list).strip()
                log_probs = [
                    float(segment.avg_logprob)
                    for segment in segment_list
                    if getattr(segment, "avg_logprob", None) is not None
                ]
                no_speech = [
                    float(segment.no_speech_prob)
                    for segment in segment_list
                    if getattr(segment, "no_speech_prob", None) is not None
                ]
                confidence = None
                if log_probs:
                    confidence = max(0.0, min(1.0, math.exp(sum(log_probs) / len(log_probs))))
                no_speech_probability = sum(no_speech) / len(no_speech) if no_speech else None
            finally:
                path.unlink(missing_ok=True)
            language = str(getattr(info, "language", "") or "").strip() or None
            language_probability = (
                None if config.language else getattr(info, "language_probability", None)
            )
            return _BackendResult(
                text=text,
                confidence=confidence,
                no_speech_probability=no_speech_probability,
                language=language,
                language_probability=language_probability,
            )

        return await asyncio.to_thread(run)


class WhisperBackend:
    """Local transcription using the optional OpenAI ``whisper`` package."""

    async def transcribe(self, wav_bytes: bytes, config: STTConfig) -> str:
        def run() -> str:
            try:
                import whisper
            except ImportError as exc:
                raise RuntimeError(
                    "whisper is not installed; run: pip install openai-whisper"
                ) from exc
            key = ("whisper", config.model, config.device, config.compute_type)
            with _MODEL_LOCK:
                model = _LOCAL_MODELS.get(key)
                if model is None:
                    kwargs = {} if config.device == "auto" else {"device": config.device}
                    model = whisper.load_model(config.model, **kwargs)
                    _LOCAL_MODELS[key] = model
            path = _temporary_wav(wav_bytes)
            try:
                kwargs = {"language": config.language} if config.language else {}
                result = model.transcribe(str(path), **kwargs)
                return str((result or {}).get("text") or "").strip()
            finally:
                path.unlink(missing_ok=True)

        return await asyncio.to_thread(run)


class OpenAIBackend:
    """Hosted OpenAI transcription; enabled only when explicitly selected."""

    async def transcribe(self, wav_bytes: bytes, config: STTConfig) -> str:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when REACHY_STT_BACKEND=openai")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai is not installed; run: pip install 'wactorz[openai]'"
            ) from exc
        client = AsyncOpenAI(api_key=api_key)
        file_arg = ("reachy.wav", wav_bytes, "audio/wav")
        kwargs: dict[str, Any] = {"file": file_arg, "model": config.model}
        if config.language:
            kwargs["language"] = config.language
        result = await client.audio.transcriptions.create(**kwargs)
        return str(getattr(result, "text", "") or "").strip()


_BACKENDS: dict[str, STTBackend] = {
    "faster-whisper": FasterWhisperBackend(),
    "whisper": WhisperBackend(),
    "openai": OpenAIBackend(),
}


async def transcribe_wav(
    wav_bytes: bytes,
    payload: Mapping[str, Any] | None = None,
) -> Transcription:
    """Transcribe WAV bytes with the backend selected by payload/environment."""
    if not wav_bytes:
        raise ValueError("WAV payload is empty")
    config = STTConfig.resolve(payload)
    result = await _BACKENDS[config.backend].transcribe(wav_bytes, config)
    if isinstance(result, _BackendResult):
        text = result.text
        confidence = result.confidence
        no_speech_probability = result.no_speech_probability
        language = result.language
        language_probability = result.language_probability
    else:
        text = result
        confidence = None
        no_speech_probability = None
        language = None
        language_probability = None
    return Transcription(
        text=text.strip(),
        backend=config.backend,
        model=config.model,
        confidence=confidence,
        no_speech_probability=no_speech_probability,
        language=language,
        language_probability=language_probability,
    )
