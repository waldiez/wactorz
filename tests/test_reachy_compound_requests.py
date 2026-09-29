"""One sentence, two requests, and the half that must not be dropped.

`_embodied_command_for_text` matches a verb *anywhere* in a sentence and returns
exactly one command, and `handle_task` then replaces the whole request with it.
For "do a dance" that is right and saves an LLM call. For "turn on the light and
do a dance" it discards the smart-home half with no error and no mention, and
the reply reads as a success — which is the shape of failure a user cannot tell
apart from the robot simply not managing the task.

Only the planner can emit a robot command and a Home Assistant command from one
sentence, so a request naming both belongs to it.
"""

from __future__ import annotations

import pytest

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS: dict = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)


def shortcut(text: str):
    return NS["_embodied_command_for_text"](text)


class TestASentenceThatAlsoAsksForSomethingElse:
    """These must reach the planner, which can emit a robot command and a Home
    Assistant command from one sentence."""

    @pytest.mark.parametrize(
        "text",
        [
            "turn on the light and make it green and do a dance",
            "turn the light green and do a dance",
            "turn on the light and nod",
            "do a dance and turn the light red",
            "dim the lamp then wiggle",
            "turn off the switch and look around",
            "put the tv on and nod",
        ],
    )
    def test_the_shortcut_stands_aside(self, text: str) -> None:
        assert shortcut(text) is None, (
            "the shortcut answered a two-part request with one command, which "
            "silently discards the other part"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "άναψε το φως και χόρεψε",
            "σβήσε τη λάμπα και κούνα τις κεραίες",
        ],
    )
    def test_it_stands_aside_in_greek_too(self, text: str) -> None:
        """Greek joins clauses with «και», which no English joiner would catch."""
        assert shortcut(text) is None


class TestSingleRequestsStayInstant:
    """The shortcut exists to skip an LLM call. Deferring everything with a
    conjunction in it would trade one bug for a slower robot."""

    @pytest.mark.parametrize(
        ("text", "expected_cmd"),
        [
            ("do a dance", "gesture"),
            ("nod", "gesture"),
            ("turn around", "gesture"),
            ("look around", "look_around"),
            ("what do you see", "look_around"),
            ("calm down", "life"),
            ("showtime", "life"),
            ("antennas only", "life"),
            ("stop moving", "life"),
        ],
    )
    def test_it_still_answers_directly(self, text: str, expected_cmd: str) -> None:
        result = shortcut(text)
        assert result is not None, f"{text!r} lost its shortcut"
        assert result["cmd"] == expected_cmd

    def test_a_conjunction_alone_does_not_defer(self) -> None:
        """ "Turn around and tell me what you see" is one `look_behind`, not two
        requests — the command already does both halves."""
        result = shortcut("Turn around and tell me what you see")

        assert result is not None
        assert result["cmd"] == "look_behind"

    def test_a_light_named_without_a_second_request_is_untouched(self) -> None:
        """No joiner, so nothing was dropped; this always went to the planner."""
        assert shortcut("turn on the light") is None

    def test_around_is_not_mistaken_for_the_word_and(self) -> None:
        """The joiner is matched with spaces, so "around" cannot trip it."""
        assert shortcut("look around") is not None


class TestTheGuardItself:
    def test_a_joiner_with_no_other_domain_is_not_compound(self) -> None:
        assert NS["_asks_for_more_than_one_thing"]("nod and smile", "nod and smile") is False

    def test_a_joiner_plus_a_device_is_compound(self) -> None:
        text = "turn on the light and nod"
        assert NS["_asks_for_more_than_one_thing"](text, text) is True

    def test_a_device_with_no_joiner_is_not_compound(self) -> None:
        text = "turn on the light"
        assert NS["_asks_for_more_than_one_thing"](text, text) is False

    def test_the_exempt_phrases_are_never_treated_as_compound(self) -> None:
        for phrase in NS["_JOINED_SINGLE_INTENTS"]:
            assert NS["_asks_for_more_than_one_thing"](phrase, phrase) is False

    def test_the_known_gap_is_recorded_rather_than_pretended_away(self) -> None:
        """Two robot verbs still take the shortcut and only the first happens.

        Asserted so the limitation is visible in the suite rather than found in a
        room. If this starts returning None the gap has been closed, and this
        test should become the opposite assertion.
        """
        assert shortcut("nod and dance") is not None
