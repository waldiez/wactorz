"""Transcripts as they would be written, from recognisers that answer in capitals."""

import pytest

from wactorz.ext.stt.written import as_written


class TestCapitals:
    @pytest.mark.parametrize(
        ("heard", "written"),
        [
            ("WHAT TIME IS IT", "What time is it"),
            ("I THINK I'M LATE", "I think I'm late"),
            ("  HELLO", "  Hello"),
            ("OK. TURN ON THE LIGHTS", "Ok. Turn on the lights"),
            ("REALLY? YES!", "Really? Yes!"),
        ],
    )
    def test_all_capitals_become_sentence_case(self, heard: str, written: str) -> None:
        assert as_written(heard) == written

    @pytest.mark.parametrize("heard", ["What time is it?", "Turn on the TV", "", "123", "  "])
    def test_anything_already_written_is_left_as_it_is(self, heard: str) -> None:
        # Mixed case is the recogniser's own choice: "TV" there is deliberate.
        assert as_written(heard) == heard
