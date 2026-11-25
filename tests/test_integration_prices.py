# SPDX-License-Identifier: Apache-2.0
"""AG2's and LangChain's tokens priced as Wactorz prices its own, unless the program says otherwise.

A price table the program gives wins for the models it names; any other model
is priced from Wactorz's own prices. LangChain reports each request, so its
long-prompt tier applies; AG2 reports a run's sum, which is priced at the
base rate, since the sum of many prompts says nothing about any one of them.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.test_library_integrations import Probe, _usage_report
from wactorz.agents.llm import cost as cost_module
from wactorz.agents.llm import pricing
from wactorz.agents.llm.pricing import Price, Tier, base_rates
from wactorz.core.integrations import ag2 as bridge
from wactorz.core.integrations.pricing import call_cost, cost_of, total_cost

#: A model only these tests price: $1/$2 per 1M, $10/$20 for a prompt over 1,000 tokens.
TIERED = "test-tiered-model"


@pytest.fixture(autouse=True)
def _wactorz_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wactorz's prices as these tests need them, whatever the catalogue fetched."""
    monkeypatch.setattr(pricing, "_dynamic_pricing", {})
    monkeypatch.setitem(
        pricing._FALLBACK_PRICING, TIERED, Price(1.0, 2.0, (Tier(1000, 10.0, 20.0),))
    )


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(cost_module, "accumulate_global_cost", recorded.append)
    return recorded


class TestTheFallback:
    def test_a_call_without_a_table_is_priced_at_its_tier(self) -> None:
        assert call_cost({}, TIERED, 500, 100) == pytest.approx((500 * 1 + 100 * 2) / 1e6)
        assert call_cost({}, TIERED, 2000, 100) == pytest.approx((2000 * 10 + 100 * 20) / 1e6)

    def test_a_total_without_a_table_is_priced_at_the_base_rate(self) -> None:
        # Over the threshold as a sum, which no single request need have been.
        assert total_cost({}, TIERED, 2000, 100) == pytest.approx((2000 * 1 + 100 * 2) / 1e6)

    def test_a_table_without_the_model_falls_back(self) -> None:
        other = {"someone-elses-model": (99.0, 99.0)}
        assert call_cost(other, TIERED, 500, 100) == call_cost({}, TIERED, 500, 100)
        assert total_cost(other, TIERED, 500, 100) == total_cost({}, TIERED, 500, 100)

    def test_a_table_that_names_the_model_wins(self) -> None:
        own = {"test-tiered": (0.5, 0.5)}  # a family prefix, as a table names one
        assert call_cost(own, TIERED, 2000, 0) == pytest.approx(2000 * 0.5 / 1e6)
        assert total_cost(own, TIERED, 2000, 0) == pytest.approx(2000 * 0.5 / 1e6)
        assert call_cost({TIERED: (0.0, 0.0)}, TIERED, 2000, 100) == 0.0, "free, as the table says"

    def test_a_model_priced_nowhere_costs_nothing(self) -> None:
        assert call_cost({}, "no-such-model", 10, 10) == 0.0
        assert total_cost({}, "no-such-model", 10, 10) == 0.0
        assert base_rates("no-such-model") is None

    def test_cost_of_still_reads_only_the_table(self) -> None:
        assert cost_of({}, TIERED, 500, 100) == 0.0

    def test_base_rates_are_a_short_prompts(self) -> None:
        assert base_rates(TIERED) == (1.0, 2.0)


class TestLangChain:
    def test_a_call_without_a_table_is_charged_at_wactorz_price(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        pytest.importorskip("langchain_core")
        from langchain_core.outputs import Generation, LLMResult

        from wactorz.core.integrations.langchain import CostCallback

        actor = Probe(name="triage", persistence_dir=str(tmp_path))
        result = LLMResult(
            generations=[[Generation(text="ok")]],
            llm_output={
                "token_usage": {"prompt_tokens": 2000, "completion_tokens": 100},
                "model_name": TIERED,
            },
        )

        CostCallback(actor).on_llm_end(result, run_id=None)

        expected = (2000 * 10 + 100 * 20) / 1e6
        assert actor.metrics.llm_cost_usd == pytest.approx(expected), "one request: its tier"
        assert ledger == [pytest.approx(expected)]


class TestAG2:
    @staticmethod
    def _reply(report: Any) -> Any:
        class Reply:
            async def usage(self) -> Any:
                return report

        return Reply()

    async def test_a_run_without_a_table_is_charged_at_the_base_rate(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        actor = Probe(name="review", persistence_dir=str(tmp_path))
        reply = self._reply(_usage_report(**{TIERED: (2000, 100)}))

        cost = await bridge.record_reply(actor, reply)

        assert cost == pytest.approx((2000 * 1 + 100 * 2) / 1e6)
        assert ledger == [pytest.approx(cost)]

    def test_each_model_of_a_run_is_priced_its_own_way(self) -> None:
        report = _usage_report(**{TIERED: (1000, 0), "own-model": (1000, 0), "unpriced": (5, 5)})

        cost, *_ = bridge.usage_totals(report, {"own-model": (3.0, 3.0)})

        assert cost == pytest.approx(1000 * 1 / 1e6 + 1000 * 3 / 1e6)
