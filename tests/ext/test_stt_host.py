"""Hearing the room, for the branch that owns a microphone."""

from __future__ import annotations

import array
import asyncio
import io
import json
import math
import time
import wave
from typing import Any

import pytest
from aiohttp import web

from wactorz.ext import stt
from wactorz.ext.stt import listener, streaming


def tone(seconds: float, level: float, rate: int = listener.RATE) -> bytes:
    """A steady tone at a given fraction of full scale."""
    samples = array.array(
        "h",
        (
            int(level * 32767 * math.sin(2 * math.pi * 440 * i / rate))
            for i in range(int(rate * seconds))
        ),
    )
    return samples.tobytes()


class TestHowLoudSomethingIs:
    """The measure the whole branch's endpointing rests on."""

    def test_silence_measures_as_nothing(self) -> None:
        assert listener.loudness(b"\x00\x00" * 100) == 0.0

    def test_a_loud_block_measures_near_full_scale(self) -> None:
        assert listener.loudness(tone(0.05, 0.9)) > 0.5

    def test_a_quiet_block_is_still_measured(self) -> None:
        # A laptop microphone is quieter than most people expect; a measure that
        # rounded this to nothing would make the branch deaf.
        assert 0 < listener.loudness(tone(0.05, 0.005)) < 0.01

    def test_an_odd_trailing_byte_does_not_break_it(self) -> None:
        # A block is 16-bit samples; a stray byte must not raise where a caller
        # is only asking how loud something was.
        assert listener.loudness(b"\x00\x00\x01") >= 0.0

    def test_nothing_at_all_is_not_a_division(self) -> None:
        assert listener.loudness(b"") == 0.0


class TestWhetherThisMachineCanListen:
    def test_without_the_dependency_it_cannot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(listener, "AUDIO", False)
        assert listener.available() is False

    async def test_listening_without_it_says_which_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(listener, "AUDIO", False)

        with pytest.raises(listener.NoMicrophone, match=r"wactorz\[host\]"):
            await listener.listen()


class _Room:
    """A microphone that plays a scripted room back to the listener."""

    def __init__(self, blocks: list[bytes]) -> None:
        self.blocks = blocks
        self.reads = 0

    def read(self, _frames: int) -> tuple[bytes, bool]:
        """Hand over the next block, then silence for ever."""
        block = self.blocks[self.reads] if self.reads < len(self.blocks) else b"\x00\x00" * 800
        self.reads += 1
        return block, False

    def __enter__(self) -> _Room:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def a_room(monkeypatch: pytest.MonkeyPatch, blocks: list[bytes]) -> _Room:
    """Put a scripted room behind the microphone."""
    room = _Room(blocks)
    monkeypatch.setattr(listener, "AUDIO", True)
    monkeypatch.setattr(
        listener, "sounddevice", type("S", (), {"RawInputStream": staticmethod(lambda **_k: room)})
    )
    return room


def quiet(count: int) -> list[bytes]:
    """`count` blocks of a silent room."""
    return [tone(listener.BLOCK_SECONDS, 0.0) for _ in range(count)]


def speech(count: int) -> list[bytes]:
    """`count` blocks of someone talking."""
    return [tone(listener.BLOCK_SECONDS, 0.4) for _ in range(count)]


