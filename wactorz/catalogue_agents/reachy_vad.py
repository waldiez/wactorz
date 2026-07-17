"""Incremental voice-activity capture for the Reachy Mini microphone.

The Reachy WebRTC backend yields float32 stereo chunks at 16 kHz. This module
converts them to 30 ms mono PCM frames for the tiny WebRTC VAD implementation.
It is deliberately blocking: callers run it in an executor so SDK/GStreamer
pulls never block the actor event loop.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class VADConfig:
    """Timing and sensitivity settings for one conversation listen turn."""

    speech_start_timeout_s: float = 20.0
    silence_s: float = 0.8
    max_utterance_s: float = 12.0
    min_speech_s: float = 0.18
    pre_roll_s: float = 0.3
    flush_s: float = 0.35
    frame_ms: int = 30
    mode: int = 2
    min_rms: float = 0.01


@dataclass(frozen=True)
class VoiceCapture:
    """A mono speech clip and why capture ended."""

    audio: npt.NDArray[np.float32]
    samplerate: int
    channels: int
    duration_s: float
    stop_reason: str | None = None
    flushed_reads: int = 0
    voiced_duration_s: float = 0.0
    voiced_ratio: float = 0.0


def _cancelled(cancel_event: Event | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def drain_audio(
    media: Any,
    duration_s: float,
    cancel_event: Event | None = None,
) -> int:
    """Discard every mic frame arriving during a bounded interval."""
    deadline = time.monotonic() + max(0.0, float(duration_s))
    reads = 0
    while time.monotonic() < deadline and not _cancelled(cancel_event):
        sample = media.get_audio_sample()
        if sample is None:
            time.sleep(0.005)
        else:
            reads += 1
    return reads


def _mono_float32(sample: Any, channels: int) -> npt.NDArray[np.float32]:
    arr = np.asarray(sample, dtype=np.float32)
    if arr.ndim == 2:
        return arr.mean(axis=1, dtype=np.float32)
    arr = arr.reshape(-1)
    if channels > 1 and arr.size >= channels and arr.size % channels == 0:
        return arr.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return arr


def _pcm16_bytes(frame: npt.NDArray[np.float32]) -> bytes:
    return (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def capture_utterance(
    media: Any,
    cancel_event: Event | None = None,
    config: VADConfig | None = None,
    on_speech_start: Callable[[], None] | None = None,
) -> VoiceCapture:
    """Capture one utterance, ending after post-speech silence or a limit.

    Speech onset requires two voiced frames in a three-frame window. A short
    pre-roll avoids clipping the first consonant. Before listening, queued
    WebRTC frames are discarded for ``flush_s``.
    """
    try:
        import webrtcvad
    except ImportError as exc:
        raise RuntimeError("webrtcvad is not installed; run: pip install webrtcvad-wheels") from exc

    cfg = config or VADConfig()
    if cfg.frame_ms not in (10, 20, 30):
        raise ValueError("VAD frame_ms must be 10, 20, or 30")
    samplerate = int(media.get_input_audio_samplerate() or 16000)
    channels = max(1, int(media.get_input_channels() or 1))
    if samplerate not in (8000, 16000, 32000, 48000):
        raise ValueError(f"WebRTC VAD does not support {samplerate} Hz input")

    vad = webrtcvad.Vad(max(0, min(3, int(cfg.mode))))
    frame_samples = samplerate * cfg.frame_ms // 1000
    pre_roll_frames = max(1, math.ceil(cfg.pre_roll_s * 1000 / cfg.frame_ms))
    silence_frames = max(1, math.ceil(cfg.silence_s * 1000 / cfg.frame_ms))
    min_speech_frames = max(1, math.ceil(cfg.min_speech_s * 1000 / cfg.frame_ms))
    max_frames = max(1, math.ceil(cfg.max_utterance_s * 1000 / cfg.frame_ms))

    media.start_recording()
    flushed = 0
    pending = np.zeros((0,), dtype=np.float32)
    pre_roll: deque[tuple[npt.NDArray[np.float32], bool]] = deque(maxlen=pre_roll_frames)
    onset: deque[bool] = deque(maxlen=3)
    captured: list[npt.NDArray[np.float32]] = []
    speech_started = False
    voiced_frames = 0
    trailing_silence = 0
    listen_started = time.monotonic()
    try:
        flushed = drain_audio(media, cfg.flush_s, cancel_event)
        listen_started = time.monotonic()
        while not _cancelled(cancel_event):
            if not speech_started:
                if time.monotonic() - listen_started >= cfg.speech_start_timeout_s:
                    return VoiceCapture(
                        np.zeros((0,), dtype=np.float32),
                        samplerate,
                        1,
                        0.0,
                        "inactivity_timeout",
                        flushed,
                    )
            elif len(captured) >= max_frames:
                break

            sample = media.get_audio_sample()
            if sample is None:
                time.sleep(0.005)
                continue
            mono = _mono_float32(sample, channels)
            if not mono.size:
                continue
            pending = np.concatenate((pending, mono))
            while pending.size >= frame_samples and not _cancelled(cancel_event):
                frame = pending[:frame_samples]
                pending = pending[frame_samples:]
                # WebRTC VAD can label very quiet WebRTC/codec noise as speech.
                # Require a small absolute energy floor as well as its decision.
                frame_rms = float(np.sqrt(np.mean(np.square(frame))))
                voiced = frame_rms >= cfg.min_rms and bool(
                    vad.is_speech(_pcm16_bytes(frame), samplerate)
                )

                if not speech_started:
                    pre_roll.append((frame.copy(), voiced))
                    onset.append(voiced)
                    if len(onset) == onset.maxlen and sum(onset) >= 2:
                        speech_started = True
                        captured.extend(item[0] for item in pre_roll)
                        voiced_frames = sum(1 for _, was_voiced in pre_roll if was_voiced)
                        trailing_silence = 0
                        if on_speech_start is not None:
                            on_speech_start()
                    continue

                captured.append(frame.copy())
                trailing_silence = 0 if voiced else trailing_silence + 1
                if voiced:
                    voiced_frames += 1
                if len(captured) >= max_frames:
                    pending = np.zeros((0,), dtype=np.float32)
                    break
                if len(captured) >= min_speech_frames and trailing_silence >= silence_frames:
                    pending = np.zeros((0,), dtype=np.float32)
                    break

            if speech_started and (
                len(captured) >= max_frames
                or (len(captured) >= min_speech_frames and trailing_silence >= silence_frames)
            ):
                break
    finally:
        try:
            media.stop_recording()
        except Exception:
            pass

    if _cancelled(cancel_event):
        return VoiceCapture(
            np.zeros((0,), dtype=np.float32), samplerate, 1, 0.0, "cancelled", flushed
        )
    audio = (
        np.concatenate(captured).astype(np.float32, copy=False)
        if captured
        else np.zeros((0,), dtype=np.float32)
    )
    voiced_duration_s = round(voiced_frames * cfg.frame_ms / 1000, 3)
    voiced_ratio = round(voiced_frames / len(captured), 3) if captured else 0.0
    # Two short mechanical clicks can satisfy the onset detector. Do not hand
    # those clips to Whisper: on near-silence it may invent a fluent sentence,
    # and repeated false transcriptions make a live session appear deaf.
    if audio.size and voiced_frames < min_speech_frames:
        return VoiceCapture(
            np.zeros((0,), dtype=np.float32),
            samplerate,
            1,
            0.0,
            "noise_rejected",
            flushed,
            voiced_duration_s,
            voiced_ratio,
        )
    return VoiceCapture(
        audio,
        samplerate,
        1,
        round(audio.size / samplerate, 3) if samplerate else 0.0,
        None if audio.size else "empty_audio",
        flushed,
        voiced_duration_s,
        voiced_ratio,
    )
