"""Telling speech from the room's other noise, for the branch that hears a room.

The host branch has no button, so it decides for itself when someone started
and when they finished. Two ways, the better one where it is installed:

- **Silero VAD**, through sherpa-onnx, when ``WACTORZ_VAD_MODEL`` names its
  model. A model trained on speech tells a voice from a fan, a television or a
  door, which loudness alone cannot.
- **Loudness over the room's noise floor** otherwise. The floor is learned from
  the room and keeps following it, since the wake loop holds one stream open
  for hours and a room gets louder and quieter in that time.

Either answers one question per block: is someone speaking now. How long a
silence ends a turn stays the caller's to decide.
"""

import array
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Protocol

from ...config import _unquote

logger = logging.getLogger(__name__)

try:  # optional dependency — the module must import without it
    import sherpa_onnx

    SILERO = True
except ImportError:
    # The name stays bound either way: every use sits behind the flag, and an
    # attribute that vanishes with the dependency cannot be patched in tests.
    sherpa_onnx: Any = None
    SILERO = False

#: Where the Silero model is, from ``WACTORZ_VAD_MODEL``: a file, ``silero_vad.onnx``.
#: Absent by default, as the wake model is: it is weights, not code, fetched by a
#: deployment that wants it rather than shipped to every install.
MODEL = _unquote(os.getenv("WACTORZ_VAD_MODEL", "") or "")

#: How long the room is listened to before anything is said, to learn its floor.
FLOOR_SECONDS = 0.4

#: How far above the room's own noise a block must be to count as speech. Low
#: enough for a laptop microphone, which is quieter than most people expect.
OVER_FLOOR = 2.5

#: The quietest a block can be and still count, however silent the room is. Stops
#: a perfectly quiet room from making its own hiss into speech.
FLOOR_MINIMUM = 0.002

#: How much of the recent past the floor is read from, in seconds, and how low in
#: it. A low share rather than the quietest block: speech has gaps, so its pauses
#: hold the floor down while someone talks, and a noise that stays on -- a fan
#: switched on -- lifts it within the window instead of counting as speech for good.
FLOOR_WINDOW_SECONDS = 10.0
FLOOR_PERCENTILE = 0.1


class SpeechDetector(Protocol):
    """Whether someone is speaking in the block just heard."""

    def is_speech(self, block: bytes) -> bool: ...


def loudness(block: bytes) -> float:
    """How loud one block of 16-bit samples is, as a fraction of full scale."""
    samples = array.array("h")
    samples.frombytes(block[: len(block) - len(block) % 2])
    if not samples:
        return 0.0
    total = sum(float(s) * float(s) for s in samples)
    return (total / len(samples)) ** 0.5 / 32768.0


def as_floats(block: bytes) -> list[float]:
    """16-bit samples as the floats in [-1, 1] the models take."""
    samples = array.array("h")
    samples.frombytes(block[: len(block) - len(block) % 2])
    return [s / 32768.0 for s in samples]


def threshold_over(floor: float) -> float:
    """How loud a block must be to count as speech in a room this noisy."""
    return max(floor * OVER_FLOOR, FLOOR_MINIMUM)


def room_floor(stream: Any, block_frames: int, block_seconds: float) -> float:
    """Measure the room's own noise, so the threshold suits where it is."""
    levels = [
        loudness(bytes(stream.read(block_frames)[0]))
        for _ in range(max(1, int(FLOOR_SECONDS / block_seconds)))
    ]
    return sum(levels) / len(levels)


class LoudnessOverFloor:
    """Speech is a block well above the room's noise, the noise read from the recent past."""

    def __init__(self, floor: float, block_seconds: float) -> None:
        self._recent: deque[float] = deque(
            [floor], maxlen=max(1, int(FLOOR_WINDOW_SECONDS / block_seconds))
        )

    @property
    def floor(self) -> float:
        """The room's noise as it stands: a low share of what was heard lately."""
        ordered = sorted(self._recent)
        return ordered[int(FLOOR_PERCENTILE * (len(ordered) - 1))]

    def is_speech(self, block: bytes) -> bool:
        level = loudness(block)
        speaking = level >= threshold_over(self.floor)
        self._recent.append(level)
        return speaking


class Silero:
    """Speech as Silero VAD hears it."""

    def __init__(self, model: str, rate: int) -> None:
        config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(model=model),
            sample_rate=rate,
        )
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)

    def is_speech(self, block: bytes) -> bool:
        self._vad.accept_waveform(as_floats(block))
        # The finished segments are not used, only whether speech is going on
        # now; left in the queue they would grow for as long as the room talks.
        while not self._vad.empty():
            self._vad.pop()
        return bool(self._vad.is_speech_detected())


#: What was last said in the log about the detector, so it is said once rather
#: than on every turn.
_said: str | None = None


def _say(level: int, message: str, *args: object) -> None:
    global _said
    text = message % args
    if _said != text:
        _said = text
        logger.log(level, "[stt] %s", text)


def for_stream(stream: Any, block_frames: int, block_seconds: float, rate: int) -> SpeechDetector:
    """The detector for a stream just opened: Silero where it can run, else loudness.

    Loudness needs the room's floor first, read from the stream here; Silero
    needs nothing, so a turn heard by it starts at once.
    """
    if MODEL:
        if not SILERO:
            _say(
                logging.WARNING,
                "WACTORZ_VAD_MODEL is set, but sherpa-onnx is not installed "
                "— pip install 'wactorz[vad]'; telling speech by loudness instead",
            )
        elif not Path(MODEL).is_file():
            _say(
                logging.WARNING,
                "WACTORZ_VAD_MODEL names %s, which is not a file; telling speech by loudness instead",
                MODEL,
            )
        else:
            try:
                detector = Silero(MODEL, rate)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _say(
                    logging.WARNING,
                    "Could not load the VAD model %s (%s); telling speech by loudness instead",
                    MODEL,
                    exc,
                )
            else:
                _say(logging.INFO, "Telling speech by Silero VAD")
                return detector
        return LoudnessOverFloor(room_floor(stream, block_frames, block_seconds), block_seconds)
    _say(logging.INFO, "Telling speech by loudness over the room's noise")
    return LoudnessOverFloor(room_floor(stream, block_frames, block_seconds), block_seconds)
