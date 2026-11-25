# SPDX-License-Identifier: Apache-2.0
"""Turning a model's token counts into money, for libraries that report only tokens.

LangChain and AG2 say how many tokens a call used and which model answered;
neither says what it cost. A program may give a price table: dollars per
million input tokens and per million output tokens, by model name as the
library reports it. A model the table names is priced from it, so negotiated
or self-hosted prices win. Any other model is priced as Wactorz prices its own
calls (`wactorz.agents.llm.pricing`: the LiteLLM catalogue, else a built-in
table), and one Wactorz has no price for either counts its tokens at no cost,
so the call is still visible on the dashboard.

There are two ways to fall back, because a long-prompt tier is per request:
:func:`call_cost` prices one request, at the tier its prompt reaches, and
:func:`total_cost` prices tokens summed over many, at the model's base rate,
since a sum over a tier's threshold says nothing about any one request.
"""

from ...agents.llm.pricing import base_rates, calc_cost

#: Dollars per million input tokens and per million output tokens, by model name.
Prices = dict[str, tuple[float, float]]


def price_for(prices: Prices, model: str) -> tuple[float, float] | None:
    """The price listed for ``model``, by exact name or the longest listed prefix of it.

    A provider reports the model it resolved to, often with a date on the end
    (``gpt-4o-mini-2024-07-18`` for ``gpt-4o-mini``), so a table keyed by the
    family name still matches. The longest prefix wins, so ``gpt-4o-mini`` is
    not priced as ``gpt-4o``.
    """
    if model in prices:
        return prices[model]
    matches = [key for key in prices if key and model.startswith(key)]
    if not matches:
        return None
    return prices[max(matches, key=len)]


def _at(rates: tuple[float, float], input_tokens: int, output_tokens: int) -> float:
    per_input, per_output = rates
    return (input_tokens * per_input + output_tokens * per_output) / 1_000_000


def cost_of(prices: Prices, model: str, input_tokens: int, output_tokens: int) -> float:
    """What the tokens cost at the price ``prices`` lists for ``model``, or zero when it lists none."""
    price = price_for(prices, model)
    return _at(price, input_tokens, output_tokens) if price is not None else 0.0


def call_cost(prices: Prices, model: str, input_tokens: int, output_tokens: int) -> float:
    """What one request cost: from ``prices`` if it names ``model``, else at Wactorz's price.

    Wactorz's price includes a long-prompt tier, which ``input_tokens``, one
    request's prompt, decides.
    """
    price = price_for(prices, model)
    if price is not None:
        return _at(price, input_tokens, output_tokens)
    return calc_cost(model, input_tokens, output_tokens)


def total_cost(prices: Prices, model: str, input_tokens: int, output_tokens: int) -> float:
    """What tokens summed over many requests cost: from ``prices``, else at Wactorz's base rate."""
    price = price_for(prices, model) or base_rates(model)
    return _at(price, input_tokens, output_tokens) if price is not None else 0.0
