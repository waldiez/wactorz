"""Which voice speaks a sentence, and when Reachy is allowed to change voices.

A single-language voice handed Greek does not fail - it reads the Unicode
letter names aloud, so "Γεια σου" becomes "gamma epsilon iota alpha ...", and
takes several times as long doing it. Hence the swap to a Greek voice.

The swap has a cost of its own: Reachy becomes a different person in Greek than
in English. A Multilingual voice pronounces Greek itself and so removes the
reason to pay that cost, which is why those are exempt - and that exemption is
the part a later reader is most likely to mistake for a bug, so it is what these
tests pin down.
"""

from __future__ import annotations

import pytest

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS: dict = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)

GREEK = "Γεια σου, με λένε Ρίτσι."
ENGLISH = "Hello, my name is Reachy."
MULTILINGUAL = "en-US-BrianMultilingualNeural"
ENGLISH_ONLY = "en-HK-SamNeural"


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own `.env` must not decide what these tests assert."""
    monkeypatch.delenv("TTS_VOICE_EL", raising=False)


def voice_for(text: str, default: str) -> str:
    return NS["_voice_for_text"](text, default)


class TestASingleLanguageVoiceGivesWayToGreek:
    def test_greek_text_moves_off_an_english_only_voice(self) -> None:
        assert voice_for(GREEK, ENGLISH_ONLY) == "el-GR-AthinaNeural"

    def test_english_text_keeps_the_configured_voice(self) -> None:
        assert voice_for(ENGLISH, ENGLISH_ONLY) == ENGLISH_ONLY

    def test_a_greek_voice_is_not_swapped_for_another_greek_voice(self) -> None:
        assert voice_for(GREEK, "el-GR-NestorasNeural") == "el-GR-NestorasNeural"

    def test_a_stray_greek_letter_does_not_hijack_an_english_sentence(self) -> None:
        # Latin still dominates, so this stays in English rather than having one
        # symbol drag a whole sentence into a Greek voice.
        assert voice_for("The angle is 45 degrees, call it θ.", ENGLISH_ONLY) == ENGLISH_ONLY

    def test_text_with_no_letters_at_all_keeps_the_configured_voice(self) -> None:
        assert voice_for("42 - 7 = 35!", ENGLISH_ONLY) == ENGLISH_ONLY


class TestAMultilingualVoiceKeepsItsOwnIdentity:
    """Reachy has to sound like one person whatever language he is in."""

    def test_greek_text_stays_on_a_multilingual_voice(self) -> None:
        assert voice_for(GREEK, MULTILINGUAL) == MULTILINGUAL, (
            "a Multilingual voice pronounces Greek itself, so swapping it away "
            "changes Reachy's voice mid-conversation for no gain"
        )

    def test_english_text_stays_on_a_multilingual_voice(self) -> None:
        assert voice_for(ENGLISH, MULTILINGUAL) == MULTILINGUAL

    def test_the_check_does_not_depend_on_how_the_name_is_cased(self) -> None:
        assert voice_for(GREEK, "en-au-williammultilingualneural") == (
            "en-au-williammultilingualneural"
        )

    def test_every_multilingual_voice_edge_tts_ships_is_recognised(self) -> None:
        """Named in full so a renamed or dropped voice shows up here, not in a room."""
        for voice in (
            "en-AU-WilliamMultilingualNeural",
            "en-US-AndrewMultilingualNeural",
            "en-US-AvaMultilingualNeural",
            "en-US-BrianMultilingualNeural",
            "en-US-EmmaMultilingualNeural",
            "fr-FR-VivienneMultilingualNeural",
            "fr-FR-RemyMultilingualNeural",
            "de-DE-SeraphinaMultilingualNeural",
            "de-DE-FlorianMultilingualNeural",
            "it-IT-GiuseppeMultilingualNeural",
            "ko-KR-HyunsuMultilingualNeural",
            "pt-BR-ThalitaMultilingualNeural",
        ):
            assert voice_for(GREEK, voice) == voice, f"{voice} was swapped away from Greek"


class TestTheGreekVoiceIsConfigurable:
    def test_a_chosen_greek_voice_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TTS_VOICE_EL", "el-GR-NestorasNeural")

        assert voice_for(GREEK, ENGLISH_ONLY) == "el-GR-NestorasNeural"

    def test_an_empty_setting_means_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # What `.env.template` ships, via load_dotenv: present, and empty.
        monkeypatch.setenv("TTS_VOICE_EL", "")

        assert voice_for(GREEK, ENGLISH_ONLY) == "el-GR-AthinaNeural"

    def test_whitespace_is_not_mistaken_for_a_voice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TTS_VOICE_EL", "   ")

        assert voice_for(GREEK, ENGLISH_ONLY) == "el-GR-AthinaNeural"

    def test_it_cannot_override_a_voice_that_already_speaks_greek(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is the fallback for voices that cannot, not a global Greek setting."""
        monkeypatch.setenv("TTS_VOICE_EL", "el-GR-NestorasNeural")

        assert voice_for(GREEK, MULTILINGUAL) == MULTILINGUAL


class TestTheTemplateAndTheCodeAgree:
    def test_the_template_documents_the_greek_override(self) -> None:
        with open(".env.template", encoding="utf-8") as handle:
            text = handle.read()

        assert "TTS_VOICE_EL=" in text
        assert "el-GR-NestorasNeural" in text, (
            "edge-tts ships only two Greek voices; the template names both so the "
            "choice does not require reading the source"
        )
