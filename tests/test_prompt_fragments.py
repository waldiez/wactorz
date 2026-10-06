"""Main's prompts with Home Assistant and without it.

The templates in ``main_actor_prompts.py`` render with whatever fragments the
installation has. Two things have to hold at once: with every fragment the
result is the prompt a Home Assistant installation has always had (the byte
for byte check is ``tests/test_prompts_are_pinned.py``), and with none it never
mentions Home Assistant, so a model on an installation without one is never
taught to answer about lights.
"""

from __future__ import annotations

import re
from operator import attrgetter

import pytest

from wactorz.agents.prompts.assemble import PromptFragment, Slot, one_of, render, slot_names
from wactorz.agents.prompts.home_assistant_prompts import HOME_ASSISTANT_FRAGMENT
from wactorz.agents.prompts.main_actor_prompts import (
    CORE_INTENT_TOKENS,
    DEFAULT_FRAGMENTS,
    INTENT_CLASSIFIER_MARKER,
    INTENT_CLASSIFIER_PROMPT,
    INTENT_CLASSIFIER_TEMPLATE,
    INTENT_TOKENS,
    ORCHESTRATOR_PROMPT,
    ORCHESTRATOR_TEMPLATE,
    intent_classifier_prompt,
    intent_tokens,
    orchestrator_prompt,
)

#: What a prompt for an installation without Home Assistant must not say.
#: "automation" is not among them: the core speaks of always-on automations in
#: the general sense, which is what a pipeline is on any installation.
HOME_ASSISTANT_TERMS = (
    "Home Assistant",
    "home-assistant",
    "homeassistant/",
    "ha_actuator",
    "smart home",
    "entity",
    "ACTUATE",
)
HA_AS_A_WORD = re.compile(r"\bHA\b")


class TestWithEveryFragment:
    """What an installation with everything configured gets."""

    def test_the_constants_are_the_prompts_rendered_with_every_fragment(self) -> None:
        assert orchestrator_prompt(DEFAULT_FRAGMENTS) == ORCHESTRATOR_PROMPT
        assert intent_classifier_prompt(DEFAULT_FRAGMENTS) == INTENT_CLASSIFIER_PROMPT
        assert intent_tokens(DEFAULT_FRAGMENTS) == INTENT_TOKENS

    def test_the_defaults_are_also_what_a_call_with_no_argument_gets(self) -> None:
        assert orchestrator_prompt() == ORCHESTRATOR_PROMPT
        assert intent_classifier_prompt() == INTENT_CLASSIFIER_PROMPT

    def test_home_assistant_is_among_the_defaults(self) -> None:
        assert HOME_ASSISTANT_FRAGMENT in DEFAULT_FRAGMENTS

    def test_its_intents_come_before_the_core_ones(self) -> None:
        """The classifier is shown ACTUATE and HA first, as it always has been."""
        assert HOME_ASSISTANT_FRAGMENT.intents + CORE_INTENT_TOKENS == INTENT_TOKENS

    def test_every_slot_is_filled_by_some_fragment(self) -> None:
        """A slot nothing fills is a hole in the core that nobody asked for."""
        filled = set()
        for fragment in DEFAULT_FRAGMENTS:
            filled |= set(fragment.orchestrator)
        assert filled == slot_names(ORCHESTRATOR_TEMPLATE)

        filled = set()
        for fragment in DEFAULT_FRAGMENTS:
            filled |= set(fragment.intent_classifier)
        assert filled == slot_names(INTENT_CLASSIFIER_TEMPLATE)


