"""Live recognition against a streaming recogniser.

A transducer revises an open segment: each reply carries the whole current text
of that segment, not an addition to it, so a hypothesis formed from room noise is
replaced once speech gives the decoder enough to go on. These pin that a caller
is handed replacements rather than deltas, and that a segment is marked final
only when the server has moved past it.
"""

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from wactorz.ext.stt.streaming import (
    DONE,
    MAX_PENDING_FRAMES,
    LiveTranscription,
    Partial,
    StreamingSession,
    is_streaming_uri,
    transcribe_stream,
)


class FakeRecogniser:
    """A sherpa-onnx-shaped server: replies with whatever it is told to."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.audio: list[bytes] = []
        self.finished = False

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for reply in self.script:
            await ws.send_str(reply if isinstance(reply, str) else json.dumps(reply))
        async for message in ws:
            if message.type is web.WSMsgType.BINARY:
                self.audio.append(message.data)
            elif message.type is web.WSMsgType.TEXT and message.data == DONE:
                self.finished = True
                break
        await ws.close()
        return ws


@pytest.fixture(name="serve")
async def serve_fixture() -> Any:
    """Run a fake recogniser and hand back its address.

    Async, so teardown runs on the same loop the server was started on -- a sync
    fixture has no loop left to close it with by the time the test ends.
    """
    runners: list[web.AppRunner] = []

    async def start(script: list[Any]) -> tuple[str, FakeRecogniser]:
        fake = FakeRecogniser(script)
        app = web.Application()
        app.router.add_get("/", fake.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = runner.addresses[0][1]
        return f"ws://127.0.0.1:{port}/", fake

    yield start
    for runner in runners:
        await runner.cleanup()


async def collect(uri: str, frames: list[bytes]) -> list[Partial]:
    """Run one session over `frames` and return every reading it produced."""
    seen: list[Partial] = []

    async def audio() -> Any:
        for frame in frames:
            yield frame

    await transcribe_stream(uri, audio(), seen.append)
    return seen


class TestWhichRecogniserAnAddressNames:
    @pytest.mark.parametrize("uri", ["ws://host:6006", "wss://host:6006/"])
    def test_a_websocket_address_streams(self, uri: str) -> None:
        assert is_streaming_uri(uri) is True

    @pytest.mark.parametrize("uri", ["tcp://host:10300", "host:10300", ""])
    def test_anything_else_does_not(self, uri: str) -> None:
        # The scheme decides, so one setting cannot disagree with itself.
        assert is_streaming_uri(uri) is False


class TestReadingsAreReplacements:
    async def test_each_reading_carries_the_whole_segment(self, serve: Any) -> None:
        uri, _fake = await serve(
            [
                {"text": "hello", "segment": 0},
                {"text": "hello th", "segment": 0},
                {"text": "hello there", "segment": 0},
            ]
        )

        readings = await collect(uri, [b"\x00" * 8])

        assert [r.text for r in readings if not r.final] == ["hello", "hello th", "hello there"]

    async def test_a_reading_may_shrink(self, serve: Any) -> None:
        uri, _fake = await serve(
            [
                {"text": "mmm", "segment": 0},
                {"text": "hi", "segment": 0},
            ]
        )

        readings = await collect(uri, [b"\x00" * 8])

        # What room noise looks like: a guess the decoder withdraws once speech
        # gives it something better. A caller that appended would keep both.
        assert [r.text for r in readings if not r.final] == ["mmm", "hi"]


class TestWhenASegmentIsFinished:
    async def test_the_previous_one_is_marked_final(self, serve: Any) -> None:
        uri, _fake = await serve(
            [
                {"text": "first", "segment": 0},
                {"text": "second", "segment": 1},
            ]
        )

        readings = await collect(uri, [b"\x00" * 8])

        finals = [(r.segment, r.text) for r in readings if r.final]
        assert (0, "first") in finals

    async def test_the_last_segment_is_final_when_the_server_closes(self, serve: Any) -> None:
        uri, _fake = await serve([{"text": "all done", "segment": 0}])

        readings = await collect(uri, [b"\x00" * 8])

        assert readings[-1] == Partial(text="all done", segment=0, final=True)


class TestTheAudioSide:
    async def test_every_frame_is_sent(self, serve: Any) -> None:
        uri, fake = await serve([{"text": "x", "segment": 0}])

        await collect(uri, [b"\x01" * 4, b"\x02" * 4, b"\x03" * 4])

        assert fake.audio == [b"\x01" * 4, b"\x02" * 4, b"\x03" * 4]

    async def test_the_server_is_told_when_the_audio_ends(self, serve: Any) -> None:
        uri, fake = await serve([{"text": "x", "segment": 0}])

        await collect(uri, [b"\x00" * 4])

        # Without this the server holds the tail of the utterance and never
        # decodes it.
        assert fake.finished is True


class TestWhenTheRecogniserIsNotThere:
    async def test_connecting_fails_rather_than_hanging(self) -> None:
        with pytest.raises(Exception):
            async with StreamingSession("ws://127.0.0.1:1/"):
                pass


class TestAServerThatStatesFinality:
    async def test_the_flag_is_honoured_when_given(self, serve: Any) -> None:
        uri, _fake = await serve(
            [
                {"text": "hello there", "segment": 0, "is_final": True},
            ]
        )

        readings = await collect(uri, [b"\x00" * 8])

        # One of the two servers says so outright rather than reporting an
        # endpoint by moving to the next segment number.
        assert any(r.final and r.text == "hello there" for r in readings)

    async def test_a_stated_final_does_not_repeat_as_an_inferred_one(self, serve: Any) -> None:
        uri, _fake = await serve(
            [
                {"text": "first", "segment": 0, "is_final": True},
                {"text": "second", "segment": 1},
            ]
        )

        readings = await collect(uri, [b"\x00" * 8])

        finals = [(r.segment, r.text) for r in readings if r.final]
        assert finals.count((0, "first")) == 1


class TestFeedingFramesAsTheyArrive:
    async def test_readings_come_back_while_audio_is_still_going_in(self, serve: Any) -> None:
        uri, fake = await serve(
            [
                {"text": "one", "segment": 0},
                {"text": "one two", "segment": 0},
            ]
        )

        async with LiveTranscription(uri) as live:
            await live.feed(b"\x00" * 8)
            await live.feed(b"\x00" * 8)
            await live.finish()
            seen = [r.text async for r in live.readings()]

        assert "one" in seen and "one two" in seen
        assert fake.finished is True

    async def test_a_session_can_be_abandoned_midway(self, serve: Any) -> None:
        uri, _fake = await serve([{"text": "half a", "segment": 0}])

        live = LiveTranscription(uri)
        await live.__aenter__()
        await live.feed(b"\x00" * 8)

        # What a closed browser tab looks like: nobody is going to call finish.
        await live.close()

        assert True  # closing without finishing must not hang or raise

    async def test_a_recogniser_that_is_not_there_reaches_the_caller(self) -> None:
        async with LiveTranscription("ws://127.0.0.1:1/") as live:
            await live.finish()

            # Raised where the caller can report it, rather than logged and lost.
            with pytest.raises(Exception):
                async for _ in live.readings():
                    pass


class TestWhenAudioArrivesFasterThanItLeaves:
    async def test_the_backlog_is_bounded(self, serve: Any) -> None:
        uri, _fake = await serve([{"text": "x", "segment": 0}])

        live = LiveTranscription(uri)
        # Deliberately not started, so nothing drains the queue.
        for _ in range(MAX_PENDING_FRAMES * 3):
            await live.feed(b"\x00" * 16)

        # A client streaming faster than the recogniser consumes, or a recogniser
        # that stalls, must not grow this until something runs out of memory.
        assert live._frames.qsize() <= MAX_PENDING_FRAMES
        await live.close()

    async def test_saying_the_audio_ended_never_blocks_either(self, serve: Any) -> None:
        uri, _fake = await serve([{"text": "x", "segment": 0}])
        live = LiveTranscription(uri)
        for _ in range(MAX_PENDING_FRAMES + 5):
            await live.feed(b"\x00" * 16)

        # This is awaited inside the socket's own message loop, so blocking here
        # would stall chat and commands on that connection as well.
        await asyncio.wait_for(live.finish(), timeout=2.0)
        await live.close()

    async def test_audio_offered_after_the_end_is_ignored(self, serve: Any) -> None:
        uri, fake = await serve([{"text": "done", "segment": 0}])

        async with LiveTranscription(uri) as live:
            await live.feed(b"\x01" * 8)
            await live.finish()
            await live.feed(b"\x02" * 8)
            _ = [r async for r in live.readings()]

        # The turn ended at `finish`; anything after it belongs to no utterance.
        assert b"\x02" * 8 not in fake.audio

    async def test_feeding_never_blocks_the_caller(self, serve: Any) -> None:
        uri, _fake = await serve([{"text": "x", "segment": 0}])
        live = LiveTranscription(uri)

        # Whoever is capturing audio cannot be made to wait: the microphone
        # keeps producing whether or not the recogniser is keeping up.
        await asyncio.wait_for(
            asyncio.gather(*(live.feed(b"\x00" * 16) for _ in range(MAX_PENDING_FRAMES * 2))),
            timeout=2.0,
        )
        await live.close()


class TestAReplyThatMakesNoSense:
    async def test_a_non_object_reply_does_not_end_the_turn(self, serve: Any) -> None:
        uri, _fake = await serve(
            [
                "[1, 2, 3]",
                {"text": "still here", "segment": 0},
            ]
        )

        readings = await collect(uri, [b"\x00" * 8])

        # One malformed reply is not a reason to abandon a turn going fine.
        assert any(r.text == "still here" for r in readings)
