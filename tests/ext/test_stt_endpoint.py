"""Server-side speech recognition through a Wyoming service.

The recogniser is a separate process that may be absent, starting, or broken, so
most of what this covers is the difference between "this deployment does not do
recognition", "the clip was wrong" and "the service did not answer" -- three
outcomes a caller has to tell apart to say anything useful to the person holding
the microphone.
"""

import asyncio
import io
import wave
from typing import Any

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

# Skipped rather than stubbed with a placeholder module: the cases below import
# names out of wyoming's submodules, which an empty stand-in cannot supply. The
# extra is in `all`, so this skips only for a developer who installed without it.
pytest.importorskip("wyoming")

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event

from wactorz import config
from wactorz.ext import stt
from wactorz.web.app import build_app


def wav_bytes(frames: bytes = b"\x00\x01" * 800, rate: int = 16000) -> bytes:
    """A minimal mono 16-bit WAV clip."""
    buffer = io.BytesIO()
    # pylint: disable=no-member
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


# pylint: disable=missing-function-docstring
class FakeClient:
    """Stands in for the Wyoming service, recording what it was sent."""

    def __init__(self, text: str = "hello there", fail: bool = False, silent: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.silent = silent
        self.written: list[Event] = []
        self.disconnected = False

    async def connect(self) -> None:
        if self.fail:
            raise ConnectionRefusedError("nothing is listening")

    async def disconnect(self) -> None:
        self.disconnected = True

    async def write_event(self, event: Event) -> None:
        self.written.append(event)

    async def read_event(self) -> Event | None:
        return None if self.silent else Transcript(text=self.text).event()


@pytest.fixture(name="service")
def service_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Point the extension at a fake recogniser."""
    client = FakeClient()
    monkeypatch.setattr(
        stt, "AsyncClient", type("C", (), {"from_uri": staticmethod(lambda _u: client)})
    )
    return client


async def post_audio(payload: bytes | None = None, field: str = "audio") -> tuple[int, Any]:
    """POST a clip to the running app and return (status, parsed body)."""
    data = FormData()
    data.add_field(field, payload if payload is not None else wav_bytes(), filename="speech.wav")
    async with TestClient(TestServer(build_app())) as client:
        resp = await client.post("/api/stt", data=data)
        return resp.status, await resp.json()


# pylint: disable=missing-class-docstring
class TestWhereTheRecogniserLives:
    def test_it_defaults_to_the_local_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_STT_URI", raising=False)

        assert stt.service_uri() == stt.DEFAULT_URI

    def test_it_can_be_somewhere_else_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STT_URI", "tcp://asr.lan:10300")

        assert stt.service_uri() == "tcp://asr.lan:10300"

    def test_an_empty_setting_means_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # What following a .env template produces: load_dotenv supplies "" for a
        # name that is present but blank, and a default never applies to that.
        monkeypatch.setenv("WACTORZ_STT_URI", "  ")

        assert stt.service_uri() == stt.DEFAULT_URI


class TestReadingTheClip:
    def test_a_wav_yields_its_frames_and_shape(self) -> None:
        frames, rate, width, channels = stt._pcm_from_wav(wav_bytes(b"\x01\x02" * 40, rate=8000))

        assert (rate, width, channels) == (8000, 2, 1)
        assert frames == b"\x01\x02" * 40

    def test_something_that_is_not_a_wav_is_refused(self) -> None:
        with pytest.raises(wave.Error):
            stt._pcm_from_wav(b"\x1aE\xdf\xa3 this is webm")


class TestTalkingToTheService:
    async def test_the_clip_is_sent_as_one_bracketed_stream(self, service: FakeClient) -> None:
        await stt.transcribe(wav_bytes())

        kinds = [e.type for e in service.written]
        assert kinds[0] == "transcribe"
        assert kinds[1] == AudioStart(rate=1, width=1, channels=1).event().type
        assert kinds[-1] == AudioStop().event().type
        # Everything between the brackets is audio, and there is some.
        middle = kinds[2:-1]
        assert middle and set(middle) == {
            AudioChunk(rate=1, width=1, channels=1, audio=b"").event().type
        }

    async def test_the_clips_own_rate_is_carried(self, service: FakeClient) -> None:
        await stt.transcribe(wav_bytes(rate=8000))

        start = next(e for e in service.written if e.type == "audio-start")
        assert (start.data or {})["rate"] == 8000

    async def test_a_service_that_never_transcribes_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        silent = FakeClient(silent=True)
        monkeypatch.setattr(
            stt, "AsyncClient", type("C", (), {"from_uri": staticmethod(lambda _u: silent)})
        )

        # Returning "" would report silence, which is a thing the recogniser can
        # legitimately hear -- and this is not that.
        with pytest.raises(RuntimeError):
            await stt.transcribe(wav_bytes())


class TestTheEndpoint:
    async def test_a_clip_comes_back_as_text(self, service: FakeClient) -> None:
        status, body = await post_audio()

        assert status == 200
        assert body["text"] == "hello there"

    async def test_a_request_with_no_audio_says_so(self, service: FakeClient) -> None:
        status, body = await post_audio(field="clip")

        assert status == 400
        assert "audio" in body["error"]

    async def test_an_empty_clip_says_so(self, service: FakeClient) -> None:
        status, _ = await post_audio(payload=b"")

        assert status == 400

    async def test_a_clip_that_is_not_a_wav_is_named_as_such(self, service: FakeClient) -> None:
        status, body = await post_audio(payload=b"\x1aE\xdf\xa3 webm, actually")

        # Distinguishable from a service failure: this one is fixable by the caller.
        assert status == 415
        assert "WAV" in body["error"]

    async def test_a_clip_past_the_limit_is_refused(
        self, service: FakeClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stt, "MAX_AUDIO_BYTES", 128)

        status, _ = await post_audio(payload=wav_bytes(b"\x00\x01" * 4000))

        assert status == 413

    async def test_a_service_that_is_not_there_is_not_the_callers_fault(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refused = FakeClient(fail=True)
        monkeypatch.setattr(
            stt, "AsyncClient", type("C", (), {"from_uri": staticmethod(lambda _u: refused)})
        )

        status, body = await post_audio()

        assert status == 502
        assert "recogniser" in body["error"]

    async def test_a_body_that_is_not_multipart_is_a_caller_error(
        self, service: FakeClient
    ) -> None:
        async with TestClient(TestServer(build_app())) as client:
            resp = await client.post(
                "/api/stt", data=b"{}", headers={"Content-Type": "application/json"}
            )

        # The multipart reader asserts rather than raising on a body of the wrong
        # kind, which would otherwise reach the caller as a server fault.
        assert resp.status == 415

    async def test_the_connection_is_released_even_when_recognition_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        silent = FakeClient(silent=True)
        monkeypatch.setattr(
            stt, "AsyncClient", type("C", (), {"from_uri": staticmethod(lambda _u: silent)})
        )

        status, _ = await post_audio()

        assert status == 502
        assert silent.disconnected is True

    async def test_a_service_that_never_answers_does_not_hang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Hanging(FakeClient):
            async def read_event(self) -> Event | None:
                await asyncio.sleep(60)
                return None

        monkeypatch.setattr(stt, "TRANSCRIBE_TIMEOUT", 0.05)
        hanging = Hanging()
        monkeypatch.setattr(
            stt, "AsyncClient", type("C", (), {"from_uri": staticmethod(lambda _u: hanging)})
        )

        # Whisper loading a model is a real silence; waiting on it forever is not
        # a way to survive one.
        status, _ = await post_audio()

        assert status == 502
        assert hanging.disconnected is True

    async def test_a_connection_that_never_opens_is_still_cleaned_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Stalling(FakeClient):
            async def connect(self) -> None:
                await asyncio.sleep(60)

        monkeypatch.setattr(stt, "CONNECT_TIMEOUT", 0.05)
        stalling = Stalling()
        monkeypatch.setattr(
            stt, "AsyncClient", type("C", (), {"from_uri": staticmethod(lambda _u: stalling)})
        )

        status, _ = await post_audio()

        assert status == 502
        assert stalling.disconnected is True

    async def test_without_the_dependency_it_says_so_rather_than_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stt._stt_state, "available", False)

        status, body = await post_audio()

        # 503 rather than 500: the deployment does not do this, which is a
        # different thing from having tried and failed.
        assert status == 503
        assert "wactorz[voice]" in body["error"]


class TestWhatTheBrowserIsTold:
    async def test_both_halves_of_the_answer_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "STT_MODE", "server")

        async with TestClient(TestServer(build_app())) as client:
            payload: dict[str, Any] = await (await client.get("/api/config")).json()

        # The branch is core's to report and the recogniser's reachability is the
        # extension's, and both land on the key named after the extension. Merging
        # rather than replacing is what keeps the first from disappearing wherever
        # the second is installed.
        assert payload["stt"]["mode"] == "server"
        assert payload["stt"]["available"] is True
