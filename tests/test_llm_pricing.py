"""Model pricing lookup.

Costs are computed from a table keyed by model-name *prefix*, so a dated or
suffixed variant (`gpt-4o-2024-08-06`) inherits its family's rate. The hazard is
that a shorter key can shadow a longer one: matching `gpt-4o` first means
`gpt-4o-mini` is billed at the full model's rate.

That is not cosmetic — the same number drives the reported spend, the budget
check and the spend cap, so a shadowed model silently overstates cost by the
ratio between the two rates.
"""

# pylint: disable=missing-function-docstring

import pytest

from wactorz.agents import llm_agent
from wactorz.agents.llm_agent import _FALLBACK_PRICING, _calc_cost, pricing_info

_1M = 1_000_000


@pytest.fixture(name="no_live_catalogue", autouse=True)
def no_live_catalogue_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the fallback path — the live catalogue is empty until fetched."""
    monkeypatch.setattr(llm_agent, "_dynamic_pricing", {})


# ── the shadowing bug ───────────────────────────────────────────────────────


def test_a_mini_model_is_not_billed_at_the_full_models_rate() -> None:
    # "gpt-4o" precedes "gpt-4o-mini" in the table; a first-match-wins prefix
    # lookup bills the cheap model at ~17x its real rate.
    expected_in, _ = _FALLBACK_PRICING["gpt-4o-mini"]
    assert _calc_cost("gpt-4o-mini", _1M, 0) == pytest.approx(expected_in)


def test_the_two_models_do_not_cost_the_same() -> None:
    assert _calc_cost("gpt-4o-mini", _1M, 0) != _calc_cost("gpt-4o", _1M, 0)


def test_output_tokens_use_the_matched_models_output_rate() -> None:
    _, expected_out = _FALLBACK_PRICING["gpt-4o-mini"]
    assert _calc_cost("gpt-4o-mini", 0, _1M) == pytest.approx(expected_out)


# ── prefix matching must still work ─────────────────────────────────────────


def test_a_dated_variant_still_inherits_its_family_rate() -> None:
    # The whole point of prefix matching: unknown dated snapshots must not
    # silently cost 0. Guards against over-correcting into exact-match-only.
    assert _calc_cost("gpt-4o-2024-08-06", _1M, 0) == _calc_cost("gpt-4o", _1M, 0)


def test_a_dated_mini_variant_matches_the_longer_key() -> None:
    assert _calc_cost("gpt-4o-mini-2024-07-18", _1M, 0) == _calc_cost("gpt-4o-mini", _1M, 0)


def test_an_unknown_model_family_costs_nothing() -> None:
    # Better a visible zero than a confidently wrong number.
    assert _calc_cost("no-such-model-xyz", _1M, _1M) == 0.0


def test_a_local_model_is_free() -> None:
    assert _calc_cost("ollama", _1M, _1M) == 0.0


# ── the live catalogue wins ─────────────────────────────────────────────────


def test_an_exact_live_price_beats_the_fallback_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_agent, "_dynamic_pricing", {"gpt-4o-mini": (99.0, 99.0)})
    assert _calc_cost("gpt-4o-mini", _1M, 0) == pytest.approx(99.0)


def test_the_fallback_is_used_when_the_catalogue_lacks_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_agent, "_dynamic_pricing", {"something-else": (99.0, 99.0)})
    expected_in, _ = _FALLBACK_PRICING["gpt-4o-mini"]
    assert _calc_cost("gpt-4o-mini", _1M, 0) == pytest.approx(expected_in)


# ── the reporting helper must agree with the billing ────────────────────────


def test_pricing_info_reports_the_rate_actually_charged() -> None:
    # It exists for debugging cost problems, so disagreeing with _calc_cost
    # would send someone hunting in the wrong place.
    info = pricing_info("gpt-4o-mini")
    assert info["input_per_1m"] == pytest.approx(_calc_cost("gpt-4o-mini", _1M, 0))
    assert info["matched_prefix"] == "gpt-4o-mini"


def test_pricing_info_marks_a_live_price_as_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_agent, "_dynamic_pricing", {"gpt-4o-mini": (1.0, 2.0)})
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
        for long_key, (expected_in, _) in _FALLBACK_PRICING.items()
        if long_key != short_key
        and long_key.startswith(short_key)
        and _calc_cost(long_key, _1M, 0) != expected_in
    ]
    assert not shadowed, f"mispriced by a shorter prefix: {shadowed}"
