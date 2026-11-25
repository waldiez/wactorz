# SPDX-License-Identifier: Apache-2.0
"""Model pricing: the catalogue fetch, the fallback table, and cost arithmetic.

The fallback is used when the fetch fails or a model is absent upstream. Prefix
matching is deliberate — see ``_fallback_pricing_key``. A model that charges more
for a long prompt carries its tiers (``Tier``), in the catalogue and the table
alike, and a request is priced at the tier its prompt reaches.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

logger: logging.Logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier:
    """The rates, in dollars per 1M tokens, for a request whose prompt is over ``above`` tokens.

    Some models charge more for a long prompt, on its input and output alike, for the whole
    request rather than for the tokens past the threshold.
    """

    above: int
    input: float
    output: float


@dataclass(frozen=True)
class Price:
    """A model's rates in dollars per 1M tokens, with any long-prompt tiers."""

    input: float
    output: float
    tiers: tuple[Tier, ...] = ()

    def rates_for(self, input_tokens: int) -> tuple[float, float]:
        """The (input, output) rates a request with ``input_tokens`` of prompt pays.

        The highest tier the prompt is over wins; a prompt at a threshold exactly is not over it.
        """
        over = [tier for tier in self.tiers if input_tokens > tier.above]
        if not over:
            return self.input, self.output
        tier = max(over, key=lambda t: t.above)
        return tier.input, tier.output