class TestWithoutHomeAssistant:
    """What an installation that has not configured it gets."""

    @pytest.mark.parametrize(
        "prompt",
        [orchestrator_prompt(()), intent_classifier_prompt(())],
        ids=["orchestrator", "classifier"],
    )
    def test_it_is_never_mentioned(self, prompt: str) -> None:
        for term in HOME_ASSISTANT_TERMS:
            assert term not in prompt, term
        assert not HA_AS_A_WORD.search(prompt)

    def test_the_classifier_offers_only_the_core_intents(self) -> None:
        assert intent_tokens(()) == CORE_INTENT_TOKENS
        assert "Respond with exactly one token: PIPELINE or OTHER." in intent_classifier_prompt(())

    def test_the_prompts_still_open_the_way_their_readers_expect(self) -> None:
        """The fake provider recognises the classifier by its first words, and
        main's own override block is prepended to a prompt that starts here.
        """
        assert intent_classifier_prompt(()).startswith(INTENT_CLASSIFIER_MARKER)
        assert orchestrator_prompt(()).startswith("== WHO YOU ARE ==")

    def test_an_example_the_fragment_replaces_keeps_a_neutral_default(self) -> None:
        """The contract example subscribes to a Home Assistant topic on an
        installation with one, and to a plain sensor topic otherwise; it is
        never left blank, since the example is what teaches the shape.
        """
        assert "subscribes=['homeassistant/state_changes/#']," in ORCHESTRATOR_PROMPT
        assert "subscribes=['sensors/temperature']," in orchestrator_prompt(())
        assert "homeassistant/state_changes" not in orchestrator_prompt(())

    def test_the_abilities_list_still_starts_with_an_ability(self) -> None:
        """Removing the smart-home bullet leaves the list intact, not a gap."""
        core = orchestrator_prompt(())
        header = "WHAT THE USER CAN ASK YOU FOR — describe your abilities in these terms:\n"
        assert header in core
        after = core.split(header, 1)[1]
        assert after.startswith("  - Build always-on automations from plain language")


class TestRendering:
    """The assembly itself, on small templates."""

    def test_a_slot_shows_its_default_when_nothing_fills_it(self) -> None:
        template = ("a ", Slot("x", "default"), " b", Slot("y"), ".")
        assert render(template, (), attrgetter("orchestrator")) == "a default b."

    def test_fragments_fill_a_slot_in_their_order_and_replace_the_default(self) -> None:
        template = ("a ", Slot("x", "default"), ".")
        first = PromptFragment("first", orchestrator={"x": "one"})
        second = PromptFragment("second", orchestrator={"x": " two"})
        assert render(template, (first, second), attrgetter("orchestrator")) == "a one two."
        assert render(template, (second, first), attrgetter("orchestrator")) == "a  twoone."

    def test_a_fragment_filling_a_slot_the_template_lacks_is_refused(self) -> None:
        """A misspelt slot name would otherwise vanish without a trace."""
        template = ("a ", Slot("x"))
        fragment = PromptFragment("typo", orchestrator={"ex": "text"})
        with pytest.raises(ValueError, match=r"'typo'.*'ex'"):
            render(template, (fragment,), attrgetter("orchestrator"))

    def test_only_the_mapping_the_template_belongs_to_is_consulted(self) -> None:
        """A fragment's classifier inserts are not an error for the orchestrator template."""
        template = ("a ", Slot("x"))
        fragment = PromptFragment("f", orchestrator={"x": "1"}, intent_classifier={"other": "2"})
        assert render(template, (fragment,), attrgetter("orchestrator")) == "a 1"

    def test_a_callable_part_sees_the_fragments(self) -> None:
        template = ("names: ", lambda fragments: ", ".join(f.name for f in fragments))
        fragments = (PromptFragment("a"), PromptFragment("b"))
        assert render(template, fragments, attrgetter("orchestrator")) == "names: a, b"

    @pytest.mark.parametrize(
        ("items", "expected"),
        [
            ((), ""),
            (("A",), "A"),
            (("A", "B"), "A or B"),
            (("A", "B", "C"), "A, B, or C"),
            (("ACTUATE", "HA", "PIPELINE", "OTHER"), "ACTUATE, HA, PIPELINE, or OTHER"),
        ],
    )
    def test_one_of(self, items: tuple[str, ...], expected: str) -> None:
        assert one_of(items) == expected
