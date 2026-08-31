"""Synthesis by a service named in configuration, rather than in this process."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from aiohttp import web

pytest.importorskip("wyoming")

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import async_read_event, async_write_event

from wactorz.ext import tts
from wactorz.ext.tts import remote

ServeHttp = Callable[[web.Application], Any]


class FakeSynthesiser:
    """A Wyoming synthesiser, writing its answers with the protocol's own library.

    The events are built and written by ``wyoming`` rather than by hand here: a
    fake that frames them itself can only prove the client agrees with the fake.
    """

    def __init__(self, audio: bytes, refuse: str = "") -> None:
        self.audio = audio
        self.refuse = refuse
        self.asked: dict[str, Any] | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read the request, then answer it."""
        event = await async_read_event(reader)
        if event is not None:
            self.asked = dict(event.data)
        if self.refuse:
            await async_write_event(Error(text=self.refuse, code="refused").event(), writer)
        else:
            await async_write_event(AudioStart(rate=22050, width=2, channels=1).event(), writer)
            await async_write_event(
                AudioChunk(rate=22050, width=2, channels=1, audio=self.audio).event(), writer
            )
            await async_write_event(AudioStop().event(), writer)
        writer.close()


@pytest.fixture(name="serve_http")
async def serve_http_fixture() -> AsyncIterator[Callable[[web.Application], Any]]:
    """Run an HTTP synthesiser and hand back its address.

    Async, so teardown runs on the loop the server was started on.
    """
    runners: list[web.AppRunner] = []

    async def start(app: web.Application) -> str:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}/speak"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture(name="wyoming")
async def wyoming_fixture() -> AsyncIterator[Callable[[bytes], Any]]:
    """Run a fake Wyoming synthesiser and hand back its address."""
    servers: list[asyncio.AbstractServer] = []

    async def start(audio: bytes, refuse: str = "") -> tuple[str, FakeSynthesiser]:
        fake = FakeSynthesiser(audio, refuse=refuse)
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return f"tcp://127.0.0.1:{port}", fake

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


class TestWhichSynthesiserAnAddressNames:
    """The scheme decides, so a deployment says where by saying what."""

    def test_tcp_names_a_wyoming_service(self) -> None:
        assert remote.is_wyoming_uri("tcp://piper:10200")
        assert remote.names_a_service("tcp://piper:10200")

    @pytest.mark.parametrize("uri", ["http://voice/speak", "https://voice/speak"])
    def test_http_names_an_endpoint(self, uri: str) -> None:
        assert remote.is_http_uri(uri)
        assert remote.names_a_service(uri)

    @pytest.mark.parametrize("uri", ["", "ws://voice:6006", "not a uri"])
    def test_anything_else_names_nothing(self, uri: str) -> None:
        # Falling through to the one in this process is what an unset address
        # has always meant, and an address this cannot drive must not silently
        # become one it can.
        assert not remote.names_a_service(uri)


class TestSpeakingThroughAWyomingService:
    """Piper and anything else that speaks the protocol."""

    async def test_the_samples_come_back_in_something_playable(
        self, wyoming: Callable[[bytes], Any]
    ) -> None:
        samples = b"\x01\x02" * 64
        uri, _ = await wyoming(samples)

        spoken = await remote.synthesise(uri, "hello there", "")

        # The protocol carries samples with no container, which no browser can
        # decode, so what comes back here has to be a file rather than a stream.
        assert spoken.audio.startswith(b"RIFF")
        assert spoken.content_type == "audio/wav"
        with wave.open(io.BytesIO(spoken.audio), "rb") as played:
            assert played.readframes(played.getnframes()) == samples
            assert played.getframerate() == 22050

    async def test_the_words_reach_it(self, wyoming: Callable[[bytes], Any]) -> None:
        uri, fake = await wyoming(b"\x00\x00")

        await remote.synthesise(uri, "hello there", "")

        assert fake.asked is not None
        assert fake.asked["text"] == "hello there"

    async def test_a_voice_is_named_the_way_the_protocol_names_one(
        self, wyoming: Callable[[bytes], Any]
    ) -> None:
        uri, fake = await wyoming(b"\x00\x00")

        await remote.synthesise(uri, "hello", "en_GB-alba-medium")

        assert fake.asked is not None
        assert fake.asked["voice"] == {"name": "en_GB-alba-medium"}

    async def test_no_voice_asks_for_none(self, wyoming: Callable[[bytes], Any]) -> None:
        uri, fake = await wyoming(b"\x00\x00")

        await remote.synthesise(uri, "hello", "")

        # Absent rather than empty: a synthesiser reads an empty name as a
        # request for a voice called "", and refuses it.
        assert fake.asked is not None
        assert fake.asked.get("voice") is None

    async def test_a_refusal_is_raised_rather_than_answered_with_silence(
        self, wyoming: Callable[..., Any]
    ) -> None:
        uri, _ = await wyoming(b"", refuse="no such voice")

        # An unknown voice ends the stream. Answering 200 with nothing in it
        # would look like a synthesiser that simply had nothing to say.
        with pytest.raises(RuntimeError, match="no such voice"):
            await remote.synthesise(uri, "hello", "nonesuch")

    async def test_a_connection_that_ends_early_is_not_silence(
        self, wyoming: Callable[..., Any]
    ) -> None:
        uri, _ = await wyoming(b"", refuse="")

        with pytest.raises(RuntimeError, match="no audio"):
            await remote.synthesise(uri, "hello", "")


