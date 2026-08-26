"""Configurable speech-to-text backends for the Reachy Mini voice interface.

All optional dependencies are imported lazily. Selecting a hosted backend is
explicit: the default is local ``faster-whisper`` and audio is never uploaded
merely because an API key happens to exist in the environment.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
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
_LOGGER = logging.getLogger(__name__)

#: Compute types no CPU implements. A configuration written for a GPU box has
#: to keep working on a laptop or a Raspberry Pi, so these are exchanged for an
#: equivalent the CPU can run rather than raising.
_GPU_ONLY_COMPUTE_TYPES = frozenset({"float16", "int8_float16", "bfloat16", "int8_bfloat16"})
_CPU_COMPUTE_TYPE = "int8"
#: Directory prefix Hugging Face gives a cached repository: ``models--<org>--<name>``.
_HF_CACHE_MARKER = "models--"
#: Substrings that mark a failure as "this machine cannot run CUDA" rather than
#: a problem with the audio or the model. A machine can report a CUDA device and
#: still lack the runtime libraries the device needs, and that gap only shows up
#: once work is submitted, so the message is what identifies it.
_CUDA_FAILURE_MARKERS = (
    "cublas",
    "cudnn",
    "cuda",
    "libcu",
    "no gpu",
    "gpu is not supported",
)
#: Set once CUDA has proven unusable here, so later turns go straight to the CPU
#: instead of paying for the same failure on every utterance.
_CUDA_DISABLED = False


def _looks_like_path(value: str) -> bool:
    """Say whether a model identifier names a location rather than a model.

    A Hugging Face repository id is a bare name or one ``namespace/name`` pair.
    Anything rooted, drive-qualified, home-relative, backslash-separated, or
    deeper than one slash is a filesystem path someone typed.
    """
    candidate = PurePath(value)
    return bool(
        candidate.drive
        or candidate.is_absolute()
        or value.startswith((".", "~"))
        or "\\" in value
        or value.count("/") > 1
    )


def _repo_id_from_cache_path(path: Path) -> str | None:
    """Recover the repository a Hugging Face cache directory was filled from."""
    for part in path.parts:
        if part.startswith(_HF_CACHE_MARKER):
            segments = [segment for segment in part.split("--")[1:] if segment]
            if segments:
                return "/".join(segments)
    return None


def _resolve_model_source(model: str) -> str:
    """Return a model identifier that can be loaded on *this* machine.

    A configured path is used as it stands when it exists. When it does not —
    a settings file carried over from another machine, another user account or
    another operating system — a Hugging Face cache path still names the
    repository it was filled from, so that name is used instead and the model is
    found in this machine's own cache or downloaded. A path that names nothing
    recoverable is reported with what to set instead, because guessing a
    different model would transcribe at a quality nobody asked for.
    """
    candidate = model.strip()
    if not candidate:
        raise ValueError("STT model is empty; set REACHY_STT_MODEL to a model name or path")
    if Path(candidate).expanduser().exists():
        return str(Path(candidate).expanduser())
    if not _looks_like_path(candidate):
        return candidate
    repo_id = _repo_id_from_cache_path(Path(candidate).expanduser())
    if repo_id is None:
        raise RuntimeError(
            f"STT model path does not exist on this machine: {candidate!r}. "
            "Set REACHY_STT_MODEL to a model name (base, small, large-v3-turbo), "
            "a Hugging Face repository id, or a path that exists here."
        )
    _LOGGER.warning("STT model path %r does not exist here; loading %r instead", candidate, repo_id)
    return repo_id


def _cuda_is_usable() -> bool:
    """Report whether CUDA is worth attempting for a local model."""
    if _CUDA_DISABLED:
        return False
    try:
        # Optional dependency: installed with faster-whisper, absent on a
        # hosted-backend deployment that never loads a local model.
        import ctranslate2  # pyright: ignore[reportMissingImports]

        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        # Any failure to ask the question at all — the runtime missing, the
        # driver refusing — answers it: there is no CUDA to use here.
        return False


def _resolve_placement(device: str, compute_type: str) -> tuple[str, str]:
    """Choose the device and compute type this machine can actually run.

    ``auto`` follows the hardware. An explicit ``cuda`` is honoured when a CUDA
    device is present and demoted with a warning when it is not, because a robot
    that hears you on the CPU is better than one that cannot hear you at all.
    """
    resolved_device = (device or "auto").strip().lower() or "auto"
    if resolved_device in ("gpu", "nvidia"):
        resolved_device = "cuda"
    if resolved_device != "cpu":
        if _cuda_is_usable():
            resolved_device = "cuda"
        else:
            if resolved_device == "cuda":
                _LOGGER.warning("STT device 'cuda' is unavailable here; transcribing on the CPU")
            resolved_device = "cpu"
    resolved_compute = (compute_type or "default").strip() or "default"
    if resolved_device == "cpu" and resolved_compute.lower() in _GPU_ONLY_COMPUTE_TYPES:
        _LOGGER.info(
            "STT compute type %r needs a GPU; using %r on the CPU",
            resolved_compute,
            _CPU_COMPUTE_TYPE,
        )
        resolved_compute = _CPU_COMPUTE_TYPE
    return resolved_device, resolved_compute


def _looks_like_cuda_failure(exc: BaseException) -> bool:
    """Say whether a failure is CUDA being unusable rather than a bad request."""
    message = f"{exc}".casefold()
    return any(marker in message for marker in _CUDA_FAILURE_MARKERS)


def _disable_cuda(exc: BaseException) -> None:
    """Route every later local transcription to the CPU."""
    global _CUDA_DISABLED
    _CUDA_DISABLED = True
    _LOGGER.warning("CUDA transcription failed (%s); falling back to the CPU", exc)


def _decode(
    whisper_model_cls: Any,
    source: str,
    device: str,
    compute_type: str,
    path: Path,
    transcribe_kwargs: dict[str, Any],
) -> tuple[list[Any], Any]:
    """Load the model where it was placed and decode one clip there.

    Segments are materialised here: ``transcribe`` returns a generator, so a
    device that cannot do the work reports it while the list is being built, not
    when the call returns.
    """
    model = _load_faster_whisper_model(whisper_model_cls, source, device, compute_type)
    segments, info = model.transcribe(str(path), **transcribe_kwargs)
    return list(segments), info


def _load_faster_whisper_model(
    whisper_model_cls: Any, source: str, device: str, compute_type: str
) -> Any:
    """Return a cached ``WhisperModel``, building it on first use."""
    key = ("faster-whisper", source, device, compute_type)
    with _MODEL_LOCK:
        model = _LOCAL_MODELS.get(key)
        if model is None:
            model = whisper_model_cls(source, device=device, compute_type=compute_type)
            _LOCAL_MODELS[key] = model
        return model


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
                from faster_whisper import (  # pyright: ignore[reportMissingImports]  # optional
                    WhisperModel,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed; run: pip install faster-whisper"
                ) from exc
            source = _resolve_model_source(config.model)
            path = _temporary_wav(wav_bytes)
            try:
                transcribe_kwargs: dict[str, Any] = (
                    {"language": config.language} if config.language else {}
                )
                transcribe_kwargs.update(
                    {"vad_filter": config.vad_filter, "condition_on_previous_text": False}
                )
                if config.hotwords:
                    transcribe_kwargs["hotwords"] = config.hotwords
                device, compute_type = _resolve_placement(config.device, config.compute_type)
                try:
                    segment_list, info = _decode(
                        WhisperModel, source, device, compute_type, path, transcribe_kwargs
                    )
                except Exception as exc:
                    # A machine can advertise a CUDA device and still be missing
                    # the libraries it needs, which only surfaces once decoding
                    # starts. One retry on the CPU turns that into a slower
                    # answer instead of a voice interface that never replies.
                    if device == "cpu" or not _looks_like_cuda_failure(exc):
                        raise
                    _disable_cuda(exc)
                    device, compute_type = _resolve_placement(config.device, config.compute_type)
                    segment_list, info = _decode(
                        WhisperModel, source, device, compute_type, path, transcribe_kwargs
                    )
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
                import whisper  # pyright: ignore[reportMissingImports]  # optional backend
            except ImportError as exc:
                raise RuntimeError(
                    "whisper is not installed; run: pip install openai-whisper"
                ) from exc
            key = ("whisper", config.model, config.device, config.compute_type)
            with _MODEL_LOCK:
                model = _LOCAL_MODELS.get(key)
                if model is None:
                    load_kwargs: dict[str, Any] = (
                        {} if config.device == "auto" else {"device": config.device}
                    )
                    model = whisper.load_model(config.model, **load_kwargs)
                    _LOCAL_MODELS[key] = model
            path = _temporary_wav(wav_bytes)
            try:
                transcribe_kwargs: dict[str, Any] = (
                    {"language": config.language} if config.language else {}
                )
                result = model.transcribe(str(path), **transcribe_kwargs)
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
            from openai import AsyncOpenAI  # pyright: ignore[reportMissingImports]  # optional
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
