# SPDX-License-Identifier: Apache-2.0
"""Refreshing model prices from LiteLLM's catalogue.

The fetch happens at most once a day and never twice at once. Only entries that
carry both an input and an output price are kept, converted to dollars per
million tokens to match the built-in table, with any long-prompt tier that has
both its rates; a failed fetch keeps whatever was there, so costing carries on
from the fallback prices.
"""

import time
from typing import Any

import pytest

from wactorz.agents.llm import pricing
from wactorz.agents.llm.pricing import Price


class _Response:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.fetches = 0

    def __call__(self) -> "_Session":
        return self

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str, timeout: Any = None) -> _Response:
        self.fetches += 1
        return _Response(self.data)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_dynamic_pricing", {})
    monkeypatch.setattr(pricing, "_dynamic_pricing_ts", 0.0)
    monkeypatch.setattr(pricing, "_pricing_fetch_in_progress", False)


def _numbers(price: Price) -> tuple[float, ...]:
    """A price's rates and each tier's threshold and rates, in order, for `pytest.approx`."""
    numbers = [price.input, price.output]
    for tier in price.tiers:
        numbers += [tier.above, tier.input, tier.output]
    return tuple(numbers)


def _serve(monkeypatch: pytest.MonkeyPatch, data: Any) -> _Session:
    session = _Session(data)
    monkeypatch.setattr(pricing.aiohttp, "ClientSession", session)
    return session


async def test_complete_entries_are_kept_per_million_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        {
            "sample_spec": "not a model",
            "model-a": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
            "model-b": {"input_cost_per_token": 0.000001},
        },
    )

    await pricing.refresh_pricing()

    assert {model: _numbers(price) for model, price in pricing._dynamic_pricing.items()} == {
        "model-a": pytest.approx((1.0, 2.0))
    }
    assert pricing._pricing_fetch_in_progress is False


async def test_a_fresh_catalogue_is_not_fetched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _serve(monkeypatch, {})
    monkeypatch.setattr(pricing, "_dynamic_pricing", {"model-a": Price(1.0, 2.0)})
    monkeypatch.setattr(pricing, "_dynamic_pricing_ts", time.time())

    await pricing.refresh_pricing()

    assert session.fetches == 0


async def test_a_fetch_already_running_is_not_duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _serve(monkeypatch, {})
    monkeypatch.setattr(pricing, "_pricing_fetch_in_progress", True)

    await pricing.refresh_pricing()

    assert session.fetches == 0


async def test_a_failed_fetch_keeps_the_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _serve(monkeypatch, ValueError("not json"))

    await pricing.refresh_pricing()

    assert pricing._dynamic_pricing == {}
    assert "using fallback" in caplog.text
    assert pricing._pricing_fetch_in_progress is False


async def test_a_long_prompt_tier_is_read_with_its_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As the catalogue lists Claude Haiku 5.5; the batch rates are not the standard request's.
    _serve(
        monkeypatch,
        {
            "claude-haiku-5-5": {
                "input_cost_per_token": 1e-7,
                "output_cost_per_token": 5e-7,
                "input_cost_per_token_above_100k_tokens": 5e-7,
                "output_cost_per_token_above_100k_tokens": 2.5e-6,
                "input_cost_per_token_above_100k_tokens_batches": 2.5e-7,
                "output_cost_per_token_above_100k_tokens_batches": 1.25e-6,
            },
        },
    )

    await pricing.refresh_pricing()

    assert list(pricing._dynamic_pricing) == ["claude-haiku-5-5"]
    assert _numbers(pricing._dynamic_pricing["claude-haiku-5-5"]) == pytest.approx(
        (0.10, 0.50, 100_000, 0.50, 2.50)
    )
    assert pricing.calc_cost("claude-haiku-5-5", 100_001, 0) == pytest.approx(100_001 * 0.50 / 1e6)


async def test_a_tier_without_both_rates_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(
        monkeypatch,
        {
            "model-a": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                "input_cost_per_token_above_200k_tokens": 0.000003,
                "output_cost_per_token_above_128k_tokens": 0.000004,
                "input_cost_per_token_above_256k_tokens": None,
                "output_cost_per_token_above_256k_tokens": 0.000005,
            },
        },
    )

    await pricing.refresh_pricing()

    assert pricing._dynamic_pricing["model-a"].tiers == ()


async def test_several_tiers_are_kept_lowest_threshold_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        {
            "model-a": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                "input_cost_per_token_above_256k_tokens": 0.000005,
                "output_cost_per_token_above_256k_tokens": 0.000006,
                "input_cost_per_token_above_32k_tokens": 0.000003,
                "output_cost_per_token_above_32k_tokens": 0.000004,
            },
        },
    )

    await pricing.refresh_pricing()

    tiers = pricing._dynamic_pricing["model-a"].tiers
    assert [tier.above for tier in tiers] == [32_000, 256_000]
    assert pricing.calc_cost("model-a", 100_000, 0) == pytest.approx(100_000 * 3.0 / 1e6)
    assert pricing.calc_cost("model-a", 300_000, 0) == pytest.approx(300_000 * 5.0 / 1e6)
    assert pricing.pricing_info("model-a")["tiers"] == [
        {
            "above_tokens": 32_000,
            "input_per_1m": pytest.approx(3.0),
            "output_per_1m": pytest.approx(4.0),
        },
        {
            "above_tokens": 256_000,
            "input_per_1m": pytest.approx(5.0),
            "output_per_1m": pytest.approx(6.0),
        },
    ]