class TestASynthesiserThatStops:
    """A service that goes quiet must not take the request with it."""

    async def test_a_silent_synthesiser_is_given_up_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def never_answer(
            _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
        ) -> None:
            await asyncio.sleep(2)

        server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(remote, "TIMEOUT", 0.2)
        try:
            # Without a bound the handler waits for as long as the service does,
            # holding the connection that asked it open too.
            with pytest.raises(asyncio.TimeoutError):
                await remote.synthesise(f"tcp://127.0.0.1:{port}", "hello", "")
        finally:
            # Not awaiting the close: it waits on the handler above, which is
            # still holding the connection open on purpose.
            server.close()


class TestSpeakingThroughAnHttpEndpoint:
    """The shape most hosted services already have."""

    async def test_the_audio_comes_back(self, serve_http: ServeHttp) -> None:
        async def speak(_request: web.Request) -> web.Response:
            return web.Response(body=b"mp3-bytes", content_type="audio/mpeg")

        app = web.Application()
        app.router.add_post("/speak", speak)
        uri = await serve_http(app)

        spoken = await remote.synthesise(uri, "hello there", "")

        assert spoken.audio == b"mp3-bytes"

    async def test_the_kind_of_audio_is_carried_through(self, serve_http: ServeHttp) -> None:
        async def speak(_request: web.Request) -> web.Response:
            return web.Response(body=b"wav-bytes", content_type="audio/wav")

        app = web.Application()
        app.router.add_post("/speak", speak)

        spoken = await remote.synthesise(await serve_http(app), "hi", "")

        # The browser decodes by sniffing, but the header is what a cache and a
        # download would believe, so it is the service's answer that is passed on.
        assert spoken.content_type == "audio/wav"

    async def test_the_text_reaches_it_under_both_common_names(self, serve_http: ServeHttp) -> None:
        seen: dict[str, Any] = {}

        async def speak(request: web.Request) -> web.Response:
            seen.update(await request.json())
            return web.Response(body=b"x", content_type="audio/wav")

        app = web.Application()
        app.router.add_post("/speak", speak)

        await remote.synthesise(await serve_http(app), "hello there", "amy")

        # `input` is what an OpenAI-shaped endpoint reads; `text` is what most
        # others do. Sending both spares a deployment a translating proxy.
        assert seen["input"] == "hello there"
        assert seen["text"] == "hello there"
        assert seen["voice"] == "amy"

    async def test_a_refusal_is_raised_rather_than_returned_as_audio(
        self, serve_http: ServeHttp
    ) -> None:
        async def speak(_request: web.Request) -> web.Response:
            return web.Response(status=502, text="upstream is down")

        app = web.Application()
        app.router.add_post("/speak", speak)
        uri = await serve_http(app)

        # Otherwise the browser is handed an error page to play.
        with pytest.raises(RuntimeError, match="502"):
            await remote.synthesise(uri, "hi", "")


class TestWhatTheDeploymentOffers:
    """`available` decides whether the browser uses the server or its own voice."""

    def test_a_named_service_is_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_TTS_URI", "tcp://piper:10200")
        monkeypatch.setattr(tts._tts_state, "available", False)

        # Answering with the optional dependency's absence would hide a working
        # synthesiser from a deployment that has one.
        assert tts.synthesiser_available() is True

    def test_without_one_the_optional_dependency_decides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WACTORZ_TTS_URI", raising=False)
        monkeypatch.setattr(tts._tts_state, "available", False)

        assert tts.synthesiser_available() is False

    async def test_a_named_service_offers_no_voice_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_TTS_URI", "tcp://piper:10200")

        response = await tts.tts_voices_handler(_request=None)  # type: ignore[arg-type]

        # Its voices are its own. An empty list is the browser's cue that there
        # is no choice to make here, rather than a list that failed to load.
        body = response.body
        assert isinstance(body, bytes)
        assert not json.loads(body)


