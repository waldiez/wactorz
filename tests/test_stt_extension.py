"""The dashboard microphone, transcribed on the server.

The route takes WAV only, refuses before calling out when the configured
recognizer cannot run (the dashboard then falls back to the browser's own
recognition), and reports a failure in summary without the service's detail.

Driven through the real extension route with `transcribe_wav` stood in for.
"""

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.catalogue_agents import reachy_stt
from wactorz.ext import stt


def _wav(seconds: float = 0.1, rate: int = 16000) -> bytes:
    frames = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(frames)) + frames


@pytest.fixture(name="client")
async def client_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(reachy_stt, "configuration_problem", lambda *_a, **_k: None)
    monkeypatch.setenv("REACHY_STT_BACKEND", "deepgram")
    app = web.Application()
    stt.setup(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


def _transcribe(monkeypatch: pytest.MonkeyPatch, result=None, error=None, delay=0.0):
    seen: list[bytes] = []

    async def fake(wav_bytes, payload=None):
        del payload
        seen.append(wav_bytes)
        if delay:
            await asyncio.sleep(delay)
        if error:
            raise error
        return result or reachy_stt.Transcription(
            "hello reachy", "deepgram", "nova-3", language="en"
        )

    monkeypatch.setattr(reachy_stt, "transcribe_wav", fake)
    return seen


async def test_a_wav_upload_comes_back_as_text(client, monkeypatch):
    seen = _transcribe(monkeypatch)
    form = FormData()
    form.add_field("audio", _wav(), filename="speech.wav", content_type="audio/wav")

    response = await client.post("/api/stt", data=form)

    assert response.status == 200
    assert await response.json() == {
        "text": "hello reachy",
        "backend": "deepgram",
        "language": "en",
    }
    assert seen == [_wav()]


async def test_a_raw_wav_body_is_accepted_too(client, monkeypatch):
    _transcribe(monkeypatch)

    response = await client.post("/api/stt", data=_wav(), headers={"Content-Type": "audio/wav"})

    assert response.status == 200


async def test_audio_that_is_not_wav_is_refused_before_calling_out(client, monkeypatch):
    seen = _transcribe(monkeypatch)
    form = FormData()
    form.add_field("audio", b"\x1aE\xdf\xa3webm...", filename="speech.webm")

    response = await client.post("/api/stt", data=form)

    assert response.status == 400
    assert seen == []


async def test_an_unconfigured_recognizer_says_what_is_missing(client, monkeypatch):
    monkeypatch.setattr(
        reachy_stt, "configuration_problem", lambda *_a, **_k: "set DEEPGRAM_API_KEY"
    )
    seen = _transcribe(monkeypatch)

    response = await client.post("/api/stt", data=_wav())

    assert response.status == 503
    assert "DEEPGRAM_API_KEY" in (await response.json())["error"]
    assert seen == []


async def test_a_failing_service_is_reported_without_its_detail(client, monkeypatch):
    _transcribe(monkeypatch, error=ConnectionError("https://api.example/?token=secret refused"))

    response = await client.post("/api/stt", data=_wav())

    assert response.status == 502
    assert "secret" not in await response.text()


async def test_a_recognizer_that_never_answers_times_out(client, monkeypatch):
    monkeypatch.setenv("REACHY_STT_TIMEOUT_S", "1")
    monkeypatch.setattr(stt, "LOCAL_TIMEOUT_S", 0.05)
    monkeypatch.setenv("REACHY_STT_BACKEND", "faster-whisper")
    _transcribe(monkeypatch, delay=5)

    response = await client.post("/api/stt", data=_wav())

    assert response.status == 504


async def test_an_oversized_clip_is_refused(client, monkeypatch):
    monkeypatch.setattr(stt, "MAX_AUDIO_BYTES", 1000)
    seen = _transcribe(monkeypatch)

    response = await client.post("/api/stt", data=_wav(seconds=1))

    assert response.status == 413
    assert seen == []


def test_the_browser_is_told_whether_the_server_can_transcribe(monkeypatch):
    monkeypatch.setenv("REACHY_STT_BACKEND", "faster-whisper")
    monkeypatch.setattr(reachy_stt, "configuration_problem", lambda *_a, **_k: None)
    assert stt.public_config(web.Application()) == {
        "available": True,
        "backend": "faster-whisper",
        "problem": None,
    }

    monkeypatch.setattr(
        reachy_stt, "configuration_problem", lambda *_a, **_k: "pip install faster-whisper"
    )
    config = stt.public_config(web.Application())
    assert config["available"] is False
    assert config["problem"] == "pip install faster-whisper"


def test_an_unknown_backend_name_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("REACHY_STT_BACKEND", "telepathy")

    config = stt.public_config(web.Application())

    assert config["available"] is False
    assert config["backend"] is None
