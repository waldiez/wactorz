"""Server-side speech: what the TTS routes accept, and what they send back.

Speech is synthesised by an outside service, so the route takes a POST with a
JSON body — it cannot be fired by an image tag from another page — and refuses
anything else before calling out. Without edge-tts installed it answers 503, the
signal for the dashboard to fall back to the browser's own speech.

Driven through the real extension routes with a stand-in `edge_tts`.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.ext import tts


class _Communicate:
    spoken: "list[tuple[str, str]]" = []  # noqa: RUF012  # reset per test by the fixture
    fail = False

    def __init__(self, text: str, voice: str) -> None:
        _Communicate.spoken.append((text, voice))

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        if _Communicate.fail:
            raise ConnectionError("https://speech.example refused")
        yield {"type": "audio", "data": b"ID3"}
        yield {"type": "WordBoundary"}
        yield {"type": "audio", "data": b""}
        yield {"type": "audio", "data": b"mp3"}


class _EdgeTts:
    Communicate = _Communicate

    @staticmethod
    async def list_voices() -> list[dict[str, str]]:
        return [
            {"ShortName": "en-US-B", "Locale": "en-US", "Gender": "Male"},
            {"ShortName": "el-GR-A", "Locale": "el-GR", "Gender": "Female"},
        ]


@pytest.fixture(name="client")
async def client_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(tts, "edge_tts", _EdgeTts, raising=False)
    monkeypatch.setattr(tts._tts_state, "available", True)
    monkeypatch.setattr(tts._tts_state, "voices", None)
    monkeypatch.setattr(tts._tts_state, "default_voice", "en-US-Default")
    monkeypatch.delenv("TTS_VOICE", raising=False)
    _Communicate.spoken = []
    _Communicate.fail = False
    app = web.Application()
    tts.setup(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


class TestVoices:
    async def test_voices_are_listed_sorted_and_cached(self, client: TestClient) -> None:
        voices = await (await client.get("/api/tts/voices")).json()

        assert [v["name"] for v in voices] == ["el-GR-A", "en-US-B"]
        assert tts._tts_state.voices == voices

    async def test_an_unreachable_service_lists_none(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tts._tts_state.voices = None

        async def _refuse() -> list[dict[str, str]]:
            raise ConnectionError("offline")

        monkeypatch.setattr(_EdgeTts, "list_voices", _refuse)

        assert await (await client.get("/api/tts/voices")).json() == []

    async def test_without_edge_tts_nothing_is_fetched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tts._tts_state, "available", False)
        monkeypatch.setattr(tts._tts_state, "voices", None)

        await tts._warm_tts_voices()

        assert tts._tts_state.voices is None


class TestSpeech:
    async def test_speech_is_audio_with_code_blocks_named_and_text_capped(
        self, client: TestClient
    ) -> None:
        text = "Here:\n```python\nprint(1)\n```\n" + "a" * 400

        resp = await client.post("/api/tts", json={"text": text})

        assert resp.content_type == "audio/mpeg"
        assert await resp.read() == b"ID3mp3"
        ((spoken, voice),) = _Communicate.spoken
        assert spoken.startswith("Here:\ncode block\n") and len(spoken) == 300
        assert voice == "en-US-Default"

    async def test_the_voice_comes_from_the_request_or_the_environment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_VOICE", "  el-GR-A  ")

        await client.post("/api/tts", json={"text": "hi"})
        await client.post("/api/tts", json={"text": "hi", "voice": "en-US-B"})

        assert [v for _, v in _Communicate.spoken] == ["el-GR-A", "en-US-B"]
        assert tts.public_config(web.Application())["voice"] == "el-GR-A"

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("not json", "expected a JSON body"),
            ("[1]", "expected a JSON object"),
            ('{"text": "  "}', "text is required"),
        ],
    )
    async def test_a_bad_request_is_refused_before_calling_out(
        self, client: TestClient, body: str, message: str
    ) -> None:
        resp = await client.post(
            "/api/tts", data=body, headers={"Content-Type": "application/json"}
        )

        assert (resp.status, await resp.text()) == (400, message)
        assert _Communicate.spoken == []

    async def test_a_failing_service_is_a_500_without_its_details(self, client: TestClient) -> None:
        _Communicate.fail = True

        resp = await client.post("/api/tts", json={"text": "hi"})

        assert (resp.status, await resp.text()) == (500, "Speech synthesis failed")

    async def test_without_edge_tts_the_route_says_so(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tts._tts_state, "available", False)

        resp = await client.post("/api/tts", json={"text": "hi"})

        assert resp.status == 503
