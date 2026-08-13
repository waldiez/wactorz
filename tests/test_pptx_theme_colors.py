"""The one value in the generated deck script that is not JSON-encoded.

⚠ `theme_colors` is interpolated raw, inside single quotes, into JavaScript that
`node` executes — and the model chooses it from the *document's* own content. So
a crafted document could close the quote and append statements. Every other
value in that script goes through `json.dumps`; these five did not.
"""

import re
from typing import Any

import pytest

from wactorz.catalogue_agents.doc_to_pptx_agent import AGENT_CODE

# The agent ships as source the framework exec's when spawning it, so its
# helpers are reachable only the way the framework reaches them.
_agent: dict = {}
exec(AGENT_CODE, _agent)
_hex_color = _agent["_hex_color"]
_build_js = _agent["_build_js"]

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


class TestTheGeneratedScript:
    @staticmethod
    def _script(colors: dict[str, Any]) -> str:
        outline = {
            "title": "T",
            "theme_colors": colors,
            # `index` and `layout` are what `_build_js` reads off each slide.
            "slides": [{"index": 0, "layout": "title", "title": "S", "bullets": ["b"]}],
        }
        return _build_js(outline, {}, "/dev/null/out.pptx")

    def test_a_hostile_palette_injects_nothing(self) -> None:
        js = self._script(
            dict.fromkeys(("bg_dark", "bg_light", "accent", "text_dark", "text_light"), BREAKOUT)
        )

        assert "child_process" not in js
        assert "execSync" not in js

    def test_every_colour_in_the_script_is_six_hex_digits(self) -> None:
        # The real invariant: whatever the model sent, what lands in the script
        # can only ever be a colour.
        js = self._script({"accent": BREAKOUT, "bg_dark": "#nope", "text_light": "ABCDEF"})

        for found in re.findall(r"color:'([^']*)'", js):
            assert re.fullmatch(r"[0-9A-Fa-f]{6}", found), found

    def test_a_valid_palette_still_reaches_the_script(self) -> None:
        js = self._script({"accent": "AABBCC"})

        assert "AABBCC" in js
