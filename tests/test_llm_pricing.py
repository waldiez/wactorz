# SPDX-License-Identifier: Apache-2.0
"""Model pricing lookup.

Costs are computed from a table keyed by model-name *prefix*, so a dated or
suffixed variant (`gpt-4o-2024-08-06`) inherits its family's rate. The hazard is
that a shorter key can shadow a longer one: matching `gpt-4o` first means
`gpt-4o-mini` is billed at the full model's rate.

That is not cosmetic — the same number drives the reported spend, the budget
check and the spend cap, so a shadowed model silently overstates cost by the
ratio between the two rates.
"""

import pytest

from wactorz.agents.llm import pricing
from wactorz.agents.llm.pricing import _FALLBACK_PRICING, Price, Tier, calc_cost, pricing_info

_1M = 1_000_000


@pytest.fixture(name="no_live_catalogue", autouse=True)
def no_live_catalogue_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the fallback path — the live catalogue is empty until fetched."""
    monkeypatch.setattr(pricing, "_dynamic_pricing", {})


# ── the shadowing bug ───────────────────────────────────────────────────────


def test_a_mini_model_is_not_billed_at_the_full_models_rate() -> None:
    # "gpt-4o" precedes "gpt-4o-mini" in the table; a first-match-wins prefix
    # lookup bills the cheap model at ~17x its real rate.
    expected_in = _FALLBACK_PRICING["gpt-4o-mini"].input
    assert calc_cost("gpt-4o-mini", _1M, 0) == pytest.approx(expected_in)


def test_the_two_models_do_not_cost_the_same() -> None:
    assert calc_cost("gpt-4o-mini", _1M, 0) != calc_cost("gpt-4o", _1M, 0)


def test_output_tokens_use_the_matched_models_output_rate() -> None:
    expected_out = _FALLBACK_PRICING["gpt-4o-mini"].output
    assert calc_cost("gpt-4o-mini", 0, _1M) == pytest.approx(expected_out)


# ── prefix matching must still work ─────────────────────────────────────────


def test_a_dated_variant_still_inherits_its_family_rate() -> None:
    # The whole point of prefix matching: unknown dated snapshots must not
    # silently cost 0. Guards against over-correcting into exact-match-only.
    assert calc_cost("gpt-4o-2024-08-06", _1M, 0) == calc_cost("gpt-4o", _1M, 0)


def test_a_dated_mini_variant_matches_the_longer_key() -> None:
    assert calc_cost("gpt-4o-mini-2024-07-18", _1M, 0) == calc_cost("gpt-4o-mini", _1M, 0)


def test_an_unknown_model_family_costs_nothing() -> None:
    # Better a visible zero than a confidently wrong number.
    assert calc_cost("no-such-model-xyz", _1M, _1M) == 0.0


def test_a_local_model_is_free() -> None:
    assert calc_cost("ollama", _1M, _1M) == 0.0


# ── the live catalogue wins ─────────────────────────────────────────────────


def test_an_exact_live_price_beats_the_fallback_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_dynamic_pricing", {"gpt-4o-mini": Price(99.0, 99.0)})
    assert calc_cost("gpt-4o-mini", _1M, 0) == pytest.approx(99.0)


def test_the_fallback_is_used_when_the_catalogue_lacks_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pricing, "_dynamic_pricing", {"something-else": Price(99.0, 99.0)})
    expected_in = _FALLBACK_PRICING["gpt-4o-mini"].input
    assert calc_cost("gpt-4o-mini", _1M, 0) == pytest.approx(expected_in)


# ── the reporting helper must agree with the billing ────────────────────────


def test_pricing_info_reports_the_rate_actually_charged() -> None:
    # It exists for debugging cost problems, so disagreeing with calc_cost
    # would send someone hunting in the wrong place.
    info = pricing_info("gpt-4o-mini")
    assert info["input_per_1m"] == pytest.approx(calc_cost("gpt-4o-mini", _1M, 0))
    assert info["matched_prefix"] == "gpt-4o-mini"


def test_pricing_info_marks_a_live_price_as_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_dynamic_pricing", {"gpt-4o-mini": Price(1.0, 2.0)})
    assert pricing_info("gpt-4o-mini")["source"] == "live"


def test_pricing_info_admits_when_it_does_not_know() -> None:
    assert pricing_info("no-such-model-xyz")["source"] == "unknown"


# ── the property that stops this recurring ──────────────────────────────────


def test_no_table_entry_is_shadowed_by_a_shorter_key() -> None:
    """Adding a model whose name extends an existing key must not misprice it.

    This is the bug class, not the instance: any future `foo`/`foo-mini` pair
    reintroduces it unless the lookup prefers the longest match.
    """
    shadowed = [
        (long_key, short_key)
        for short_key in _FALLBACK_PRICING
        for long_key, price in _FALLBACK_PRICING.items()
        if long_key != short_key
        and long_key.startswith(short_key)
        and calc_cost(long_key, _1M, 0) != price.rates_for(_1M)[0]
    ]
    assert not shadowed, f"mispriced by a shorter prefix: {shadowed}"


# ── the Anthropic rows ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("claude-fable-5-2", (8.00, 40.00)),
        ("claude-mythos-5-2", (8.00, 40.00)),
        ("claude-opus-5-5", (4.00, 20.00)),
        ("claude-sonnet-5", (2.00, 10.00)),
        ("claude-sonnet-5-5", (2.00, 10.00)),
        ("claude-haiku-5-5", (0.10, 0.50)),
    ],
)
def test_each_current_claude_model_has_its_price(model: str, rates: tuple[float, float]) -> None:
    # A short prompt, under any long-prompt threshold.
    assert calc_cost(model, 1000, 0) == pytest.approx(1000 * rates[0] / _1M)
    assert calc_cost(model, 0, _1M) == pytest.approx(rates[1])


@pytest.mark.parametrize(
    "model", ["claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5"]
)
def test_the_older_fable_and_mythos_keep_their_price(model: str) -> None:
    # A row for 5.2 must not shadow 5.1 or 5.0, which are still $10 / $50.
    assert calc_cost(model, _1M, _1M) == pytest.approx(60.00)


def test_opus_5_keeps_its_price_beside_opus_5_5() -> None:
    assert calc_cost("claude-opus-5", _1M, _1M) == pytest.approx(30.00)


def test_a_dated_claude_id_inherits_its_familys_price() -> None:
    assert calc_cost("claude-opus-5-5-20261101", _1M, _1M) == calc_cost("claude-opus-5-5", _1M, _1M)


# ── a price by prompt length ────────────────────────────────────────────────


def test_haiku_5_5_at_the_threshold_pays_the_short_prompt_rate() -> None:
    # The page prices "prompts up to 100,000 tokens" at $0.10 / $0.50.
    assert calc_cost("claude-haiku-5-5", 100_000, 1000) == pytest.approx(
        (100_000 * 0.10 + 1000 * 0.50) / _1M
    )


def test_haiku_5_5_over_the_threshold_pays_the_long_prompt_rate_on_both_sides() -> None:
    assert calc_cost("claude-haiku-5-5", 100_001, 1000) == pytest.approx(
        (100_001 * 0.50 + 1000 * 2.50) / _1M
    )


@pytest.mark.parametrize(
    ("model", "short", "long"),
    [
        ("gemini-2.5-pro", (1.25, 10.00), (2.50, 15.00)),
        ("gemini-3.1-pro-preview", (2.00, 12.00), (4.00, 18.00)),
    ],
)
def test_a_gemini_pro_prompt_over_200k_tokens_pays_more(
    model: str, short: tuple[float, float], long: tuple[float, float]
) -> None:
    assert calc_cost(model, 200_000, _1M) == pytest.approx(200_000 * short[0] / _1M + short[1])
    assert calc_cost(model, 200_001, _1M) == pytest.approx(200_001 * long[0] / _1M + long[1])


def test_the_highest_tier_a_prompt_is_over_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Listed out of order, so the rule is the threshold, not the position.
    price = Price(1.0, 2.0, (Tier(256_000, 5.0, 6.0), Tier(32_000, 3.0, 4.0)))
    monkeypatch.setitem(_FALLBACK_PRICING, "tiered-model", price)
    assert calc_cost("tiered-model", 32_000, 0) == pytest.approx(32_000 * 1.0 / _1M)
    assert calc_cost("tiered-model", 32_001, 0) == pytest.approx(32_001 * 3.0 / _1M)
    assert calc_cost("tiered-model", 256_001, _1M) == pytest.approx(256_001 * 5.0 / _1M + 6.0)


def test_a_live_price_with_a_tier_is_charged_by_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pricing, "_dynamic_pricing", {"gpt-4o-mini": Price(1.0, 2.0, (Tier(1000, 3.0, 4.0),))}
    )
    assert calc_cost("gpt-4o-mini", 1001, _1M) == pytest.approx(1001 * 3.0 / _1M + 4.0)


def test_pricing_info_lists_a_fallback_prices_tiers() -> None:
    info = pricing_info("claude-haiku-5-5")
    assert info["source"] == "fallback"
    assert info["matched_prefix"] == "claude-haiku-5"
    assert (info["input_per_1m"], info["output_per_1m"]) == pytest.approx((0.10, 0.50))
    assert info["tiers"] == [
        {"above_tokens": 100_000, "input_per_1m": pytest.approx(0.50), "output_per_1m": 2.50}
    ]


def test_pricing_info_lists_a_live_prices_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pricing, "_dynamic_pricing", {"model-a": Price(1.0, 2.0, (Tier(200_000, 3.0, 4.0),))}
    )
    info = pricing_info("model-a")
    assert info["source"] == "live"
    assert info["tiers"] == [{"above_tokens": 200_000, "input_per_1m": 3.0, "output_per_1m": 4.0}]


def test_pricing_info_lists_no_tiers_for_a_single_price() -> None:
    assert pricing_info("gpt-4o-mini")["tiers"] == []
    assert pricing_info("no-such-model-xyz")["tiers"] == []
