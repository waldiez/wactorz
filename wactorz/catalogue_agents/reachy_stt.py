"""Configurable speech-to-text backends for the Reachy Mini voice interface.

All optional dependencies are imported lazily. This experimental branch uses
Deepgram by default, so voice clips are uploaded when a Deepgram API key is
configured. Local Whisper backends remain available by explicit selection.
"""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wactorz.catalogue_agents.reachy_vad import VADConfig, VoiceCapture, capture_utterance

_DEFAULT_MODELS = {
    "deepgram": "nova-3",
    "faster-whisper": "base",
    "whisper": "base",
    "openai": "gpt-4o-transcribe",
}
_BACKEND_ALIASES = {
    "nova": "deepgram",
    "nova-3": "deepgram",
    "faster_whisper": "faster-whisper",
    "fasterwhisper": "faster-whisper",
    "local": "faster-whisper",
    "openai-whisper": "openai",
    "hosted": "openai",
}
_LOCAL_MODELS: dict[tuple[str, str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


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
    timeout_s: float = 60.0
    detect_language: bool = True

    @classmethod
    def resolve(
        cls,
        payload: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> STTConfig:
        payload = payload or {}
        environ = os.environ if environ is None else environ
        raw_backend = (
            str(payload.get("stt_backend") or environ.get("REACHY_STT_BACKEND") or "deepgram")
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
        vad_filter = _as_bool(payload.get("stt_vad_filter"), True)
        timeout_s = max(
            1.0,
            float(payload.get("stt_timeout_s") or environ.get("REACHY_STT_TIMEOUT_S") or 60.0),
        )
        detect_language = _as_bool(
            payload.get("stt_detect_language", environ.get("REACHY_STT_DETECT_LANGUAGE")),
            language is None,
        )
        return cls(
            backend,
            model,
            language,
            device,
            compute_type,
            hotwords,
            vad_filter,
            timeout_s,
            detect_language,
        )


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
class StreamingTurn:
    """One locally gated microphone capture and its live Deepgram result."""

    capture: VoiceCapture
    transcription: Transcription | None
    error: str | None = None


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
                transcribe_kwargs: dict[str, Any] = (
                    {"language": config.language} if config.language else {}
                )
                transcribe_kwargs.update(
                    {"vad_filter": config.vad_filter, "condition_on_previous_text": False}
                )
                if config.hotwords:
                    transcribe_kwargs["hotwords"] = config.hotwords
                segments, info = model.transcribe(str(path), **transcribe_kwargs)
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


def _deepgram_keyterms(hotwords: str | None) -> list[str] | None:
    """Turn the shared comma-separated recognition hints into Nova-3 keyterms."""
    if not hotwords:
        return None
    terms = [term.strip() for term in hotwords.split(",") if term.strip()]
    return terms or None


def _deepgram_stream_language(
    config: STTConfig,
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
) -> str:
    """Resolve the fixed language required by Deepgram's streaming endpoint."""
    return str(
        payload.get("stt_stream_language")
        or environ.get("REACHY_STT_STREAM_LANGUAGE")
        or config.language
        or "multi"
    ).strip()


def _deepgram_result(response: Any) -> _BackendResult:
    """Extract the first Deepgram channel without coupling callers to its SDK types."""
    try:
        channel = response.results.channels[0]
        alternative = channel.alternatives[0]
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError("Deepgram returned no transcription channel") from exc
    return _BackendResult(
        text=str(getattr(alternative, "transcript", "") or "").strip(),
        confidence=getattr(alternative, "confidence", None),
        language=str(getattr(channel, "detected_language", "") or "").strip() or None,
        language_probability=getattr(channel, "language_confidence", None),
    )


def _transcribe_deepgram_sync(
    wav_bytes: bytes,
    config: STTConfig,
    api_key: str,
    client_cls: Any,
    options_cls: Any,
    httpx_module: Any,
) -> _BackendResult:
    """Send one bounded WAV clip through the synchronous Deepgram v3 client."""
    options_kwargs: dict[str, Any] = {
        "model": config.model,
        "punctuate": True,
        "smart_format": True,
    }
    if config.language:
        options_kwargs["language"] = config.language
    elif config.detect_language:
        options_kwargs["detect_language"] = True
    keyterms = _deepgram_keyterms(config.hotwords)
    if keyterms:
        options_kwargs["keyterm"] = keyterms
    client = client_cls(api_key)
    response = client.listen.prerecorded.v("1").transcribe_file(
        {"buffer": wav_bytes, "mimetype": "audio/wav"},
        options_cls(**options_kwargs),
        timeout=httpx_module.Timeout(config.timeout_s, connect=min(10.0, config.timeout_s)),
    )
    return _deepgram_result(response)


class DeepgramBackend:
    """Hosted Nova transcription using the optional Deepgram SDK v3."""

    async def transcribe(self, wav_bytes: bytes, config: STTConfig) -> _BackendResult:
        api_key = os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is required when REACHY_STT_BACKEND=deepgram")
        try:
            import httpx  # pyright: ignore[reportMissingImports]  # optional Deepgram dependency
            from deepgram import (  # pyright: ignore[reportMissingImports]  # optional backend
                DeepgramClient,
                PrerecordedOptions,
            )
        except ImportError as exc:
            raise RuntimeError(
                "deepgram-sdk is not installed; run: pip install 'deepgram-sdk>=3,<4'"
            ) from exc
        return await asyncio.to_thread(
            _transcribe_deepgram_sync,
            wav_bytes,
            config,
            api_key,
            DeepgramClient,
            PrerecordedOptions,
            httpx,
        )


def capture_deepgram_turn(
    media: Any,
    cancel_event: threading.Event,
    vad_config: VADConfig,
    payload: Mapping[str, Any] | None = None,
    on_speech_start: Callable[[], None] | None = None,
    on_interim: Callable[[str], None] | None = None,
) -> StreamingTurn:
    """Stream Reachy's WebRTC PCM to Deepgram while local VAD gates the turn.

    Local VAD remains the authority for speech onset, noise rejection, timeouts,
    and cancellation. Deepgram can close a real utterance sooner through
    ``speech_final``; if its socket fails, the completed local capture remains
    available to the caller for prerecorded fallback.
    """
    resolved_payload = payload or {}
    config = STTConfig.resolve(resolved_payload)
    if config.backend != "deepgram":
        raise ValueError("streaming capture requires the Deepgram backend")
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is required for Deepgram streaming")
    try:
        from deepgram import (  # pyright: ignore[reportMissingImports]  # optional backend
            DeepgramClient,
            LiveOptions,
            LiveTranscriptionEvents,
        )
    except ImportError as exc:
        raise RuntimeError(
            "deepgram-sdk is not installed; run: pip install 'deepgram-sdk>=3,<4'"
        ) from exc

    samplerate = int(media.get_input_audio_samplerate() or 16000)
    endpointing_ms = max(
        100,
        min(2000, int(resolved_payload.get("stt_endpointing_ms") or 500)),
    )
    utterance_end_ms = max(
        1000,
        min(5000, int(resolved_payload.get("stt_utterance_end_ms") or 1200)),
    )
    options_kwargs: dict[str, Any] = {
        "model": config.model,
        "language": _deepgram_stream_language(config, resolved_payload, os.environ),
        "smart_format": True,
        "punctuate": True,
        "encoding": "linear16",
        "sample_rate": samplerate,
        "channels": 1,
        "interim_results": True,
        "utterance_end_ms": str(utterance_end_ms),
        "vad_events": True,
        "endpointing": endpointing_ms,
    }
    keyterms = _deepgram_keyterms(config.hotwords)
    if keyterms:
        options_kwargs["keyterm"] = keyterms

    connection = DeepgramClient(api_key).listen.websocket.v("1")
    endpoint = threading.Event()
    final_result = threading.Event()
    lock = threading.Lock()
    chunks: list[str] = []
    confidences: list[float] = []
    language: str | None = None
    language_probability: float | None = None
    stream_error: str | None = None
    last_interim = ""

    def on_transcript(sender: Any, result: Any = None, **_kwargs: Any) -> None:
        nonlocal language, language_probability, last_interim
        result = sender if result is None else result
        try:
            channel = result.channel
            alternative = channel.alternatives[0]
            text = str(getattr(alternative, "transcript", "") or "").strip()
        except (AttributeError, IndexError, TypeError):
            return
        if not text:
            return
        if bool(getattr(result, "is_final", False)):
            with lock:
                chunks.append(text)
                confidence = getattr(alternative, "confidence", None)
                if confidence is not None:
                    confidences.append(float(confidence))
                language = str(getattr(channel, "detected_language", "") or "").strip() or None
                language_probability = getattr(channel, "language_confidence", None)
            final_result.set()
            if bool(getattr(result, "speech_final", False)):
                endpoint.set()
        elif on_interim is not None and text != last_interim:
            last_interim = text
            on_interim(text)

    def on_utterance_end(_sender: Any, _event: Any = None, **_kwargs: Any) -> None:
        with lock:
            has_text = bool(chunks)
        if has_text:
            endpoint.set()

    def on_error(sender: Any, error: Any = None, **_kwargs: Any) -> None:
        nonlocal stream_error
        error = sender if error is None else error
        stream_error = str(error)

    connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
    connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
    connection.on(LiveTranscriptionEvents.Error, on_error)
    if not connection.start(LiveOptions(**options_kwargs)):
        raise RuntimeError("Deepgram streaming connection did not start")

    def send_frame(frame: bytes) -> None:
        nonlocal stream_error
        if stream_error is not None:
            return
        try:
            connection.send(frame)
        except Exception as exc:
            stream_error = str(exc)

    capture = None
    try:
        capture = capture_utterance(
            media,
            cancel_event,
            vad_config,
            on_speech_start,
            send_frame,
            endpoint.is_set,
        )
        # Local VAD owns capture timing, so it can finish before Deepgram emits
        # its final result. Flush pending audio and give the listener thread a
        # short, bounded window to deliver that result before closing the socket.
        if capture.audio.size and not final_result.is_set() and stream_error is None:
            finalize = getattr(connection, "finalize", None)
            if callable(finalize):
                try:
                    if finalize() is False:
                        stream_error = "Deepgram streaming finalize failed"
                except Exception as exc:
                    stream_error = str(exc)
            if stream_error is None:
                final_result.wait(
                    timeout=max(
                        0.5,
                        min(
                            3.0,
                            float(resolved_payload.get("stt_finalize_timeout_s") or 1.5),
                        ),
                    )
                )
    finally:
        try:
            connection.finish()
        except Exception as exc:
            if stream_error is None:
                stream_error = str(exc)

    with lock:
        text = " ".join(chunks).strip()
        confidence = sum(confidences) / len(confidences) if confidences else None
    transcription = None
    if text:
        transcription = Transcription(
            text=text,
            backend="deepgram-streaming",
            model=config.model,
            confidence=confidence,
            language=language or options_kwargs["language"],
            language_probability=language_probability,
        )
    elif capture is not None and capture.audio.size and stream_error is None:
        stream_error = "Deepgram streaming returned no final transcript"
    if capture is None:
        raise RuntimeError(stream_error or "Deepgram streaming capture failed")
    return StreamingTurn(capture, transcription, stream_error)


_BACKENDS: dict[str, STTBackend] = {
    "deepgram": DeepgramBackend(),
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
