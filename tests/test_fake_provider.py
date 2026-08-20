"""The provider that answers without a model.

Its whole value is that a system built on it behaves the same way twice, so the
tests worth having are the ones that would fail if it stopped being predictable
or stopped looking like a real provider to the code around it.

Two of them guard properties that are easy to lose and expensive to lose
quietly: that the contract-bound call sites get the shape they parse, and that a
call reports a cost above zero.
"""

import asyncio
import json
from typing import Any

import pytest

from wactorz.agents.llm.base import LLMProvider
from wactorz.agents.llm.providers.fake import (
    DEFAULT_REPLY,
    INTENTS,
    NO_FACTS,
    FakeProvider,
    parse_script,
)
from wactorz.agents.prompts.main_actor_prompts import (
    FACTS_EXTRACT_PROMPT,
    INTENT_CLASSIFIER_PROMPT,
    ORCHESTRATOR_PROMPT,
)


def user(text: str) -> list[dict[str, Any]]:
    """One user turn, the shape every provider is handed."""
    return [{"role": "user", "content": text}]


async def collect(provider: FakeProvider, messages: list[dict[str, Any]], system: str = "") -> str:
    """Everything `stream` yields, joined — what a caller ends up displaying."""
    return "".join([chunk async for chunk in provider.stream(messages, system=system)])


class TestTheContractBoundCallSites:
    """Two callers parse the answer and treat anything else as a failure."""

    def test_the_classifier_gets_a_token_it_accepts(self) -> None:
        """An echo would return the question here, and the classifier would
        fall through to its parse-failure branch — which is the nondeterminism
        this provider exists to remove.
        """
        provider = FakeProvider()
        reply, _ = asyncio.run(
            provider.complete(user("what is the weather?"), system=INTENT_CLASSIFIER_PROMPT)
        )
        assert reply in INTENTS

    @pytest.mark.parametrize("intent", INTENTS)
    def test_a_scenario_can_choose_the_intent(self, intent: str) -> None:
        provider = FakeProvider(intent=intent)
        reply, _ = asyncio.run(provider.complete(user("anything"), system=INTENT_CLASSIFIER_PROMPT))
        assert reply == intent

    def test_an_unknown_intent_is_refused_at_construction(self) -> None:
        """Refused where it is written, not silently downgraded at answer time.

        A scenario asking for an intent that does not exist has a bug in it, and
        finding out at construction is cheaper than a run that quietly routed
        somewhere else.
        """
        with pytest.raises(ValueError, match="intent must be one of"):
            FakeProvider(intent="PLANNER")

    def test_fact_extraction_gets_parseable_json_meaning_nothing_new(self) -> None:
        """The caller strips code fences and `json.loads` the rest.

        Returning prose here does not fail loudly — it writes junk into the
        user's persisted facts or silently no-ops, and both are worse than an
        error.
        """
        provider = FakeProvider()
        reply, _ = asyncio.run(
            provider.complete(user("my name is Sam"), system=FACTS_EXTRACT_PROMPT)
        )
        assert reply == NO_FACTS
        assert json.loads(reply) == {}

    def test_the_routing_is_on_the_prompt_constants_not_on_wording(self) -> None:
        """Matching the constant means rewording a prompt cannot break routing.

        A substring match on prose would, and the failure would look like the
        classifier misbehaving rather than like a moved string.
        """
        provider = FakeProvider()
        near_miss = INTENT_CLASSIFIER_PROMPT + "\n"
        reply, _ = asyncio.run(provider.complete(user("hello"), system=near_miss))
        assert reply not in INTENTS
        assert reply == DEFAULT_REPLY