# Fallback pricing per 1M tokens in USD.
# Used when the dynamic fetch fails or the model isn't in the LiteLLM catalogue.
# Supports prefix matching so "gpt-5" covers "gpt-5-mini", etc.
_FALLBACK_PRICING: dict[str, Price] = {
    # Anthropic
    "claude-fable-5-2": Price(8.00, 40.00),
    "claude-fable-5": Price(10.00, 50.00),
    "claude-mythos-5-2": Price(8.00, 40.00),
    "claude-mythos-5": Price(10.00, 50.00),
    "claude-opus-5-5": Price(4.00, 20.00),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-opus-4-7": Price(5.00, 25.00),
    "claude-opus-4-6": Price(5.00, 25.00),
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-sonnet-4-6": Price(3.00, 15.00),
    "claude-haiku-5": Price(0.10, 0.50, (Tier(100_000, 0.50, 2.50),)),
    "claude-haiku-4-5": Price(1.00, 5.00),
    # OpenAI. A prompt over 272K tokens pays the long-context price for the
    # whole request.
    "gpt-6.2-astra": Price(8.00, 40.00, (Tier(272_000, 16.00, 60.00),)),
    "gpt-6-astra": Price(10.00, 50.00, (Tier(272_000, 20.00, 75.00),)),
    "gpt-6.2-sol": Price(2.00, 10.00, (Tier(272_000, 4.00, 15.00),)),
    "gpt-6.1-sol": Price(2.00, 10.00, (Tier(272_000, 4.00, 15.00),)),
    "gpt-6-sol": Price(2.00, 10.00, (Tier(272_000, 4.00, 15.00),)),
    "gpt-6.1-terra": Price(1.00, 5.00, (Tier(272_000, 2.00, 7.50),)),
    "gpt-6-luna": Price(0.10, 0.50, (Tier(272_000, 0.20, 0.75),)),
    # 5.6 Sol's price is promotional until 2026-11-30.
    "gpt-5.6-sol": Price(4.00, 20.00, (Tier(272_000, 8.00, 30.00),)),
    "gpt-5.6-terra": Price(2.00, 12.00, (Tier(272_000, 4.00, 18.00),)),
    "gpt-5.6-luna": Price(0.20, 1.20, (Tier(272_000, 0.40, 1.80),)),
    "gpt-5.5-pro": Price(30.00, 180.00, (Tier(272_000, 60.00, 270.00),)),
    "gpt-5.5": Price(5.00, 30.00, (Tier(272_000, 10.00, 45.00),)),
    "gpt-5.4-pro": Price(30.00, 180.00, (Tier(272_000, 60.00, 270.00),)),
    "gpt-5.4-mini": Price(0.75, 4.50),
    "gpt-5.4-nano": Price(0.20, 1.25),
    "gpt-5.4": Price(2.50, 15.00, (Tier(272_000, 5.00, 22.50),)),
    "gpt-5.3-codex": Price(1.75, 14.00),
    "gpt-5.2-pro": Price(21.00, 168.00),
    "gpt-5.2": Price(1.75, 14.00),
    "gpt-5.1": Price(1.25, 10.00),
    "gpt-5-search-api": Price(1.25, 10.00),
    "chat-latest": Price(5.00, 30.00),
    # OpenAI shuts down gpt-5, gpt-5-mini, gpt-5-nano and gpt-5-pro on
    # 2026-12-11; requests until then are billed at these prices.
    "gpt-5-pro": Price(15.00, 120.00),
    "gpt-5-mini": Price(0.25, 2.00),
    "gpt-5-nano": Price(0.05, 0.40),
    "gpt-5": Price(1.25, 10.00),
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4.1": Price(2.00, 8.00),
    "gpt-4o": Price(2.50, 10.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    # Ollama — local, no cost
    "ollama": Price(0.00, 0.00),
    # NVIDIA NIM — free tier covers most usage; paid tier pricing varies by model
    "nim/": Price(0.00, 0.00),
    # Google Gemini
    # Google's price for 3.6 and 3.8 Flash through 2026-12-31; it doubles to
    # $1.50 / $7.50 from 2027-01-01.
    "gemini-3.8-flash": Price(0.75, 3.75),
    "gemini-3.6-flash": Price(0.75, 3.75),
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    "gemini-3.1-flash-lite": Price(0.25, 1.50),
    "gemini-3-flash-preview": Price(0.50, 3.00),
    "gemini-3.1-pro-preview": Price(2.00, 12.00, (Tier(200_000, 4.00, 18.00),)),
    "gemini-2.5-flash-lite": Price(0.10, 0.40),
    # Google serves requests for 2.5 Flash and 2.5 Pro with 3.5 Flash-Lite and
    # 3.8 Flash from 2026-12-01, and bills them at those models' prices.
    "gemini-2.5-flash": Price(0.30, 2.50),
    "gemini-2.5-pro": Price(1.25, 10.00, (Tier(200_000, 2.50, 15.00),)),
    "gemini-": Price(0.30, 2.50),
}

_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
_PRICING_TTL = 86400.0  # 24 hours

# Populated by refresh_pricing(); maps exact model name → its price
_dynamic_pricing: dict[str, Price] = {}
_dynamic_pricing_ts: float = 0.0
_pricing_fetch_in_progress: bool = False

# A catalogue field for the input rate of a long prompt, as in
# `input_cost_per_token_above_200k_tokens`. Its variants for other service
# modes (`..._batches`, `..._flex`, `..._priority`) do not match: Wactorz sends
# standard requests.
_TIER_FIELD = re.compile(r"input_cost_per_token_above_(\d+)k_tokens")

_PER_MILLION = 1_000_000


def _catalogue_tiers(info: dict[str, Any]) -> tuple[Tier, ...]:
    """The long-prompt tiers of one catalogue entry, lowest threshold first.

    A tier needs both its input and its output rate; one with only one of them
    is left out, as an entry without both base rates is.
    """
    tiers: list[Tier] = []
    for field, inp in info.items():
        found = _TIER_FIELD.fullmatch(field)
        if not found or inp is None:
            continue
        thousands = found.group(1)
        out = info.get(f"output_cost_per_token_above_{thousands}k_tokens")
        if out is None:
            continue
        tiers.append(
            Tier(int(thousands) * 1000, float(inp) * _PER_MILLION, float(out) * _PER_MILLION)
        )
    return tuple(sorted(tiers, key=lambda tier: tier.above))


def _catalogue_price(info: object) -> Price | None:
    """One catalogue entry as a price per 1M tokens, or None when it has no complete price."""
    if not isinstance(info, dict):
        return None
    inp = info.get("input_cost_per_token")
    out = info.get("output_cost_per_token")
    if inp is None or out is None:
        return None
    return Price(float(inp) * _PER_MILLION, float(out) * _PER_MILLION, _catalogue_tiers(info))


async def refresh_pricing() -> None:
    """Fetch latest model prices from LiteLLM's catalogue and cache them for 24 h."""
    global _dynamic_pricing, _dynamic_pricing_ts, _pricing_fetch_in_progress
    if time.time() - _dynamic_pricing_ts < _PRICING_TTL and _dynamic_pricing:
        return
    if _pricing_fetch_in_progress:
        return
    _pricing_fetch_in_progress = True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _LITELLM_PRICING_URL,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
        pricing: dict[str, Price] = {}
        for model, info in data.items():
            price = _catalogue_price(info)
            if price is not None:
                pricing[model] = price
        _dynamic_pricing = pricing
        _dynamic_pricing_ts = time.time()
        logger.info("[pricing] Fetched %d model prices from LiteLLM", len(pricing))
    except Exception as exc:
        logger.warning("[pricing] Failed to fetch dynamic pricing: %s - using fallback", exc)
    finally:
        _pricing_fetch_in_progress = False


def _fallback_pricing_key(model: str) -> str | None:
    """Longest table prefix matching `model`, or None.

    Longest wins because a shorter key would otherwise shadow a longer one:
    `gpt-4o` matches `gpt-4o-mini` and would bill the cheap model at the full
    model's rate. Prefix matching itself is deliberate — it lets dated variants
    like `gpt-4o-2024-08-06` inherit their family's price instead of costing 0.
    """
    candidates = [k for k in _FALLBACK_PRICING if model.startswith(k)]
    return max(candidates, key=len) if candidates else None


def _price_of(model: str) -> Price | None:
    """The live catalogue's price for exactly ``model``, else the table's by prefix, else None."""
    if model in _dynamic_pricing:
        return _dynamic_pricing[model]
    key = _fallback_pricing_key(model)
    return _FALLBACK_PRICING[key] if key else None


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """What ``input_tokens`` and ``output_tokens`` of ``model`` cost, in dollars.

    Priced from the live catalogue by exact name, else from the built-in table by prefix; 0 for a
    model in neither. A model that charges more for a long prompt is priced at the tier
    ``input_tokens`` reaches, on its input and output alike.
    """
    price = _price_of(model)
    if price is None:
        return 0.0
    price_in, price_out = price.rates_for(input_tokens)
    return (input_tokens * price_in + output_tokens * price_out) / _PER_MILLION


def base_rates(model: str) -> tuple[float, float] | None:
    """A short prompt's (input, output) rates for ``model``, in dollars per 1M tokens; None if unpriced.

    For tokens that are a sum over many requests, where no one request's prompt is known: a
    long-prompt tier is per request, and a sum over it says nothing about any one of them.
    """
    price = _price_of(model)
    return (price.input, price.output) if price is not None else None


def _tiers_info(price: Price) -> list[dict[str, float]]:
    return [
        {"above_tokens": tier.above, "input_per_1m": tier.input, "output_per_1m": tier.output}
        for tier in price.tiers
    ]


def pricing_info(model: str) -> dict[str, object]:
    """Return pricing source and rates for a model — useful for debugging.

    The rates are a short prompt's; ``tiers`` lists what a longer one pays, empty for a model
    with one price.
    """
    price = _price_of(model)
    if price is None:
        return {"source": "unknown", "input_per_1m": 0.0, "output_per_1m": 0.0, "tiers": []}
    info: dict[str, object] = {
        "source": "live" if model in _dynamic_pricing else "fallback",
        "input_per_1m": price.input,
        "output_per_1m": price.output,
        "tiers": _tiers_info(price),
    }
    if model in _dynamic_pricing:
        info["cache_age_s"] = round(time.time() - _dynamic_pricing_ts)
    else:
        info["matched_prefix"] = _fallback_pricing_key(model)
    return info
