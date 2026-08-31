"""An empty setting means "use the default", because that is what the template says.

`.env.template` ships `TTS_VOICE=` under the line "Leave TTS_VOICE empty for the
default (en-US-JennyNeural)". `load_dotenv` puts that in the environment as an
empty string rather than leaving it absent -- and `os.getenv(NAME, default)`
applies its default only when the name is *absent*. So the documented setup
handed the synthesiser `""`, which it refuses, and the only sign was
`Invalid voice ''` on every attempt to speak.

The same distinction the e2e harness is careful about, arriving from the other
side: there, a variable is emptied rather than deleted precisely because
`load_dotenv` refills an absent one.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from wactorz.ext.tts import public_config

DEFAULT = "en-US-JennyNeural"


@pytest.fixture(name="tts_state")
def tts_state_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A known default to resolve against, whatever the module warmed up with."""
    from wactorz.ext import tts

    monkeypatch.setattr(tts._tts_state, "default_voice", DEFAULT, raising=False)
    monkeypatch.setattr(tts._tts_state, "available", True, raising=False)
    return tts._tts_state


class TestTheVoiceOfferedToTheBrowser:
    def test_an_empty_setting_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:
        # What following `.env.template` actually produces.
        monkeypatch.setenv("TTS_VOICE", "")

        assert public_config(None)["voice"] == DEFAULT, (
            "an empty TTS_VOICE reached the synthesiser, which refuses it -- the "
            "template documents empty as meaning the default"
        )

    def test_an_absent_setting_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:
        monkeypatch.delenv("TTS_VOICE", raising=False)

        assert public_config(None)["voice"] == DEFAULT

    def test_a_chosen_voice_is_used(self, monkeypatch: pytest.MonkeyPatch, tts_state: Any) -> None:
        monkeypatch.setenv("TTS_VOICE", "en-GB-SoniaNeural")

        assert public_config(None)["voice"] == "en-GB-SoniaNeural"

    def test_whitespace_is_not_mistaken_for_a_voice(
        self, monkeypatch: pytest.MonkeyPatch, tts_state: Any
    ) -> None:
        # `.env` lines carry trailing spaces more often than anyone intends, and
        # a space is no more a voice name than an empty string is.
        monkeypatch.setenv("TTS_VOICE", "   ")

        assert public_config(None)["voice"] == DEFAULT


class TestTheTemplateAndTheCodeAgree:
    def test_the_template_still_documents_empty_as_the_default(self) -> None:
        """If that line ever goes, this test is what says the behaviour was deliberate."""
        template = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.template"
        )
        with open(template, encoding="utf-8") as handle:
            text = handle.read()

        assert "Leave TTS_VOICE empty for the default" in text, (
            "the code treats an empty TTS_VOICE as a request for the default because "
            "the template says to leave it empty; if that advice changed, this "
            "behaviour should be reconsidered rather than silently kept"
        )
