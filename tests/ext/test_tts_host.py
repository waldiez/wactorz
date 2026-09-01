"""Speech that comes out of this machine rather than a browser."""

from __future__ import annotations

import asyncio
import io
import pathlib
import time
import wave
from typing import Any

import pytest

pytest.importorskip("miniaudio", reason="decoding anything but WAV needs wactorz[host]")

from wactorz.ext import tts
from wactorz.ext.tts import remote, speaker
from wactorz.web import chat

#: A real MP3, which is what the in-process synthesiser answers with. Kept as a
#: file because there is no way to make one without an encoder, and the point of
#: the test is that this exact shape reaches the device.
#:
#: Made by this project's own default synthesiser -- ``edge_tts.Communicate`` on
#: the phrase "A short line for the tests." -- so that what the tests decode is
#: the same thing a deployment would be handed.
AN_MP3 = (pathlib.Path(__file__).parent / "fixtures" / "speech.mp3").read_bytes()


def a_wav(seconds: float = 0.1, rate: int = 22050) -> bytes:
    """A silent WAV of the shape a synthesiser answers with."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class TestTheSpeakersThemselves:
    """`speaker` is the only part that touches a sound device."""

    async def test_without_the_dependency_it_says_which_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(speaker, "AUDIO", False)

        with pytest.raises(speaker.NoSpeakers, match=r"wactorz\[host\]"):
            await speaker.play(a_wav())

    async def test_audio_it_cannot_decode_is_refused_before_the_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(speaker, "DECODES", False)
        eight_bit = io.BytesIO()
        with wave.open(eight_bit, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(1)
            out.setframerate(8000)
            out.writeframes(b"\x80" * 100)

        with pytest.raises(speaker.NoSpeakers, match=r"wactorz\[host\]"):
            await speaker.play(eight_bit.getvalue())

    async def test_it_plays_what_the_built_in_synthesiser_makes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        played: list[tuple[int, int]] = []
        monkeypatch.setattr(speaker, "AUDIO", True)
        monkeypatch.setattr(
            speaker, "_play_blocking", lambda _s, rate, ch: played.append((rate, ch))
        )

        # An MP3, which is what edge-tts answers with. Requiring a WAV would
        # leave the branch silent for the synthesiser every install already has.
        await speaker.play(AN_MP3)

        assert played, "the audio never reached the device"

    def test_what_it_will_and_will_not_take(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speaker, "DECODES", False)
        assert speaker.can_play("audio/wav") is True
        assert speaker.can_play("audio/mpeg") is False

        monkeypatch.setattr(speaker, "DECODES", True)
        assert speaker.can_play("audio/mpeg") is True

    async def test_a_device_that_stops_accepting_audio_is_given_up_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(speaker, "AUDIO", True)
        monkeypatch.setattr(speaker, "MARGIN", 0.1)
        monkeypatch.setattr(speaker, "_play_blocking", lambda *_a: time.sleep(5))

        # A wedged device would otherwise hold the turn that asked for the
        # speech open for as long as it stays wedged, which is for ever.
        with pytest.raises(speaker.NoSpeakers, match="stopped accepting"):
            await speaker.play(a_wav(seconds=0.05))

    async def test_a_failure_at_the_device_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speaker, "AUDIO", True)

        class Refusing:
            """A sound device that will not open."""

            @staticmethod
            def RawOutputStream(**_kwargs: object) -> object:
                raise RuntimeError("device is busy")

        # The device itself, not the function that wraps its failures: patching
        # that would take the wrapping out of the test along with the device.
        monkeypatch.setattr(speaker, "sounddevice", Refusing)

        with pytest.raises(speaker.NoSpeakers, match="device is busy"):
            await speaker.play(a_wav())

    async def test_one_voice_at_a_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speaker, "AUDIO", True)
        overlapping = {"now": 0, "most": 0}

        def occupy(*_args: object) -> None:
            overlapping["now"] += 1
            overlapping["most"] = max(overlapping["most"], overlapping["now"])
            time.sleep(0.05)
            overlapping["now"] -= 1

        monkeypatch.setattr(speaker, "_play_blocking", occupy)

        await asyncio.gather(speaker.play(a_wav()), speaker.play(a_wav()), speaker.play(a_wav()))

        # Two turns answering at once would otherwise talk over each other in
        # the room, or lose one to a device that allows a single writer.
        assert overlapping["most"] == 1

    async def test_speech_can_be_cut_off_part_way(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speaker, "AUDIO", True)
        written: list[int] = []

        class Piecewise:
            """A device that records how much of the speech it was given."""

            @staticmethod
            def RawOutputStream(**_kwargs: object) -> Any:
                return Piecewise()

            def __enter__(self) -> Piecewise:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def write(self, chunk: bytes) -> None:
                written.append(len(chunk))
                if len(written) == 2:
                    speaker.silence()

        monkeypatch.setattr(speaker, "sounddevice", Piecewise)

        await speaker.play(a_wav(seconds=2.0))

        # Stopping is only possible between pieces: one write of the whole
        # sentence blocks until it has all played, and neither cancelling the
        # task nor aborting the stream releases it.
        assert len(written) == 2

    def test_silencing_an_idle_machine_is_harmless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speaker, "AUDIO", False)

        speaker.silence()  # nothing to stop, and nothing to raise about it


class TestWhatComesOutOfTheSpeakers:
    """`speak_here` is the branch where nobody is looking at a page."""

    async def test_it_plays_what_the_synthesiser_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        played: list[bytes] = []
        audio = a_wav()

        async def made(_text: str, _voice: str = "") -> remote.Speech:
            return remote.Speech(audio=audio, content_type="audio/wav")

        monkeypatch.setattr(tts, "make_speech", made)
        monkeypatch.setattr(speaker, "play", lambda data: _record(played, data))

        await tts.speak_here("hello there")

        assert played == [audio]

    async def test_what_the_built_in_synthesiser_makes_is_played_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        played: list[bytes] = []

        async def made(_text: str, _voice: str = "") -> remote.Speech:
            return remote.Speech(audio=AN_MP3, content_type="audio/mpeg")

        monkeypatch.setattr(tts, "make_speech", made)
        monkeypatch.setattr(speaker, "play", lambda data: _record(played, data))

        # The branch is not limited to the one backend that happens to answer in
        # the simplest container.
        await tts.speak_here("hello there")

        assert played == [AN_MP3]

    async def test_audio_nothing_can_decode_is_reported_not_played(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def made(_text: str, _voice: str = "") -> remote.Speech:
            return remote.Speech(audio=b"ID3nonsense", content_type="audio/mpeg")

        monkeypatch.setattr(tts, "make_speech", made)
        monkeypatch.setattr(speaker, "AUDIO", True)
        monkeypatch.setattr(speaker, "DECODES", False)

        await tts.speak_here("hello there")

        # The reply already reached whoever asked; a machine that cannot say it
        # aloud says so in the log rather than failing the turn.
        assert "wactorz[host]" in caplog.text

    async def test_a_failure_does_not_take_the_turn_with_it(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def refuse(_text: str, _voice: str = "") -> remote.Speech:
            raise RuntimeError("no synthesiser")

        monkeypatch.setattr(tts, "make_speech", refuse)

        # The reply has already reached whoever asked for it.
        await tts.speak_here("hello there")

        assert "no synthesiser" in caplog.text

    async def test_nothing_is_said_about_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[str] = []

        async def made(text: str, _voice: str = "") -> remote.Speech:
            asked.append(text)
            return remote.Speech(audio=a_wav(), content_type="audio/wav")

        monkeypatch.setattr(tts, "make_speech", made)
        monkeypatch.setattr(speaker, "play", lambda _data: _nothing())

        await tts.speak_here("   ")

        assert not asked


async def _record(into: list[bytes], data: bytes) -> None:
    """Stand in for playback, keeping what would have been played."""
    into.append(data)


async def _nothing() -> None:
    """Stand in for playback that should never happen."""


class TestWhichTurnsAreSpokenAloud:
    """Only what a person asked for, on the branch that answers into a room."""

    @staticmethod
    def _host(monkeypatch: pytest.MonkeyPatch, spoken: list[str]) -> None:
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "host")

        async def say(text: str) -> None:
            spoken.append(text)

        monkeypatch.setattr(chat.tts, "speak_here", say)

    async def test_a_streamed_answer_is_spoken_once_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spoken: list[str] = []
        self._host(monkeypatch, spoken)
        streamed: list[str] = []

        _reply, chunk, end = chat._also_spoken_here(
            _collect(streamed), _collect(streamed), _ending()
        )
        assert chunk is not None and end is not None
        await chunk("Hello ")
        await chunk("there.")
        await end()

        # One sentence read as a sentence: a synthesiser given the pieces reads
        # them as pieces, with a pause and a falling tone at every chunk.
        assert spoken == ["Hello there."]

    async def test_an_unstreamed_answer_is_spoken_as_it_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spoken: list[str] = []
        self._host(monkeypatch, spoken)
        sent: list[str] = []

        reply, chunk, end = chat._also_spoken_here(_collect(sent), None, None)
        await reply("Only this.")

        assert sent == ["Only this."]
        assert spoken == ["Only this."]
        # Streaming was never asked for, so none is invented.
        assert chunk is None and end is None

    async def test_a_turn_with_nothing_to_say_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spoken: list[str] = []
        self._host(monkeypatch, spoken)

        reply, _chunk, _end = chat._also_spoken_here(_collect([]), None, None)
        await reply(None)

        assert not spoken

    async def test_every_other_branch_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spoken: list[str] = []
        self._host(monkeypatch, spoken)
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "server")

        sent: list[str] = []
        reply, chunk, end = chat._also_spoken_here(_collect(sent), None, None)
        await reply("Not for the room.")

        # The browser is doing the speaking, and the callbacks come back untouched.
        assert not spoken
        assert chunk is None and end is None


def _collect(into: list[Any]) -> Any:
    """A reply callback that records what it was given."""

    async def collect(text: Any) -> None:
        into.append(text)

    return collect


def _ending() -> Any:
    """A stream-end callback that does nothing."""

    async def end(*_args: object, **_kwargs: object) -> None:
        pass

    return end


class TestATurnActuallyReachesTheSpeakers:
    """The wrapper is no use unless the path a question takes goes through it."""

    async def test_a_slash_command_answer_is_spoken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spoken: list[str] = []
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "host")

        async def say(text: str) -> None:
            spoken.append(text)

        monkeypatch.setattr(chat.tts, "speak_here", say)
        replies: list[Any] = []

        # Driven through `route_chat` rather than the wrapper, so that removing
        # the wrapping from it is a failure here rather than a silent machine.
        await chat.route_chat("/help", _collect(replies))

        assert replies, "the command produced no answer to speak"
        assert spoken == replies

    async def test_an_answer_given_whole_is_spoken_even_when_streaming_was_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spoken: list[str] = []
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "host")

        async def say(text: str) -> None:
            spoken.append(text)

        monkeypatch.setattr(chat.tts, "speak_here", say)
        replies: list[Any] = []

        async def chunk(_text: str) -> None:
            pass

        async def end(*_args: object, **_kwargs: object) -> None:
            pass

        # The browser always offers somewhere to stream to, and a slash command
        # answers whole regardless. Wrapping only the streaming callbacks left
        # every turn of this shape silent.
        await chat.route_chat("/help", _collect(replies), stream_fn=chunk, stream_end_fn=end)

        assert replies, "the command produced no answer to speak"
        assert spoken == replies

    async def test_a_streamed_answer_is_not_said_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spoken: list[str] = []
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "host")

        async def say(text: str) -> None:
            spoken.append(text)

        monkeypatch.setattr(chat.tts, "speak_here", say)

        reply, chunk, end = chat._also_spoken_here(_collect([]), _collect([]), _ending())
        assert chunk is not None and end is not None
        await chunk("Hello there.")
        await end()
        await reply(None)

        assert spoken == ["Hello there."]

    async def test_no_other_branch_reaches_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spoken: list[str] = []
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "server")

        async def say(text: str) -> None:
            spoken.append(text)

        monkeypatch.setattr(chat.tts, "speak_here", say)

        await chat.route_chat("/help", _collect([]))

        assert not spoken


class TestASynthesiserInThisProcessThatStops:
    """The built-in one is reached over the network too, and can go quiet."""

    async def test_it_is_given_up_on_like_any_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def never_answer(_text: str, _voice: str) -> remote.Speech:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

        monkeypatch.setattr(tts, "_made_here", never_answer)
        monkeypatch.setattr(tts.remote, "TIMEOUT", 0.2)
        monkeypatch.setattr(tts.remote, "service_uri", lambda: "")

        # It is on the path of a whole turn now that the machine answers out
        # loud, so a stream that stops arriving would hold that turn open.
        with pytest.raises(asyncio.TimeoutError):
            await tts.make_speech("hello there")


class TestWhatStartupWarnsAbout:
    """Said once at startup, because a silent branch reports nothing per turn."""

    @staticmethod
    def _host(monkeypatch: pytest.MonkeyPatch, uri: str, decoder: bool) -> None:
        monkeypatch.setattr(tts.voice_settings, "speaking", lambda: "host")
        monkeypatch.setattr(speaker, "DECODES", decoder)
        monkeypatch.setattr(speaker, "available", lambda: True)
        if uri:
            monkeypatch.setenv("WACTORZ_TTS_URI", uri)
        else:
            monkeypatch.delenv("WACTORZ_TTS_URI", raising=False)

    def test_no_decoder_and_the_built_in_voice_is_a_silent_pairing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._host(monkeypatch, "", decoder=False)

        tts.warn_if_the_room_will_stay_quiet()

        assert "wactorz[host]" in caplog.text

    def test_an_http_service_is_not_assumed_to_answer_in_wav(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._host(monkeypatch, "https://voice.example/speak", decoder=False)

        tts.warn_if_the_room_will_stay_quiet()

        # It answers in whatever it likes, which without a decoder may be
        # nothing this can play.
        assert "wactorz[host]" in caplog.text

    def test_a_wyoming_service_needs_no_decoder(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._host(monkeypatch, "tcp://piper:10200", decoder=False)

        tts.warn_if_the_room_will_stay_quiet()

        # It answers in raw samples, which are given a WAV header on the way
        # back, so the standard library is enough.
        assert "wactorz[host]" not in caplog.text

    def test_a_decoder_settles_it_whatever_the_synthesiser(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._host(monkeypatch, "", decoder=True)

        tts.warn_if_the_room_will_stay_quiet()

        assert not caplog.text

    def test_no_sound_device_is_worth_saying_on_its_own(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._host(monkeypatch, "tcp://piper:10200", decoder=True)
        monkeypatch.setattr(speaker, "available", lambda: False)

        tts.warn_if_the_room_will_stay_quiet()

        assert "no sound device" in caplog.text

    def test_every_other_branch_is_quiet_about_it(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._host(monkeypatch, "", decoder=False)
        monkeypatch.setattr(tts.voice_settings, "speaking", lambda: "server")

        tts.warn_if_the_room_will_stay_quiet()

        assert not caplog.text
