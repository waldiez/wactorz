"""An empty setting means "use the default", because that is what the template says.

`.env.template` ships `TTS_VOICE=` under a line saying to leave it empty for the
default. `load_dotenv` puts that in the environment as an
empty string rather than leaving it absent -- and `os.getenv(NAME, default)`
applies its default only when the name is *absent*. So the documented setup
handed the synthesiser `""`, which it refuses, and the only sign was
`Invalid voice ''` on every attempt to speak.

The same distinction the e2e harness is careful about, arriving from the other
side: there, a variable is emptied rather than deleted precisely because
`load_dotenv` refills an absent one.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from wactorz.ext import tts, voice
from wactorz.ext.tts import public_config
from wactorz.ext.voice import settings as voice_settings

DEFAULT = "en-US-JennyNeural"


@pytest.fixture(name="tts_state")
def tts_state_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A known default to resolve against, whatever the module warmed up with."""
    monkeypatch.setattr(tts._tts_state, "default_voice", DEFAULT, raising=False)
    monkeypatch.setattr(tts._tts_state, "available", True, raising=False)
    return tts._tts_state


class TestTheVoiceOfferedToTheBrowser:
    def test_an_empty_setting_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:
        # What following `.env.template` actually produces.
        monkeypatch.setenv("TTS_VOICE", "")

        assert public_config(web.Application())["voice"] == DEFAULT, (
            "an empty TTS_VOICE reached the synthesiser, which refuses it -- the "
            "template documents empty as meaning the default"
        )

    def test_an_absent_setting_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:
        monkeypatch.delenv("TTS_VOICE", raising=False)

        assert public_config(web.Application())["voice"] == DEFAULT

    def test_a_chosen_voice_is_used(self, monkeypatch: pytest.MonkeyPatch, tts_state: Any) -> None:
        monkeypatch.setenv("TTS_VOICE", "en-GB-SoniaNeural")

        assert public_config(web.Application())["voice"] == "en-GB-SoniaNeural"

    def test_whitespace_is_not_mistaken_for_a_voice(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:
        # `.env` lines carry trailing spaces more often than anyone intends, and
        # a space is no more a voice name than an empty string is.
        monkeypatch.setenv("TTS_VOICE", "   ")

        assert public_config(web.Application())["voice"] == DEFAULT


class TestAVoiceTheSynthesiserDoesNotHave:
    """A browser's own voice saved as the deployment's would fail every reply."""

    @staticmethod
    def _voices(tts_state: Any, monkeypatch: pytest.MonkeyPatch, names: list[str] | None) -> None:
        listed = (
            None if names is None else [{"name": n, "locale": "en-GB", "gender": ""} for n in names]
        )
        monkeypatch.setattr(tts_state, "voices", listed)
        monkeypatch.delenv("WACTORZ_TTS_URI", raising=False)

    def test_one_not_on_the_list_is_named(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:

        self._voices(tts_state, monkeypatch, ["en-GB-SoniaNeural"])

        assert tts.unknown_voice("en-GB-SoniaNeural") == ""
        assert "Google UK English Female" in tts.unknown_voice("Google UK English Female")

    def test_nothing_is_refused_before_the_list_is_known(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:

        self._voices(tts_state, monkeypatch, None)

        assert tts.unknown_voice("Anything") == ""

    def test_a_named_service_may_have_any_voice(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:

        self._voices(tts_state, monkeypatch, ["en-GB-SoniaNeural"])
        monkeypatch.setenv("WACTORZ_TTS_URI", "tcp://piper:10200")

        # Its voices are its own and cannot be listed here.
        assert tts.unknown_voice("en_US-lessac-medium") == ""

    def test_with_no_synthesiser_there_is_no_voice_to_choose(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:

        self._voices(tts_state, monkeypatch, None)
        monkeypatch.setattr(tts_state, "available", False)

        assert "no synthesiser" in tts.unknown_voice("Google UK English Female")

    def test_one_stored_before_it_could_be_checked_gives_way_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any, caplog: pytest.LogCaptureFixture
    ) -> None:

        self._voices(tts_state, monkeypatch, ["en-GB-SoniaNeural"])
        monkeypatch.setattr(voice_settings, "voice", lambda: "Google UK English Free Voice")
        monkeypatch.setattr(tts, "_ignored_voices", set())

        with caplog.at_level("WARNING"):
            first = public_config(web.Application())["voice"]
            second = public_config(web.Application())["voice"]

        assert first == second == DEFAULT
        # Said once, not on every reply.
        assert caplog.text.count("Speaking in the default voice") == 1

    async def test_the_settings_route_refuses_it(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:

        self._voices(tts_state, monkeypatch, ["en-GB-SoniaNeural"])
        app = web.Application()
        voice.check_voices_with(app, tts.unknown_voice)

        class _Request:
            def __init__(self) -> None:
                self.app = app

            @staticmethod
            async def json() -> Any:
                return {"voice": "Google UK English Female"}

        response = await voice.voice_settings_handler(_Request())  # type: ignore[arg-type]

        assert response.status == 400
        assert "Google UK English Female" in json.loads(response.body)["error"]  # type: ignore[arg-type]


class TestTheTemplateAndTheCodeAgree:
    def test_the_template_still_documents_empty_as_the_default(self) -> None:
        """If that line ever goes, this test is what says the behaviour was deliberate."""
        template = Path(__file__).resolve().parents[1] / ".env.template"
        text = template.read_text(encoding="utf-8")

        assert "Leave TTS_VOICE empty for the default" in text, (
            "the code treats an empty TTS_VOICE as a request for the default because "
            "the template says to leave it empty; if that advice changed, this "
            "behaviour should be reconsidered rather than silently kept"
        )
