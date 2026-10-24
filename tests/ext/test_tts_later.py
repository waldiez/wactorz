"""A reply spoken into the room after the turn has answered, not while it waits."""

import asyncio
from collections.abc import Iterator

import pytest
from aiohttp import web

from tests.waiting import until
from wactorz.ext import tts
from wactorz.ext.tts import speaker
from wactorz.web import chat


class _Held:
    """Stands in for `speak_here`: starts, then waits until it is let go or cancelled."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.go = asyncio.Event()

    async def __call__(self, text: str) -> None:
        self.started.append(text)
        try:
            await self.go.wait()
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        self.finished.append(text)


@pytest.fixture(name="held")
def held_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Held]:
    held = _Held()
    monkeypatch.setattr(tts, "speak_here", held)
    yield held
    # Whatever a test left pending is not the next test's speech.
    tts.stop_speaking()


async def _no_speech_left() -> None:
    await until(lambda: not tts._speeches, "the pending speech to finish")


class TestTheTurnDoesNotWait:
    async def test_a_reply_is_answered_before_it_is_said(
        self, monkeypatch: pytest.MonkeyPatch, held: _Held
    ) -> None:
        monkeypatch.setattr(chat.voice_settings, "speaking", lambda: "host")
        sent: list[str] = []

        async def reply(text: str | None) -> None:
            sent.append(text or "")

        speaking_reply, _chunk, _end = chat._also_spoken_here(reply, None, None)

        # Returns while the room is still to hear it: the request that carried
        # the question is not held open for as long as the sentence takes.
        await speaking_reply("the lights are on")

        assert sent == ["the lights are on"]
        await until(lambda: held.started, "the speech to start")
        assert not held.finished
        held.go.set()
        await _no_speech_left()
        assert held.finished == ["the lights are on"]

    async def test_nothing_is_started_for_nothing(self, held: _Held) -> None:
        tts.speak_later("   ")

        assert not tts._speeches
        assert not speaker.is_speaking()


class TestNotHearingItsOwnReply:
    async def test_it_counts_as_speaking_from_the_call_not_from_the_task(self, held: _Held) -> None:
        tts.speak_later("hello")

        # Before the task has run at all: a turn that started listening in that
        # gap would hear the reply begin and answer it.
        assert speaker.is_speaking()
        held.go.set()
        await _no_speech_left()
        assert not speaker.is_speaking()

    async def test_speech_cancelled_before_it_starts_lets_the_room_go(self, held: _Held) -> None:
        tts.speak_later("hello")
        tts.stop_speaking()

        await _no_speech_left()

        # The task never ran its own code, so only the done callback could have
        # released it; without that the microphone stays shut for good.
        assert not held.started
        assert not speaker.is_speaking()

    def test_an_intent_released_twice_is_released_once(self) -> None:
        other = speaker.intend()
        intent = speaker.intend()

        intent.release()
        intent.release()

        # The second release must not count the other turn as finished too.
        assert speaker.is_speaking()
        other.release()
        assert not speaker.is_speaking()


class TestStopping:
    async def test_speech_waiting_its_turn_is_cancelled_too(
        self, monkeypatch: pytest.MonkeyPatch, held: _Held
    ) -> None:
        silenced: list[bool] = []
        monkeypatch.setattr(speaker, "silence", lambda: silenced.append(True))
        tts.speak_later("first")
        tts.speak_later("second")
        await until(lambda: len(held.started) == 2, "both to start")

        tts.stop_speaking()
        await _no_speech_left()

        # Otherwise the second would begin the moment the first was cut off,
        # after the interface had said everything stopped.
        assert sorted(held.cancelled) == ["first", "second"]
        assert not held.finished
        assert silenced == [True]
        assert not speaker.is_speaking()

    async def test_the_server_stopping_waits_out_what_it_cancels(self, held: _Held) -> None:
        tts.speak_later("goodbye")
        await until(lambda: held.started, "the speech to start")

        await tts._quiet_on_cleanup(web.Application())

        assert held.cancelled == ["goodbye"]
        assert not tts._speeches
