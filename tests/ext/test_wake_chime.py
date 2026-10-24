"""The sound a room hears when the machine has heard its phrase."""

import array
import threading
from typing import Any

import pytest

from tests.waiting import until
from wactorz.ext.tts import speaker
from wactorz.ext.wake import chime


def _samples() -> "array.array[int]":
    samples = array.array("h")
    samples.frombytes(chime.SOUND)
    return samples


class TestTheSound:
    def test_it_is_short_and_quiet(self) -> None:
        samples = _samples()

        # Short enough not to be talked over, long enough to be heard as a sound.
        assert 0.15 < len(samples) / chime.RATE < 0.4
        assert max(abs(s) for s in samples) <= int(chime.LEVEL * 32767)

    def test_it_starts_and_ends_without_a_click(self) -> None:
        samples = _samples()

        assert abs(samples[0]) < 50
        assert abs(samples[-1]) < 50


class _Output:
    """A sound device that records what it was given, and on which thread."""

    def __init__(self, fail: bool = False) -> None:
        self.written: list[bytes] = []
        self.threads: set[str] = set()
        self._fail = fail

    def RawOutputStream(self, **_kwargs: Any) -> "_Output":
        if self._fail:
            raise OSError("no output device")
        return self

    def __enter__(self) -> "_Output":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def write(self, data: bytes) -> None:
        self.threads.add(threading.current_thread().name)
        self.written.append(bytes(data))

    @property
    def played(self) -> bytes:
        return b"".join(self.written)


@pytest.fixture(name="output")
def output_fixture(monkeypatch: pytest.MonkeyPatch) -> _Output:
    output = _Output()
    monkeypatch.setattr(speaker, "AUDIO", True)
    monkeypatch.setattr(speaker, "sounddevice", output)
    return output


class TestPlayingItAside:
    async def test_it_plays_whole_on_a_thread_of_its_own(self, output: _Output) -> None:
        chime.chime()

        await until(lambda: output.played == chime.SOUND, "the sound to have been played")
        # The recording goes on meanwhile, so playing on the caller's thread
        # would hold it up for as long as the sound lasts.
        assert threading.current_thread().name not in output.threads

    async def test_a_stopped_reply_does_not_silence_it(self, output: _Output) -> None:
        # A stopped turn leaves the speech stop set until the next reply.
        speaker.silence()
        try:
            chime.chime()

            await until(lambda: output.played == chime.SOUND, "the sound to have been played")
        finally:
            speaker._stopped.clear()

    async def test_a_device_that_refuses_costs_the_turn_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        finished = threading.Event()
        real = speaker._play_quietly

        def watched(samples: bytes, rate: int) -> None:
            real(samples, rate)
            finished.set()

        monkeypatch.setattr(speaker, "AUDIO", True)
        monkeypatch.setattr(speaker, "sounddevice", _Output(fail=True))
        monkeypatch.setattr(speaker, "_play_quietly", watched)

        chime.chime()

        # The failure is swallowed on the sound's own thread, not raised anywhere.
        await until(finished.is_set, "the failed sound to have been given up on")

    def test_without_a_sound_device_nothing_is_tried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[object] = []
        monkeypatch.setattr(speaker, "AUDIO", False)
        monkeypatch.setattr(speaker.threading, "Thread", lambda *a, **k: started.append(a))

        chime.chime()

        assert started == []
