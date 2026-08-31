"""Measurements as they are said, for a synthesiser that reads what it is given."""

from __future__ import annotations

import pytest

from wactorz.ext import tts
from wactorz.ext.tts.spoken import speakable


class TestUnitsThatWouldOtherwiseGoUnsaid:
    """The symbols a self-hosted synthesiser reads wrong, or not at all."""

    @pytest.mark.parametrize(
        ("written", "said"),
        [
            ("It is 21 °C", "It is 21 degrees Celsius"),
            ("It is 70 °F", "It is 70 degrees Fahrenheit"),
            ("turn it 90°", "turn it 90 degrees"),
            ("humidity 48%", "humidity 48 percent"),
            ("pressure 1013 hPa", "pressure 1013 hectopascals"),
            ("used 3.4 kWh", "used 3.4 kilowatt hours"),
        ],
    )
    def test_a_symbol_becomes_a_word(self, written: str, said: str) -> None:
        assert speakable(written) == said

    @pytest.mark.parametrize(
        ("written", "said"),
        [
            ("wind 5 m/s", "wind 5 metres per second"),
            ("gusts 12 km/h", "gusts 12 kilometres per hour"),
        ],
    )
    def test_a_rate_is_said_as_per(self, written: str, said: str) -> None:
        # Otherwise the solidus is read out: "five em slash ess".
        assert speakable(written) == said

    def test_one_of_something_is_singular(self) -> None:
        assert speakable("dropped 1 °C") == "dropped 1 degree Celsius"
        assert speakable("dropped 1.0 °C") == "dropped 1.0 degree Celsius"

    def test_none_of_something_is_not(self) -> None:
        # English counts zero as plural, and "0 degree" is worse than the symbol.
        assert speakable("0 °C") == "0 degrees Celsius"

    def test_the_space_is_optional(self) -> None:
        assert speakable("set to 20°C") == "set to 20 degrees Celsius"

    def test_a_unit_written_against_the_number_still_counts(self) -> None:
        # "100km" read as characters is "one hundred kay em".
        assert speakable("a 100km walk") == "a 100 kilometres walk"

    def test_the_longest_unit_wins(self) -> None:
        # `km` and `°` both match inside these, and matching them would leave
        # the rest of the unit dangling after the word.
        assert speakable("12 km/h") == "12 kilometres per hour"
        assert speakable("21 °C") == "21 degrees Celsius"


class TestWhatItLeavesAlone:
    """Only a measurement is a measurement; the same letters are words."""

    @pytest.mark.parametrize(
        "written",
        [
            "See /api/tts for the route",
            "the m/s field is documented",
            "docs/reference.md and infra/voice/stt",
        ],
    )
    def test_prose_and_paths_are_untouched(self, written: str) -> None:
        assert speakable(written) == written

    def test_a_number_glued_to_a_word_is_not_a_unit(self) -> None:
        # "5 metres" already says it; expanding the "m" would leave "etres".
        assert speakable("5 metres") == "5 metres"
        assert speakable("3 members") == "3 members"

    def test_a_version_is_not_a_measurement(self) -> None:
        assert speakable("version 1.2.3cm") == "version 1.2.3cm"


class TestWhatIsHandedToTheSynthesiser:
    """`worth_saying` is what every branch speaks through."""

    def test_it_says_the_units(self) -> None:
        assert "degrees Celsius" in tts.worth_saying("It is 21 °C outside.")

    def test_it_still_drops_code(self) -> None:
        said = tts.worth_saying("Run this:\n```\nprint(1)\n```\ndone")

        assert "print" not in said
        assert "code block" in said

    def test_it_still_stops_at_a_readable_length(self) -> None:
        assert len(tts.worth_saying("word " * 200)) <= 300