class TestWhichBranchSpeaks:
    """`WACTORZ_TTS` is a deployment's choice, not its packages'."""

    @staticmethod
    def _mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        """Put the process on `mode`, as config resolves it once at import."""
        monkeypatch.setattr(tts.config, "TTS_MODE", mode)

    def test_server_speaks_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mode(monkeypatch, "server")
        monkeypatch.setattr(tts._tts_state, "available", True)

        assert tts.synthesiser_available() is True

    def test_browser_keeps_the_words_on_this_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mode(monkeypatch, "browser")
        monkeypatch.setattr(tts._tts_state, "available", True)
        monkeypatch.setenv("WACTORZ_TTS_URI", "https://voice.example/speak")

        # Everything needed to synthesise is present, and the branch says the
        # text must not be handed to it.
        assert tts.synthesiser_available() is False

    def test_off_says_nothing_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mode(monkeypatch, "off")
        monkeypatch.setattr(tts._tts_state, "available", True)

        assert tts.synthesiser_available() is False

    def test_host_does_not_ask_the_browser_to_play(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mode(monkeypatch, "host")
        monkeypatch.setattr(tts._tts_state, "available", True)

        # The speech comes out of the server's own device on that branch, so a
        # browser told "available" would play it a second time.
        assert tts.synthesiser_available() is False

    async def test_a_silenced_branch_refuses_the_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mode(monkeypatch, "browser")
        monkeypatch.setattr(tts._tts_state, "available", True)

        response = await tts.tts_handler(_FakeRequest({"text": "hello there"}))  # type: ignore[arg-type]

        assert response.status == 503


class _FakeRequest:
    """Just enough of a request to carry a JSON body to the handler."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        """The decoded body."""
        return self._body


class TestASynthesiserThatCannotBeSpokenTo:
    """A named address is only usable with whatever speaks to it installed."""

    def test_wyoming_needs_its_dependency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tts.config, "TTS_MODE", "server")
        monkeypatch.setenv("WACTORZ_TTS_URI", "tcp://piper:10200")
        monkeypatch.setattr(remote, "WYOMING", False)

        # Reporting yes here tells the browser the server speaks; every request
        # then fails, and a failure is not one of the answers it falls back on.
        assert tts.synthesiser_available() is False

    def test_an_http_one_needs_nothing_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tts.config, "TTS_MODE", "server")
        monkeypatch.setenv("WACTORZ_TTS_URI", "https://voice.example/speak")
        monkeypatch.setattr(remote, "WYOMING", False)
        monkeypatch.setattr(tts._tts_state, "available", False)

        assert tts.synthesiser_available() is True


class TestWhatARefusalSays:
    """A 503 is read by whoever configured this, so it names what to change."""

    async def _refusal(self) -> str:
        response = await tts.tts_handler(_FakeRequest({"text": "hello there"}))  # type: ignore[arg-type]
        assert response.status == 503
        return response.text or ""

    async def test_a_missing_wyoming_asks_for_wyoming(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tts.config, "TTS_MODE", "server")
        monkeypatch.setenv("WACTORZ_TTS_URI", "tcp://piper:10200")
        monkeypatch.setattr(remote, "WYOMING", False)

        said = await self._refusal()

        # Naming the other extra here sends whoever reads it to install a package
        # that would change nothing.
        assert "wactorz[voice]" in said
        assert "wactorz[tts]" not in said

    async def test_a_silenced_branch_says_so_rather_than_naming_a_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tts.config, "TTS_MODE", "browser")
        monkeypatch.setattr(tts._tts_state, "available", True)

        said = await self._refusal()

        # Nothing is missing here; the deployment chose this.
        assert "WACTORZ_TTS=browser" in said
        assert "pip install" not in said

    async def test_nothing_configured_asks_for_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tts.config, "TTS_MODE", "server")
        monkeypatch.delenv("WACTORZ_TTS_URI", raising=False)
        monkeypatch.setattr(tts._tts_state, "available", False)

        said = await self._refusal()

        assert "wactorz[tts]" in said

    async def test_the_address_is_never_quoted_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tts.config, "TTS_MODE", "server")
        monkeypatch.setenv("WACTORZ_TTS_URI", "tcp://voice.internal.example:10200")
        monkeypatch.setattr(remote, "WYOMING", False)

        said = await self._refusal()

        # Where this deployment keeps its services is not the browser's business,
        # and a 503 body is the easiest place for that to slip out.
        assert "voice.internal.example" not in said
