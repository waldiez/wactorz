"""Telling speech from the room's other noise, on the branch that hears a room."""

import array
import logging
import math
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wactorz.ext.stt import vad

BLOCK = 0.05
RATE = 16000


def tone(level: float, seconds: float = BLOCK) -> bytes:
    """A steady tone at a fraction of full scale, one block long by default."""
    samples = array.array(
        "h",
        (
            int(level * 32767 * math.sin(2 * math.pi * 440 * i / RATE))
            for i in range(int(RATE * seconds))
        ),
    )
    return samples.tobytes()


class _Stream:
    """Hands out the blocks it was given, and counts how many were read."""

    def __init__(self, blocks: list[bytes]) -> None:
        self._blocks = blocks
        self.read_count = 0

    def read(self, _frames: int) -> tuple[bytes, bool]:
        self.read_count += 1
        return self._blocks.pop(0) if self._blocks else tone(0.0), False


class TestLoudnessOverTheRoomsNoise:
    def test_a_voice_well_above_a_quiet_room_is_speech(self) -> None:
        detector = vad.LoudnessOverFloor(floor=0.001, block_seconds=BLOCK)

        assert not detector.is_speech(tone(0.001))
        assert detector.is_speech(tone(0.3))

    def test_a_noise_that_stays_on_stops_counting_within_the_window(self) -> None:
        # A fan switched on: at first it is louder than the room was, then it
        # is the room. Measured once, it would be speech for as long as it ran.
        detector = vad.LoudnessOverFloor(floor=0.001, block_seconds=BLOCK)
        window = int(vad.FLOOR_WINDOW_SECONDS / BLOCK)

        heard = [detector.is_speech(tone(0.05)) for _ in range(window + 10)]

        assert heard[0] is True
        assert heard[-1] is False

    def test_someone_talking_does_not_raise_the_floor_over_their_voice(self) -> None:
        # Speech has pauses, and the floor is read from the quiet share of the
        # recent past, so a long talk is still heard as talk at the end.
        detector = vad.LoudnessOverFloor(floor=0.001, block_seconds=BLOCK)
        pattern = [tone(0.3)] * 6 + [tone(0.001)] * 4

        for _ in range(int(2 * vad.FLOOR_WINDOW_SECONDS / (len(pattern) * BLOCK))):
            for block in pattern:
                detector.is_speech(block)

        assert detector.is_speech(tone(0.3))
        assert detector.floor < 0.01


class _FakeDetector:
    """Stands in for sherpa-onnx's VoiceActivityDetector."""

    def __init__(self, config: Any, buffer_size_in_seconds: float) -> None:
        self.config = config
        self.fed: list[list[float]] = []
        self.queued = 0
        self.speaking = False

    def accept_waveform(self, samples: list[float]) -> None:
        self.fed.append(samples)
        self.queued += 1

    def empty(self) -> bool:
        return self.queued == 0

    def pop(self) -> None:
        self.queued -= 1

    def is_speech_detected(self) -> bool:
        return self.speaking


class _FakeSherpa:
    """The three names of sherpa-onnx the wrapper uses, recording what they are given."""

    built: ClassVar[list[_FakeDetector]] = []

    @staticmethod
    def SileroVadModelConfig(**kwargs: Any) -> dict[str, Any]:
        return {"silero": kwargs}

    @staticmethod
    def VadModelConfig(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    @classmethod
    def VoiceActivityDetector(cls, config: Any, buffer_size_in_seconds: float) -> _FakeDetector:
        detector = _FakeDetector(config, buffer_size_in_seconds)
        cls.built.append(detector)
        return detector


@pytest.fixture(name="sherpa")
def sherpa_fixture(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSherpa]:
    _FakeSherpa.built = []
    monkeypatch.setattr(vad, "sherpa_onnx", _FakeSherpa)
    monkeypatch.setattr(vad, "SILERO", True)
    # What was last logged is per process; each test starts with nothing said.
    monkeypatch.setattr(vad, "_said", None)
    return _FakeSherpa


class TestSilero:
    def test_it_is_built_for_the_model_and_the_rate(self, sherpa: type[_FakeSherpa]) -> None:
        vad.Silero("/models/silero_vad.onnx", RATE)

        config = sherpa.built[0].config
        assert config["silero_vad"] == {"silero": {"model": "/models/silero_vad.onnx"}}
        assert config["sample_rate"] == RATE

    def test_it_says_what_the_model_says_and_keeps_no_segments(
        self, sherpa: type[_FakeSherpa]
    ) -> None:
        detector = vad.Silero("/models/silero_vad.onnx", RATE)
        model = sherpa.built[0]

        assert detector.is_speech(tone(0.3)) is False
        model.speaking = True
        assert detector.is_speech(tone(0.3)) is True
        # Fed as floats in [-1, 1], and the finished segments popped, or they
        # would pile up for as long as the room talks.
        assert all(-1.0 <= x <= 1.0 for x in model.fed[0])
        assert model.queued == 0


class TestWhichOneHears:
    def test_without_a_model_it_is_loudness_over_the_room(
        self, monkeypatch: pytest.MonkeyPatch, sherpa: type[_FakeSherpa]
    ) -> None:
        monkeypatch.setattr(vad, "MODEL", "")
        stream = _Stream([])

        detector = vad.for_stream(stream, 800, BLOCK, RATE)

        assert isinstance(detector, vad.LoudnessOverFloor)
        # The floor is learned from the room before anything is said.
        assert stream.read_count == int(vad.FLOOR_SECONDS / BLOCK)

    def test_with_a_model_it_is_silero_and_starts_at_once(
        self, monkeypatch: pytest.MonkeyPatch, sherpa: type[_FakeSherpa], tmp_path: Path
    ) -> None:
        model = tmp_path / "silero_vad.onnx"
        model.write_bytes(b"weights")
        monkeypatch.setattr(vad, "MODEL", str(model))
        stream = _Stream([])

        detector = vad.for_stream(stream, 800, BLOCK, RATE)

        assert isinstance(detector, vad.Silero)
        assert stream.read_count == 0

    @pytest.mark.parametrize("problem", ["no sherpa-onnx", "no such file", "will not load"])
    def test_a_model_that_cannot_be_used_is_said_once_and_loudness_used(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sherpa: type[_FakeSherpa],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        problem: str,
    ) -> None:
        model = tmp_path / "silero_vad.onnx"
        if problem != "no such file":
            model.write_bytes(b"weights")
        monkeypatch.setattr(vad, "MODEL", str(model))
        if problem == "no sherpa-onnx":
            monkeypatch.setattr(vad, "SILERO", False)
        if problem == "will not load":

            def refuse(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("not an onnx model")

            monkeypatch.setattr(_FakeSherpa, "VoiceActivityDetector", refuse)

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                detector = vad.for_stream(_Stream([]), 800, BLOCK, RATE)

        assert isinstance(detector, vad.LoudnessOverFloor)
        # Once, not on every turn: the setting is the same each time.
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