class TestDecidingWhenSomeoneHasFinished:
    """There is no button on this branch, so the silence has to end the turn."""

    async def test_it_stops_when_the_talking_stops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        floor = int(listener.FLOOR_SECONDS / listener.BLOCK_SECONDS)
        a_room(monkeypatch, quiet(floor) + speech(10) + quiet(200))

        clip = await listener.listen(max_seconds=10.0, silence_seconds=0.3)

        with wave.open(io.BytesIO(clip), "rb") as heard:
            seconds = heard.getnframes() / heard.getframerate()
        # The talking plus the silence that ended it, not the ten seconds it was
        # allowed to run for.
        assert 0.5 < seconds < 2.0

    async def test_a_room_where_nobody_speaks_answers_with_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a_room(monkeypatch, quiet(400))

        assert await listener.listen(max_seconds=2.0, silence_seconds=0.3) == b""

    async def test_the_opening_silence_does_not_end_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        floor = int(listener.FLOOR_SECONDS / listener.BLOCK_SECONDS)
        # Someone taking a breath before speaking: ending on this silence would
        # cut them off before they started.
        a_room(monkeypatch, quiet(floor) + quiet(20) + speech(10) + quiet(200))

        clip = await listener.listen(max_seconds=10.0, silence_seconds=0.3)

        assert clip, "the turn ended before anyone spoke"

    async def test_it_gives_up_on_someone_who_never_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        floor = int(listener.FLOOR_SECONDS / listener.BLOCK_SECONDS)
        a_room(monkeypatch, quiet(floor) + speech(10_000))

        clip = await listener.listen(max_seconds=1.0, silence_seconds=0.3)

        with wave.open(io.BytesIO(clip), "rb") as heard:
            seconds = heard.getnframes() / heard.getframerate()
        assert seconds <= 1.2

    async def test_a_loud_room_does_not_hear_itself_talking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        floor = int(listener.FLOOR_SECONDS / listener.BLOCK_SECONDS)
        # A fan, a fridge, a desk near a road: steady and well above silence.
        hum = [tone(listener.BLOCK_SECONDS, 0.02) for _ in range(floor + 200)]
        a_room(monkeypatch, hum)

        # Measured against the room rather than a constant, so its own noise is
        # not mistaken for someone speaking.
        assert await listener.listen(max_seconds=2.0, silence_seconds=0.3) == b""


class _Request:
    """Just enough of a request for the listen route."""

    def __init__(self) -> None:
        self.app = web.Application()


async def _listen() -> tuple[int, dict[str, Any]]:
    """Call the route and decode what it answered."""
    response = await stt.listen_handler(_Request())  # type: ignore[arg-type]
    body = response.body
    assert isinstance(body, bytes)
    return response.status, json.loads(body)


