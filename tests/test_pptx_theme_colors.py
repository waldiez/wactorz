"""The deck's palette, which the model chooses from the *document's* own content.

So a crafted document decides what `theme_colors` holds. The deck is built in
Python and no colour is ever run, but each one still becomes six hex digits or
the default, so a bad palette costs the deck its colours and never the
conversion.
"""

from typing import Any

import pytest

from tests.programs import program_namespace

# The agent ships as source the framework exec's when spawning it, so its
# helpers are reachable only the way the framework reaches them.
_agent = program_namespace("doc_to_pptx_agent.py")
_hex_color = _agent["_hex_color"]
_palette = _agent["_palette"]

BREAKOUT = "x'}); require('child_process').execSync('id'); ({a:'"


class TestWhatCountsAsAColour:
    @pytest.mark.parametrize("value", ["1E2761", "ffffff", "AbCdEf", "000000"])
    def test_six_hex_digits_pass(self, value: str) -> None:
        assert _hex_color(value, "FALLBK") == value

    @pytest.mark.parametrize(
        "value",
        [
            BREAKOUT,
            "'; alert(1); '",
            "#1E2761",  # a leading hash is not six hex digits
            "1E276",  # five
            "1E27611",  # seven
            "GGGGGG",
            "",
            "   ",
            None,
            123456,
            ["1E2761"],
            {"hex": "1E2761"},
        ],
    )
    def test_anything_else_falls_back(self, value: Any) -> None:
        assert _hex_color(value, "FALLBK") == "FALLBK"

    def test_it_falls_back_rather_than_raising(self) -> None:
        # A bad palette is not a reason to fail a conversion the user asked for.
        assert _hex_color(BREAKOUT, "1E2761") == "1E2761"


class TestThePalette:
    def test_a_hostile_palette_becomes_the_default(self) -> None:
        hostile = dict.fromkeys(
            ("bg_dark", "bg_light", "accent", "text_dark", "text_light"), BREAKOUT
        )

        assert _palette({"theme_colors": hostile}) == _palette({})

    def test_every_colour_is_six_hex_digits(self) -> None:
        palette = _palette({"theme_colors": {"accent": BREAKOUT, "bg_dark": "#nope"}})

        assert all(len(c) == 6 and int(c, 16) >= 0 for c in palette.values())

    def test_a_valid_colour_is_kept(self) -> None:
        assert _palette({"theme_colors": {"accent": "AABBCC"}})["accent"] == "AABBCC"

    @pytest.mark.parametrize("colors", [None, "dark", ["1E2761"]])
    def test_a_palette_that_is_not_one_is_the_default(self, colors: Any) -> None:
        assert _palette({"theme_colors": colors}) == _palette({})
