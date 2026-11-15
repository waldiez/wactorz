# SPDX-License-Identifier: Apache-2.0
"""The langgraph_triage example: a LangGraph graph as an agent.

What it needs that is not installed for the suite is stood in for.
"""

from pathlib import Path

import pytest

from tests.examples import load
from wactorz.agents.function_agent import spec_of
from wactorz.agents.llm.providers.fake import FakeProvider


class TestTheLangGraphExample:
    async def test_a_ticket_goes_through_the_graph_without_a_model(self, tmp_path: Path) -> None:
        pytest.importorskip("langgraph")
        triage = load("langgraph_triage").triage
        spec = spec_of(triage)
        assert spec is not None and spec.concurrency == 4
        actor = spec.build(persistence_dir=str(tmp_path))

        result = await actor.call({"id": "T-1", "text": "The pump stopped, the line is down"})
        again = await actor.call({"id": "T-2", "text": "How do I change a threshold?"})

        assert result == {
            "id": "T-1",
            "category": "outage",
            "priority": "high",
            "reply": result["reply"],  # pyright: ignore[reportOptionalSubscript]
        }
        assert again is not None and (again["category"], again["priority"]) == ("question", "low")
        assert actor.options["_graph"] is not None, "compiled once, kept on the actor"
        assert actor.recall("tickets_total") == 2
        assert await actor.call({"id": "T-3"}) is None

    async def test_the_graph_uses_the_systems_model_when_it_has_one(self, tmp_path: Path) -> None:
        pytest.importorskip("langgraph")
        triage = load("langgraph_triage").triage
        model = FakeProvider(
            script={"invoice": "billing", "reply": "We will refund it today. Sorry."}
        )
        actor = spec_of(triage).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path), llm_provider=model
        )

        result = await actor.call({"id": "T-9", "text": "Charged twice on the invoice"})

        assert result is not None and result["category"] == "billing"
        assert result["priority"] == "medium"
        assert len(model.calls) == 2, "one call to classify, one to draft"
        assert actor.metrics.llm_calls == 2, "both counted on the card"
        assert actor.metrics.llm_input_tokens > 0