class TestOrdinaryConversation:
    """Everything that is not one of those two is answered from the script."""

    def test_without_a_script_every_reply_is_the_default(self) -> None:
        provider = FakeProvider()
        reply, _ = asyncio.run(provider.complete(user("hello"), system=ORCHESTRATOR_PROMPT))
        assert reply == DEFAULT_REPLY

    def test_a_script_entry_answers_when_its_key_appears(self) -> None:
        provider = FakeProvider(script={"weather": "It is 22 degrees and clear."})
        reply, _ = asyncio.run(provider.complete(user("what is the weather in Athens?")))
        assert reply == "It is 22 degrees and clear."

    def test_matching_ignores_case(self) -> None:
        provider = FakeProvider(script={"Weather": "sunny"})
        reply, _ = asyncio.run(provider.complete(user("WHAT IS THE WEATHER")))
        assert reply == "sunny"

    def test_the_longest_matching_key_wins(self) -> None:
        """So a broad entry added later cannot shadow a precise one.

        Ordering by length rather than insertion also means the answer does not
        depend on how the script happened to be written down.
        """
        provider = FakeProvider(
            script={"weather": "generic", "weather in Athens": "specifically Athens"}
        )
        reply, _ = asyncio.run(provider.complete(user("the weather in Athens please")))
        assert reply == "specifically Athens"

    def test_an_unmatched_message_falls_back_rather_than_failing(self) -> None:
        provider = FakeProvider(script={"weather": "sunny"})
        reply, _ = asyncio.run(provider.complete(user("something else entirely")))
        assert reply == DEFAULT_REPLY

    def test_the_last_user_turn_is_what_matches(self) -> None:
        """History is present, but the current question is what is answered."""
        provider = FakeProvider(script={"second": "matched the second"})
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ]
        reply, _ = asyncio.run(provider.complete(messages))
        assert reply == "matched the second"

    def test_block_content_is_flattened_rather_than_crashing(self) -> None:
        """Attachments make content a list of blocks.

        A fake has no use for an image, but a provider that raises on one turns
        an attachment scenario into a provider bug.
        """
        provider = FakeProvider(script={"caption": "a cat"})
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"data": "..."}},
                    {"type": "text", "text": "write a caption"},
                ],
            }
        ]
        reply, _ = asyncio.run(provider.complete(messages))
        assert reply == "a cat"

    def test_no_messages_at_all_still_answers(self) -> None:
        provider = FakeProvider()
        reply, _ = asyncio.run(provider.complete([]))
        assert reply == DEFAULT_REPLY


class TestItLooksLikeARealProvider:
    """The code around it must not be able to tell, except by the answers."""

    def test_it_declares_streaming_support(self) -> None:
        """`supports_streaming` reads whether `_stream` was overridden.

        A fake that answered False would silently take the non-streaming branch
        and no scenario asserting "the reply streamed" could ever pass.
        """
        assert FakeProvider.supports_streaming() is True

    def test_streaming_yields_more_than_one_chunk_for_a_real_sentence(self) -> None:
        """Otherwise "it streamed" is indistinguishable from one blob."""
        provider = FakeProvider(script={"x": "y" * 200})
        chunks = asyncio.run(_chunks(provider, user("x")))
        assert len(chunks) > 1

    def test_streaming_and_completing_give_the_same_answer(self) -> None:
        provider = FakeProvider(script={"weather": "It is 22 degrees and clear."})
        streamed = asyncio.run(collect(provider, user("the weather")))
        completed, _ = asyncio.run(provider.complete(user("the weather")))
        assert streamed == completed

    def test_usage_carries_the_keys_every_caller_reads(self) -> None:
        provider = FakeProvider()
        _, usage = asyncio.run(provider.complete(user("hello")))
        assert set(usage) == {"input_tokens", "output_tokens", "cost_usd"}

    def test_a_call_costs_more_than_nothing(self) -> None:
        """The assertion this provider exists to keep honest.

        "The cost total moves while an agent is answering" is the criterion
        written after a regression froze that number while every test stayed
        green. A provider reporting zero would make it pass without the total
        ever moving — vacuously green, which is worse than red.
        """
        provider = FakeProvider()
        _, usage = asyncio.run(provider.complete(user("hello")))
        assert usage["cost_usd"] > 0
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0

    def test_even_the_tersest_exchange_is_not_free(self) -> None:
        """The `max(1, …)` floor, and it is not cosmetic.

        Token counts are length divided by four, so a two-word question and a
        two-letter answer both round to zero without the floor — and a zero-cost
        call makes "the total moves while answering" pass while the total sits
        still, for the shortest exchanges, which is where a demo starts. Caught
        by mutation: removing the floor failed nothing until this existed.
        """
        provider = FakeProvider(script={"x": "ok"})
        _, usage = asyncio.run(provider.complete(user("x")))
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        assert usage["cost_usd"] > 0

    def test_a_longer_exchange_costs_more(self) -> None:
        """So totals grow with the work rather than by a constant per call."""
        provider = FakeProvider()
        _, small = asyncio.run(provider.complete(user("hi")))
        _, large = asyncio.run(provider.complete(user("hi " * 500)))
        assert large["input_tokens"] > small["input_tokens"]
        assert large["cost_usd"] > small["cost_usd"]

    def test_the_spend_cap_applies_because_the_base_applies_it(self) -> None:
        """Reached through `complete`, not `_complete`, so the cap is not bypassed.

        This is a property of the base class rather than of this provider, and
        it is asserted here because a fake that was reached some other way would
        be the one place the cap silently did not hold.
        """
        assert type(FakeProvider()).complete is LLMProvider.complete

    def test_it_records_what_it_was_asked(self) -> None:
        """Without this, a silent no-call and a silent empty answer look alike."""
        provider = FakeProvider()
        asyncio.run(provider.complete(user("first"), system=ORCHESTRATOR_PROMPT))
        asyncio.run(provider.complete(user("second"), system=INTENT_CLASSIFIER_PROMPT))
        assert len(provider.calls) == 2
        assert provider.calls[0][0] == ORCHESTRATOR_PROMPT
        assert provider.calls[1][0] == INTENT_CLASSIFIER_PROMPT


