"""Carrying a recognition session over the dashboard's own socket.

The browser says when to listen and sends audio; the server sends back what the
recogniser hears. It rides the existing socket because that connection already
reconnects on its own and already carries everything else the dashboard shows --
a transcript is no heavier than a chat chunk.

The session belongs to the connection that opened it: two browsers may listen at
once, and a closed tab must end its own and nobody else's.
"""

import asyncio
import json
from typing import Any

import pytest

from wactorz.ext.stt.streaming import Partial
from wactorz.web import ws


class FakeSocket:
    """Collects what the server sends, in order."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def types(self) -> list[str]:
        return [frame.get("type", "") for frame in self.sent]


class FakeLive:
    """Stands in for a recogniser: yields what the test scripts."""

    def __init__(self, readings: list[Partial], fail: Exception | None = None) -> None:
        self._readings = readings
        self._fail = fail
        self.fed: list[bytes] = []
        self.finished = False
        self.closed = False

    async def __aenter__(self) -> "FakeLive":
        return self

    async def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def finish(self) -> None:
        self.finished = True

    async def close(self) -> None:
        self.closed = True

    async def readings(self) -> Any:
        for reading in self._readings:
            yield reading
        if self._fail is not None:
            raise self._fail


def use(monkeypatch: pytest.MonkeyPatch, live: Any, uri: str = "ws://recogniser:6006/") -> None:
    """Point the transport at a scripted recogniser."""
    monkeypatch.setattr(ws, "service_uri", lambda: uri)
    monkeypatch.setattr(ws.streaming, "LiveTranscription", lambda _uri: live)


async def settle() -> None:
    """Let the forwarding task run."""
    for _ in range(6):
        await asyncio.sleep(0)


class TestWhatTheBrowserIsSent:
    async def test_readings_arrive_as_they_are_heard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        live = FakeLive(
            [
                Partial(text="hello", segment=0, final=False),
                Partial(text="hello there", segment=0, final=False),
                Partial(text="hello there", segment=0, final=True),
            ]
        )
        use(monkeypatch, live)
        socket, listening = FakeSocket(), ws.Listening()

        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        await settle()
        await listening.stop()

        assert socket.types() == ["stt_partial", "stt_partial", "stt_final"]
        assert socket.sent[1]["text"] == "hello there"
        assert socket.sent[0]["segment"] == 0

    async def test_a_batch_recogniser_says_so_rather_than_pretending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws, "service_uri", lambda: "tcp://whisper:10300")
        socket, listening = FakeSocket(), ws.Listening()

        await listening.start(socket)  # pyright: ignore[reportArgumentType]

        # Opening a session that can never produce a partial would leave the
        # interface waiting for something the recogniser does not send.
        assert socket.types() == ["stt_error"]
        assert listening.session is None

    async def test_a_recogniser_that_fails_reaches_the_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = FakeLive([Partial(text="par", segment=0, final=False)], fail=OSError("gone"))
        use(monkeypatch, live)
        socket, listening = FakeSocket(), ws.Listening()

        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        await settle()
        await listening.stop()

        assert socket.types() == ["stt_partial", "stt_error"]


class TestTheAudioSide:
    async def test_frames_reach_the_recogniser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        live = FakeLive([])
        use(monkeypatch, live)
        listening = ws.Listening()
        await listening.start(FakeSocket())  # pyright: ignore[reportArgumentType]

        await listening.session.feed(b"\x01\x02")
        await listening.session.feed(b"\x03\x04")
        await listening.stop()

        assert live.fed == [b"\x01\x02", b"\x03\x04"]

    async def test_stopping_tells_the_recogniser_the_audio_ended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = FakeLive([])
        use(monkeypatch, live)
        listening = ws.Listening()
        await listening.start(FakeSocket())  # pyright: ignore[reportArgumentType]

        await listening.finish()

        # Without this the recogniser holds the tail of the utterance.
        assert live.finished is True
        await listening.stop()


class TestTheSessionBelongsToItsConnection:
    async def test_a_second_start_does_not_open_a_second_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = FakeLive([])
        use(monkeypatch, live)
        socket, listening = FakeSocket(), ws.Listening()

        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        first = listening.session
        await listening.start(socket)  # pyright: ignore[reportArgumentType]

        assert listening.session is first
        await listening.stop()

    async def test_the_socket_going_away_ends_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        live = FakeLive([])
        use(monkeypatch, live)
        listening = ws.Listening()
        await listening.start(FakeSocket())  # pyright: ignore[reportArgumentType]

        # What the handler does in its finally: a closed tab leaves nothing
        # holding a connection to the recogniser.
        await listening.stop()

        assert live.closed is True
        assert listening.session is None

    async def test_stopping_a_session_that_never_started_is_harmless(self) -> None:
        await ws.Listening().stop()

    async def test_audio_arriving_before_a_session_is_dropped(self) -> None:
        listening = ws.Listening()

        # A browser that sends a frame before saying `stt_start` -- or after the
        # turn ended -- has nothing to feed. Dropping it is the whole handling,
        # and the handler checks this before it feeds.
        assert listening.session is None


class TestListeningAgain:
    async def test_a_second_turn_works_after_the_first_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[FakeLive] = []

        def build(_uri: str) -> FakeLive:
            live = FakeLive([Partial(text="one", segment=0, final=True)])
            opened.append(live)
            return live

        monkeypatch.setattr(ws, "service_uri", lambda: "ws://recogniser:6006/")
        monkeypatch.setattr(ws.streaming, "LiveTranscription", build)
        socket, listening = FakeSocket(), ws.Listening()

        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        await listening.finish()
        await settle()

        # A spent session must be let go, or the guard against opening a second
        # one refuses every turn after the first and the socket is good for
        # exactly one utterance.
        assert listening.session is None
        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        await settle()

        assert len(opened) == 2
        assert socket.types() == ["stt_final", "stt_final"]
        await listening.stop()

    async def test_a_second_turn_works_after_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[FakeLive] = []

        def build(_uri: str) -> FakeLive:
            live = FakeLive([], fail=OSError("gone"))
            opened.append(live)
            return live

        monkeypatch.setattr(ws, "service_uri", lambda: "ws://recogniser:6006/")
        monkeypatch.setattr(ws.streaming, "LiveTranscription", build)
        socket, listening = FakeSocket(), ws.Listening()

        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        await settle()

        # Reporting the failure is not enough: without letting the session go,
        # recovering from it would need a whole new connection.
        assert listening.session is None
        await listening.start(socket)  # pyright: ignore[reportArgumentType]
        await settle()

        assert len(opened) == 2
        await listening.stop()
