"""What reaches the synthesiser, and what must never be spoken.

edge-tts does not skip pictographs; it pronounces their Unicode names, so a
reply carrying one ends with Reachy announcing "smiling face with smiling eyes".
Stripping happens at synthesis because that is the one point every spoken word
crosses, whatever produced it.
"""

from __future__ import annotations

import pytest

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS: dict = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)


def _is_pictograph(char: str) -> bool:
    point = ord(char)
    if point in NS["_EMOJI_SINGLES"]:
        return True
    return any(low <= point <= high for low, high in NS["_EMOJI_RANGES"])


class TestEmojiAreNeverSpoken:
    @pytest.mark.parametrize(
        "text",
        [
            "Sure! \U0001f60a",
            "Done ✅",
            "Look ⭐",
            "Go ➡",
            "Family \U0001f468‍\U0001f469‍\U0001f467",
            "Greek flag \U0001f1ec\U0001f1f7",
            "Keycap 1️⃣",
            "Sparkles ✨",
            "Clock ⏰",
            "Play ▶",
        ],
    )
    def test_no_pictograph_survives(self, text: str) -> None:
        """edge-tts reads their Unicode names aloud rather than skipping them."""
        out = NS["_strip_emoji"](text)
        assert all(not _is_pictograph(ch) for ch in out), f"{out!r} still carries a pictograph"

    def test_real_punctuation_is_left_alone(self) -> None:
        """Dashes, curly quotes and ellipsis belong in speech and must survive."""
        text = "It is 22 degrees — clear, “nice”… yes"
        assert NS["_strip_emoji"](text) == text

    def test_greek_is_untouched(self) -> None:
        greek = "Γεια σου Ρίτσι"
        assert NS["_strip_emoji"](greek) == greek

    def test_a_message_of_nothing_but_emoji_becomes_empty(self) -> None:
        assert NS["_strip_emoji"]("\U0001f600\U0001f600\U0001f600") == ""

    def test_the_gap_left_behind_does_not_become_a_double_space(self) -> None:
        assert NS["_strip_emoji"]("all \U0001f60a good") == "all good"