class TestDeterminism:
    """The reason to prefer this over a cheap real model."""

    def test_the_same_request_gives_the_same_answer_and_the_same_cost(self) -> None:
        provider = FakeProvider(script={"weather": "It is 22 degrees and clear."})
        first = asyncio.run(provider.complete(user("the weather")))
        second = asyncio.run(provider.complete(user("the weather")))
        assert first == second

    def test_two_providers_built_the_same_way_agree(self) -> None:
        """A run is reproducible across processes, not just within one."""
        script = {"weather": "It is 22 degrees and clear."}
        a, _ = asyncio.run(FakeProvider(script=script).complete(user("the weather")))
        b, _ = asyncio.run(FakeProvider(script=script).complete(user("the weather")))
        assert a == b


class TestParseScript:
    """Scripts arrive as JSON, because they arrive through the environment."""

    def test_an_object_of_strings_comes_through(self) -> None:
        assert parse_script('{"weather": "sunny"}') == {"weather": "sunny"}

    def test_values_are_coerced_to_strings(self) -> None:
        assert parse_script('{"count": 3}') == {"count": "3"}

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "not json", "[1, 2]", '"a string"', "null"],
        ids=["empty", "blank", "prose", "array", "string", "null"],
    )
    def test_anything_else_is_an_empty_script_rather_than_an_error(self, raw: str) -> None:
        """A malformed script falls back to the default reply.

        The reply text is never what a scenario is testing, so failing a whole
        run over it would trade a real signal for a cosmetic one.
        """
        assert not parse_script(raw)


class TestTheFactory:
    """It is reached like any other backend, not patched in."""

    def test_it_is_registered_under_a_name(self) -> None:
        from wactorz.llm_factory import create_provider

        assert isinstance(create_provider("fake"), FakeProvider)

    def test_the_script_and_intent_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.llm_factory import create_provider

        monkeypatch.setenv("LLM_FAKE_SCRIPT", '{"weather": "sunny"}')
        monkeypatch.setenv("LLM_FAKE_INTENT", "PIPELINE")
        provider = create_provider("fake")
        assert isinstance(provider, FakeProvider)
        assert provider.intent == "PIPELINE"
        reply, _ = asyncio.run(provider.complete(user("the weather")))
        assert reply == "sunny"

    def test_an_unusable_intent_from_the_environment_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The environment is not a scenario file — a typo there should not stop
        a run, and OTHER is the answer that routes to ordinary chat.
        """
        from wactorz.llm_factory import create_provider

        monkeypatch.setenv("LLM_FAKE_INTENT", "NONSENSE")
        provider = create_provider("fake")
        assert isinstance(provider, FakeProvider)
        assert provider.intent == "OTHER"

    def test_it_is_not_what_an_empty_provider_name_means(self) -> None:
        """`none`/`""` still disables the provider entirely.

        Worth pinning: the no-provider path has its own behaviour — callers get
        `None` and the bridge answers with an error — and quietly turning that
        into a working fake would hide every misconfiguration.
        """
        from wactorz.llm_factory import create_provider

        assert create_provider("") is None
        assert create_provider("none") is None


async def _chunks(provider: FakeProvider, messages: list[dict[str, Any]]) -> list[str]:
    return [chunk async for chunk in provider.stream(messages)]
