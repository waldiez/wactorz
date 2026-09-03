"""What the wake-word spotter does with audio, and what it refuses to do without.

The model is not shipped with the tests -- it is 12MB of weights fetched at
deploy time -- so these drive the wrapper against a double. What the real model
answers is a question for the machine sheet, not for a unit test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wactorz.ext.wake import spotter


class _FakeStream:
    """Records what was fed to it, the way an OnlineStream would be."""

    def __init__(self, keywords: str | None) -> None:
        self.keywords = keywords
        self.fed: list[list[float]] = []

    def accept_waveform(self, _rate: int, samples: list[float]) -> None:
        self.fed.append(list(samples))


class _FakeSpotter:
    """A spotter that answers with whatever the test queued."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.resets = 0
        self.stream: _FakeStream | None = None
        self.built: dict[str, Any] = {}

    def create_stream(self, keywords: str | None = None) -> _FakeStream:
        self.stream = _FakeStream(keywords)
        return self.stream

    def is_ready(self, _stream: object) -> bool:
        return False

    def decode_stream(self, _stream: object) -> None:  # pragma: no cover - never ready
        raise AssertionError("decode_stream must follow is_ready")

    def get_result(self, _stream: object) -> str:
        return self.answers.pop(0) if self.answers else ""

    def reset_stream(self, _stream: object) -> None:
        self.resets += 1


@pytest.fixture(name="model_dir")
def model_dir_fixture(tmp_path: Path) -> Path:
    """A directory shaped like a keyword model, with empty weights."""
    for name in (
        spotter.ENCODER,
        spotter.DECODER,
        spotter.JOINER,
        spotter.TOKENS,
        spotter.BPE_MODEL,
    ):
        (tmp_path / name).write_bytes(b"")
    return tmp_path


@pytest.fixture(name="fake")
def fake_fixture(monkeypatch: pytest.MonkeyPatch) -> _FakeSpotter:
    """Stand in for the loaded model, and for the text-to-token conversion."""
    built = _FakeSpotter([])

    class _Module:
        # Named as the library names it, so the double is swapped in by name.
        @staticmethod
        def KeywordSpotter(**kwargs: Any) -> _FakeSpotter:
            built.built = kwargs
            return built

        @staticmethod
        def text2token(texts: list[str], **_kwargs: Any) -> list[list[str]]:
            return [[t.replace(" ", "▁")] for t in texts]

    monkeypatch.setattr(spotter, "SPOTTING", True)
    monkeypatch.setattr(spotter, "sherpa_onnx", _Module)
    return built


class TestWhatItRefusesToStartWithout:
    def test_the_dependency(self, monkeypatch: pytest.MonkeyPatch, model_dir: Path) -> None:
        monkeypatch.setattr(spotter, "SPOTTING", False)

        with pytest.raises(spotter.NoSpotter, match="wactorz\\[wake\\]"):
            spotter.Spotter(model_dir, ["hey waldiez"])

    def test_a_phrase_to_listen_for(self, fake: _FakeSpotter, model_dir: Path) -> None:
        # A spotter with nothing to hear would run the room's audio through a
        # model for ever and never wake, which is cost with no capability.
        with pytest.raises(spotter.NoSpotter, match="no wake phrases"):
            spotter.Spotter(model_dir, [])

    def test_the_model_files(self, fake: _FakeSpotter, tmp_path: Path) -> None:
        # Named individually: "model not found" sends someone looking for the
        # directory, which is there, rather than the file that is not.
        (tmp_path / spotter.TOKENS).write_bytes(b"")

        with pytest.raises(spotter.NoSpotter, match=spotter.ENCODER):
            spotter.Spotter(tmp_path, ["hey waldiez"])

    def test_the_file_the_conversion_reads(self, fake: _FakeSpotter, tmp_path: Path) -> None:
        # Left out of the check, a missing bpe.model surfaced from the converter
        # as some other exception, which the loop treats as a passing fault and
        # retries for ever -- rather than the configuration answer it is.
        for name in (spotter.ENCODER, spotter.DECODER, spotter.JOINER, spotter.TOKENS):
            (tmp_path / name).write_bytes(b"")

        with pytest.raises(spotter.NoSpotter, match=spotter.BPE_MODEL):
            spotter.Spotter(tmp_path, ["hey waldiez"])


class TestListeningForAPhrase:
    def test_the_phrases_are_converted_and_written_where_the_library_reads_them(
        self, fake: _FakeSpotter, model_dir: Path
    ) -> None:
        # Given as text on purpose: making someone run a command-line tool to
        # convert their own wake word before it works is a step got wrong. Into
        # the file the library documents as the source, not the per-stream
        # argument, which it documents as *extra* keywords added to that file.
        ear = spotter.Spotter(model_dir, ["hey waldiez", "hey wactorz"])

        written = Path(fake.built["keywords_file"]).read_text(encoding="utf-8")
        assert written.split() == ["HEY▁WALDIEZ", "HEY▁WACTORZ"]
        assert fake.stream is not None
        assert fake.stream.keywords is None
        ear.close()

    def test_the_keywords_file_goes_when_the_spotter_does(
        self, fake: _FakeSpotter, model_dir: Path
    ) -> None:
        ear = spotter.Spotter(model_dir, ["hey waldiez"])
        written = Path(fake.built["keywords_file"])
        assert written.exists()

        ear.close()

        assert not written.exists()

    def test_silence_wakes_nothing(self, fake: _FakeSpotter, model_dir: Path) -> None:
        heard = spotter.Spotter(model_dir, ["hey waldiez"]).hears([0.0] * 800)

        assert not heard
        assert fake.resets == 0

    def test_the_phrase_is_answered_once(self, fake: _FakeSpotter, model_dir: Path) -> None:
        # Without the reset the phrase stays in the result and every later block
        # reports it again, so one utterance wakes the room over and over.
        fake.answers = ["HEY WALDIEZ", "", ""]
        ear = spotter.Spotter(model_dir, ["hey waldiez"])

        assert ear.hears([0.1] * 800) == "HEY WALDIEZ"
        assert fake.resets == 1
        assert not ear.hears([0.1] * 800)
        assert not ear.hears([0.1] * 800)

    def test_audio_reaches_the_model_at_the_rate_it_expects(
        self, fake: _FakeSpotter, model_dir: Path
    ) -> None:
        ear = spotter.Spotter(model_dir, ["hey waldiez"])
        ear.hears([0.5] * 800)

        assert fake.built["sample_rate"] == spotter.RATE
        assert fake.stream is not None
        assert fake.stream.fed == [[0.5] * 800]

    def test_it_says_what_it_is_listening_for(self, fake: _FakeSpotter, model_dir: Path) -> None:
        ear = spotter.Spotter(model_dir, ["hey waldiez"])

        assert ear.phrases == ["hey waldiez"]