class TestAskingTheMachineToListen:
    """`POST /api/stt/listen` is the control this branch has instead of a button."""

    async def test_every_other_branch_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stt.config, "STT_MODE", "server")

        status, body = await _listen()

        assert status == 503
        assert "WACTORZ_STT=server" in body["error"]

    async def test_a_machine_with_no_microphone_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: False)

        status, body = await _listen()

        assert status == 503
        assert "wactorz[host]" in body["error"]

    async def test_silence_is_an_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)

        async def heard_nothing(**_kwargs: object) -> bytes:
            return b""

        monkeypatch.setattr(stt.listener, "listen", heard_nothing)

        status, body = await _listen()

        # Nobody spoke. That is not a failure, and nothing should be routed.
        assert status == 200
        assert body == {"text": "", "heard": False}

    async def test_what_was_said_is_routed_as_though_typed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        routed: list[str] = []
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)

        async def heard(**_kwargs: object) -> bytes:
            return b"a clip"

        async def read(_clip: bytes) -> str:
            return "  turn on the lights  "

        async def route(_request: object, said: str) -> None:
            routed.append(said)

        monkeypatch.setattr(stt.listener, "listen", heard)
        monkeypatch.setattr(stt, "transcribe", read)
        monkeypatch.setattr(stt, "_route_as_typed", route)

        status, body = await _listen()

        assert status == 200
        assert body == {"text": "turn on the lights", "heard": True}
        # One routing path, not two: an @mention reaches the agent it names and
        # anything else reaches main, exactly as from the composer.
        assert routed == ["turn on the lights"]

    async def test_it_reads_through_a_streaming_recogniser_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        routed: list[str] = []
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)
        monkeypatch.setattr(stt, "service_uri", lambda: "ws://recogniser:6006")

        async def heard(**_kwargs: object) -> bytes:
            return tone_wav(0.3)

        async def stream(_uri: str, audio: Any, on_reading: Any) -> None:
            async for _frame in audio:
                pass
            on_reading(streaming.Partial(text="lights on", segment=0, final=True))

        async def route(_request: object, said: str) -> None:
            routed.append(said)

        monkeypatch.setattr(stt.listener, "listen", heard)
        monkeypatch.setattr(stt.streaming, "transcribe_stream", stream)
        monkeypatch.setattr(stt, "_route_as_typed", route)

        status, body = await _listen()

        # Reaching for the Wyoming client here raises on a `ws://` address, which
        # is the recogniser `infra/voice/stt/` builds.
        assert status == 200
        assert body["text"] == "lights on"
        assert routed == ["lights on"]

    async def test_it_will_not_listen_while_the_machine_is_talking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listened = {"did": False}
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)
        monkeypatch.setattr(stt.speaker, "is_speaking", lambda: True)

        async def heard(**_kwargs: object) -> bytes:
            listened["did"] = True
            return b"a clip"

        monkeypatch.setattr(stt.listener, "listen", heard)

        status, body = await _listen()

        # One device answers into the room the other listens to. Recording now
        # takes the reply as the next question, and that does not stop.
        assert status == 200
        assert body == {"text": "", "heard": False, "speaking": True}
        assert not listened["did"]

    async def test_a_microphone_that_wedges_is_answered_as_one_that_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)
        monkeypatch.setattr(stt.speaker, "is_speaking", lambda: False)

        async def wedged(**_kwargs: object) -> bytes:
            # What `wait_for` raises. On 3.10 this is not the builtin, and the
            # handler has to catch the one that actually arrives.
            raise asyncio.TimeoutError

        monkeypatch.setattr(stt.listener, "listen", wedged)

        status, body = await _listen()

        # Otherwise it leaves the handler as a 500, which says nothing useful to
        # whatever asked it to listen.
        assert status == 503
        assert "stopped delivering" in body["error"]

    async def test_a_microphone_that_fails_mid_capture_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)
        monkeypatch.setattr(stt.speaker, "is_speaking", lambda: False)

        async def unplugged(**_kwargs: object) -> bytes:
            raise listener.NoMicrophone("the device went away")

        monkeypatch.setattr(stt.listener, "listen", unplugged)

        status, body = await _listen()

        # A microphone can be taken away between the check and the recording.
        assert status == 503
        assert "went away" in body["error"]

    async def test_a_turn_that_cannot_be_answered_says_so(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from wactorz.web import chat as chat_module
        from wactorz.web import runtime as runtime_module
        from wactorz.web import ws as ws_module

        async def broadcast(_msg: dict[str, Any]) -> None:
            return None

        async def fails(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the agent fell over")

        monkeypatch.setattr(runtime_module, "db", None)
        monkeypatch.setattr(ws_module, "broadcast", broadcast)
        monkeypatch.setattr(chat_module, "route_chat", fails)
        monkeypatch.setattr(chat_module, "track_chat_task", lambda task: task)

        await stt._route_as_typed(None, "hello")  # type: ignore[arg-type]
        await asyncio.sleep(0)

        # Said in these words, not merely present in the log: an unretrieved
        # task exception also reaches the log eventually, from the collector,
        # long after the room stopped waiting for an answer.
        assert "could not be answered" in caplog.text

    async def test_a_recogniser_that_fails_does_not_route_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        routed: list[str] = []
        monkeypatch.setattr(stt.config, "STT_MODE", "host")
        monkeypatch.setattr(stt.listener, "available", lambda: True)

        async def heard(**_kwargs: object) -> bytes:
            return b"a clip"

        async def refuse(_clip: bytes) -> str:
            raise RuntimeError("recogniser gone")

        async def route(_request: object, said: str) -> None:
            routed.append(said)

        monkeypatch.setattr(stt.listener, "listen", heard)
        monkeypatch.setattr(stt, "transcribe", refuse)
        monkeypatch.setattr(stt, "_route_as_typed", route)

        status, _body = await _listen()

        assert status == 502
        assert not routed


class TestReadingTheClipBack:
    """Either recogniser, chosen the way every other address is."""

    async def test_a_wyoming_service_takes_the_clip_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        given: list[bytes] = []

        async def batch(clip: bytes) -> str:
            given.append(clip)
            return "heard it"

        monkeypatch.setattr(stt, "service_uri", lambda: "tcp://whisper:10300")
        monkeypatch.setattr(stt, "transcribe", batch)

        assert await stt.hear(b"a wav") == "heard it"
        assert given == [b"a wav"]

    async def test_a_streaming_service_is_fed_the_same_audio_in_frames(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fed: list[bytes] = []

        async def stream(_uri: str, audio: Any, on_reading: Any) -> None:
            async for frame in audio:
                fed.append(frame)
            on_reading(streaming.Partial(text="hello there", segment=0, final=True))

        monkeypatch.setattr(stt, "service_uri", lambda: "ws://recogniser:6006")
        monkeypatch.setattr(stt.streaming, "transcribe_stream", stream)

        said = await stt.hear(tone_wav(0.35))

        # Without this the branch that owns a microphone works against one kind
        # of recogniser and raises against the other.
        assert said == "hello there"
        assert len(fed) > 1, "the clip was not cut into frames"
        # Floats, which is what a streaming recogniser reads; the recording is
        # stored as integers.
        assert all(len(frame) % 4 == 0 for frame in fed)

    async def test_the_last_reading_of_each_segment_is_what_was_heard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def stream(_uri: str, audio: Any, on_reading: Any) -> None:
            async for _frame in audio:
                pass
            on_reading(streaming.Partial(text="hello th", segment=0, final=False))
            on_reading(streaming.Partial(text="hello there", segment=0, final=True))
            on_reading(streaming.Partial(text="how are you", segment=1, final=True))

        monkeypatch.setattr(stt, "service_uri", lambda: "ws://recogniser:6006")
        monkeypatch.setattr(stt.streaming, "transcribe_stream", stream)

        # A reading replaces its segment rather than adding to it, and the
        # segments join in the order they were spoken.
        assert await stt.hear(tone_wav(0.2)) == "hello there how are you"


def tone_wav(seconds: float) -> bytes:
    """A recording of the shape the listener produces."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(listener.RATE)
        out.writeframes(tone(seconds, 0.3))
    return buffer.getvalue()


class TestOneMicrophoneAtATime:
    async def test_two_turns_do_not_listen_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlapping = {"now": 0, "most": 0}
        monkeypatch.setattr(listener, "AUDIO", True)

        def occupy(*_args: object) -> bytes:
            overlapping["now"] += 1
            overlapping["most"] = max(overlapping["most"], overlapping["now"])
            time.sleep(0.05)
            overlapping["now"] -= 1
            return b""

        monkeypatch.setattr(listener, "_listen_blocking", occupy)

        await asyncio.gather(listener.listen(), listener.listen(), listener.listen())

        # The same words taken twice are two turns, answered twice and billed
        # twice; on a device that allows one reader the second simply fails.
        assert overlapping["most"] == 1

    async def test_a_microphone_that_stops_delivering_is_given_up_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(listener, "AUDIO", True)
        monkeypatch.setattr(listener, "MARGIN", 0.1)
        monkeypatch.setattr(listener, "_listen_blocking", lambda *_a: time.sleep(5))

        with pytest.raises(asyncio.TimeoutError):
            await listener.listen(max_seconds=0.1, silence_seconds=0.1)


class TestWhatTheRoomLeavesBehind:
    """A microphone in a room that keeps no record is the wrong thing to build."""

    async def test_the_heard_turn_and_its_answer_are_recorded_and_shown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.web import chat as chat_module
        from wactorz.web import runtime as runtime_module
        from wactorz.web import ws as ws_module

        written: list[dict[str, Any]] = []
        shown: list[dict[str, Any]] = []

        class _Db:
            @staticmethod
            def write_chat_log(**kwargs: Any) -> None:
                written.append(kwargs)

        async def broadcast(msg: dict[str, Any]) -> None:
            shown.append(msg)

        async def route(said: str, reply: Any, **_kw: object) -> None:
            await reply(f"answering {said}")

        monkeypatch.setattr(runtime_module, "db", _Db())
        monkeypatch.setattr(ws_module, "broadcast", broadcast)
        monkeypatch.setattr(chat_module, "route_chat", route)
        monkeypatch.setattr(chat_module, "track_chat_task", lambda task: task)

        await stt._route_as_typed(None, "turn on the lights")  # type: ignore[arg-type]
        await asyncio.sleep(0)

        roles = [w["role"] for w in written]
        assert roles == ["user", "assistant"]
        assert [m["from"] for m in shown] == ["user", "main"]

    async def test_a_streamed_answer_is_recorded_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.web import chat as chat_module
        from wactorz.web import runtime as runtime_module
        from wactorz.web import ws as ws_module

        written: list[dict[str, Any]] = []
        shown: list[dict[str, Any]] = []

        class _Db:
            @staticmethod
            def write_chat_log(**kwargs: Any) -> None:
                written.append(kwargs)

        async def broadcast(msg: dict[str, Any]) -> None:
            shown.append(msg)

        async def route(
            _said: str, _reply: Any, stream_fn: Any = None, stream_end_fn: Any = None
        ) -> None:
            # What an agent that streams actually does.
            for chunk in ["Hello ", "there, ", "the lights ", "are on."]:
                await stream_fn(chunk)
            await stream_end_fn()

        monkeypatch.setattr(runtime_module, "db", _Db())
        monkeypatch.setattr(ws_module, "broadcast", broadcast)
        monkeypatch.setattr(chat_module, "route_chat", route)
        monkeypatch.setattr(chat_module, "track_chat_task", lambda task: task)

        await stt._route_as_typed(None, "are the lights on")  # type: ignore[arg-type]
        await asyncio.sleep(0)

        # One answer, not one per piece: otherwise a forty-chunk reply is forty
        # rows in the log, forty bubbles on the page, and forty utterances aloud.
        answers = [w for w in written if w["role"] == "assistant"]
        assert len(answers) == 1
        assert answers[0]["content"] == "Hello there, the lights are on."
        assert len([m for m in shown if m["from"] != "user"]) == 1

    async def test_what_is_said_aloud_is_redacted_like_anything_typed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.web import runtime as runtime_module

        written: list[dict[str, Any]] = []

        class _Db:
            @staticmethod
            def write_chat_log(**kwargs: Any) -> None:
                written.append(kwargs)

        monkeypatch.setattr(runtime_module, "db", _Db())

        # A credential can be spoken as easily as typed, and this is a microphone
        # listening to a room. What redaction covers is its own business; this is
        # about the text reaching it at all.
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        stt._remember(runtime_module, "user", f"the token={secret}", "main")

        assert secret not in written[0]["content"]
        assert "[redacted]" in written[0]["content"]

    async def test_a_missing_log_does_not_lose_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.web import chat as chat_module
        from wactorz.web import runtime as runtime_module
        from wactorz.web import ws as ws_module

        routed: list[str] = []

        async def broadcast(_msg: dict[str, Any]) -> None:
            return None

        async def route(said: str, _reply: Any, **_kw: object) -> None:
            routed.append(said)

        monkeypatch.setattr(runtime_module, "db", None)
        monkeypatch.setattr(ws_module, "broadcast", broadcast)
        monkeypatch.setattr(chat_module, "route_chat", route)
        monkeypatch.setattr(chat_module, "track_chat_task", lambda task: task)

        await stt._route_as_typed(None, "hello")  # type: ignore[arg-type]
        await asyncio.sleep(0)

        # An install with no database still answers; it just remembers nothing.
        assert routed == ["hello"]


class TestOneUtterancePerTurn:
    """What the room hears back, when this machine is also the one speaking."""

    async def test_a_streamed_answer_is_spoken_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from wactorz.web import chat as chat_module

        spoken: list[str] = []
        monkeypatch.setattr(chat_module.config, "TTS_MODE", "host")

        async def say(text: str) -> None:
            spoken.append(text)

        monkeypatch.setattr(chat_module.tts, "speak_here", say)

        reply, chunk, end = chat_module._also_spoken_here(
            _nothing_said(), _nothing_said(), _nothing_ended()
        )
        assert chunk is not None and end is not None
        for piece in ["The lights ", "in the kitchen ", "are on."]:
            await chunk(piece)
        await end()
        await reply(None)

        # One sentence read as a sentence. Handed the pieces, a synthesiser reads
        # them as pieces, with a pause and a falling tone at each.
        assert spoken == ["The lights in the kitchen are on."]


def _nothing_said() -> Any:
    """A reply callback that keeps nothing."""

    async def said(_text: Any) -> None:
        return None

    return said


def _nothing_ended() -> Any:
    """A stream-end callback that does nothing."""

    async def ended(*_args: object, **_kwargs: object) -> None:
        return None

    return ended
