"""LangChain and LangGraph beside Wactorz: the spend of a chain on the dashboard.

A chain or a graph calls its model through LangChain, not through the
system's providers, so the system would not see what it costs. The callback
handler here reports each model call to the actor that runs the chain, with
:meth:`~wactorz.core.actor.Actor.record_llm_cost`::

    from wactorz.core.integrations.langchain import CostCallback

    result = await graph.ainvoke(state, config={"callbacks": [CostCallback(me)]})

LangChain reports tokens, not money. Give ``prices`` to turn them into a cost:
``{"gpt-4o-mini": (0.15, 0.60)}`` is dollars per million input and output
tokens for that model name, as LangChain reports it. Without a price the
tokens are still counted and the cost is zero.
"""

from typing import Any

# Optional dependency: `pip install 'wactorz[langgraph]'`. The handler must
# subclass LangChain's base to be accepted, so this module needs it.
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

#: Dollars per million input tokens and per million output tokens, by model name.
Prices = dict[str, tuple[float, float]]


def token_usage(response: LLMResult) -> tuple[int, int, str]:
    """Input tokens, output tokens and the model name a result carries.

    Chat models put the usage on the message; older paths put it in
    ``llm_output``. Both are read, the message first.
    """
    model = str((response.llm_output or {}).get("model_name") or "")
    input_tokens = output_tokens = 0
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
            input_tokens += int(usage.get("input_tokens", 0) or 0)
            output_tokens += int(usage.get("output_tokens", 0) or 0)
            if not model and message is not None:
                model = str(
                    (getattr(message, "response_metadata", None) or {}).get("model_name") or ""
                )
    if not input_tokens and not output_tokens:
        usage = (response.llm_output or {}).get("token_usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)
    return input_tokens, output_tokens, model


def cost_of(prices: Prices, model: str, input_tokens: int, output_tokens: int) -> float:
    """What the tokens cost at the price listed for ``model``, or zero when none is."""
    price = prices.get(model)
    if price is None:
        return 0.0
    per_input, per_output = price
    return (input_tokens * per_input + output_tokens * per_output) / 1_000_000


class CostCallback(BaseCallbackHandler):
    """Reports every model call of a chain to ``actor``; see the module docstring."""

    def __init__(self, actor: Any, prices: Prices | None = None) -> None:
        super().__init__()
        self.actor = actor
        self.prices: Prices = dict(prices or {})

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        input_tokens, output_tokens, model = token_usage(response)
        self.actor.record_llm_cost(
            cost_of(self.prices, model, input_tokens, output_tokens),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="langchain",
        )
