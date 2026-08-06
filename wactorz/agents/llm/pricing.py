"""Model pricing: the catalogue fetch, the fallback table, and cost arithmetic.

The fallback is used when the fetch fails or a model is absent upstream. Prefix
matching is deliberate — see ``_fallback_pricing_key``.
"""

import logging
import time

import aiohttp

logger = logging.getLogger(__name__)


# Fallback pricing per 1M tokens (input, output) in USD.
# Used when the dynamic fetch fails or the model isn't in the LiteLLM catalogue.
# Supports prefix matching so "gpt-5" covers "gpt-5-mini", etc.
_FALLBACK_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # OpenAI
    "gpt-5.4-pro": (30.00, 180.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.2-pro": (21.00, 168.00),
    "gpt-5.2-chat-latest": (1.75, 14.00),
    "gpt-5.2-codex": (1.75, 14.00),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1-codex-max": (1.25, 10.00),
    "gpt-5.1-codex-mini": (0.275, 2.20),
    "gpt-5.1-chat-latest": (1.25, 10.00),
    "gpt-5.1-codex": (1.25, 10.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5-search-api": (1.25, 10.00),
    "gpt-5-pro": (15.00, 120.00),
    "gpt-5-chat-latest": (1.25, 10.00),
    "gpt-5-codex": (1.25, 10.00),
    "gpt-5-mini": (0.275, 2.20),
    "gpt-5-nano": (0.055, 0.44),
    "gpt-5": (1.25, 10.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    # Ollama — local, no cost
    "ollama": (0.00, 0.00),
    # NVIDIA NIM — free tier covers most usage; paid tier pricing varies by model
    "nim/": (0.00, 0.00),
    # Google Gemini
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-pro": (2.00, 12.00),
    "gemini-": (0.30, 2.50),
}

_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
_PRICING_TTL = 86400.0  # 24 hours

# Populated by refresh_pricing(); maps exact model name → ($/1M input, $/1M output)
_dynamic_pricing: dict[str, tuple[float, float]] = {}
_dynamic_pricing_ts: float = 0.0
_pricing_fetch_in_progress: bool = False


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
        pricing: dict[str, tuple[float, float]] = {}
        for model, info in data.items():
            if not isinstance(info, dict):
                continue
            inp = info.get("input_cost_per_token")
            out = info.get("output_cost_per_token")
            if inp is not None and out is not None:
                # Store as $/1M tokens to match _FALLBACK_PRICING units
                pricing[model] = (float(inp) * 1_000_000, float(out) * 1_000_000)
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


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    # Exact match against live catalogue
    if model in _dynamic_pricing:
        price_in, price_out = _dynamic_pricing[model]
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    # Prefix match against local fallback table
    key = _fallback_pricing_key(model)
    if not key:
        return 0.0
    price_in, price_out = _FALLBACK_PRICING[key]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def pricing_info(model: str) -> dict[str, object]:
    """Return pricing source and rates for a model — useful for debugging."""
    if model in _dynamic_pricing:
        inp, out = _dynamic_pricing[model]
        age = time.time() - _dynamic_pricing_ts
        return {
            "source": "live",
            "input_per_1m": inp,
            "output_per_1m": out,
            "cache_age_s": round(age),
        }
    key = _fallback_pricing_key(model)
    if key:
        inp, out = _FALLBACK_PRICING[key]
        return {
            "source": "fallback",
            "input_per_1m": inp,
            "output_per_1m": out,
            "matched_prefix": key,
        }
    return {"source": "unknown", "input_per_1m": 0.0, "output_per_1m": 0.0}
