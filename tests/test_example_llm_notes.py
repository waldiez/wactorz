"""The llm_notes example: notes in, one-line summaries out, with the model's cost kept.

What it needs that is not installed for the suite is stood in for.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.examples import load
from wactorz.agents.function_agent import spec_of
from wactorz.agents.llm.providers.fake import FakeProvider


class TestTheLLMNotesExample:
    @pytest.fixture
    def summarise(self) -> Any:
        return load("llm_notes").summarise

    async def test_a_note_is_summarised_with_the_model_and_the_cost_kept(
        self, summarise: Any, tmp_path: Path
    ) -> None:
        spec = spec_of(summarise)
        assert spec is not None
        assert spec.subscribes == ("notes/raw/#",) and spec.publishes == "notes/summary"
        model = FakeProvider(script={"pump": "The pump filter clogs nightly; replace it Friday."})
        actor = spec.build(persistence_dir=str(tmp_path), llm_provider=model)

        result = await actor.call({"text": "The pump on line 2 drops to 2.1 l/min at 19:00."})

        assert result is not None
        assert result["summary"] == "The pump filter clogs nightly; replace it Friday."
        assert result["cost_usd"] >= 0
        system, messages = model.calls[0]
        assert "one sentence" in system
        assert messages[-1]["content"].startswith("The pump")
        assert actor.recall("notes_total") == 1
        # The call through `me.llm` landed on the card by itself.
        assert actor.metrics.llm_calls == 1
        assert actor.metrics.llm_cost_usd == pytest.approx(result["cost_usd"])

    async def test_plain_text_is_accepted_as_it_arrives(
        self, summarise: Any, tmp_path: Path
    ) -> None:
        actor = spec_of(summarise).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path),
            llm_provider=FakeProvider(script={"dentist": "Call back."}),
        )

        result = await actor.call({"raw": "Call the dentist back on Tuesday."})

        assert result is not None and result["summary"] == "Call back."

    async def test_nothing_is_published_for_an_empty_note_or_without_a_model(
        self, summarise: Any, tmp_path: Path
    ) -> None:
        spec = spec_of(summarise)
        assert spec is not None
        with_model = spec.build(persistence_dir=str(tmp_path), llm_provider=FakeProvider())
        assert await with_model.call({"text": "   "}) is None

        without = spec.build(persistence_dir=str(tmp_path / "b"))
        assert await without.call({"text": "A note."}) is None
        assert without.recall("notes_total") is None
