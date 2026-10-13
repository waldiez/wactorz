"""Turning a model's token counts into money, for libraries that report only tokens.

LangChain and AG2 say how many tokens a call used and which model answered;
neither says what it cost. A price table does: dollars per million input
tokens and per million output tokens, by model name as the library reports
it. A model with no price counts its tokens at no cost, so an unpriced call
is still visible on the dashboard.
"""

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


def cost_of(prices: Prices, model: str, input_tokens: int, output_tokens: int) -> float:
    """What the tokens cost at the price listed for ``model``, or zero when none is."""
    price = price_for(prices, model)
    if price is None:
        return 0.0
    per_input, per_output = price
    return (input_tokens * per_input + output_tokens * per_output) / 1_000_000
