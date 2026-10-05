"""AG2 (AutoGen) beside Wactorz: an AG2 chat's spend on the dashboard.

AG2 agents keep their own usage ledger per model client. After a chat,
:func:`record_usage` reports what the chat added to the actor that ran it,
with :meth:`~wactorz.core.actor.Actor.record_llm_cost`::

    from wactorz.core.integrations.ag2 import record_usage

    result = await writer.a_initiate_chat(critic, message=text, max_turns=4)
    record_usage(me, [writer, critic])

AG2 prices the models it knows itself, so the cost is AG2's figure. Written
against AG2 0.x, whose package is ``autogen``.
"""

from typing import Any

# Optional dependency: `pip install 'wactorz[ag2]'`. Only the summary function
# is needed; the agents are the caller's.
from autogen import gather_usage_summary

#: Where the running total since the last report is kept on the actor.
_RECORDED = "_ag2_usage_recorded"


def usage_totals(agents: list[Any]) -> tuple[float, int, int]:
    """Cost, input tokens and output tokens across ``agents``, since their creation."""
    summary = gather_usage_summary(agents).get("usage_including_cached_inference") or {}
    cost = float(summary.get("total_cost", 0.0) or 0.0)
    input_tokens = output_tokens = 0
    for model, usage in summary.items():
        if model == "total_cost" or not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("prompt_tokens", 0) or 0)
        output_tokens += int(usage.get("completion_tokens", 0) or 0)
    return cost, input_tokens, output_tokens


def record_usage(actor: Any, agents: list[Any], *, model: str = "") -> float:
    """Report to ``actor`` what ``agents`` have spent since the last report. Returns that cost.

    AG2's totals only grow, so the difference from the last call is what one
    chat cost; the first call reports everything so far.
    """
    cost, input_tokens, output_tokens = usage_totals(agents)
    last_cost, last_in, last_out = getattr(actor, _RECORDED, (0.0, 0, 0))
    delta_cost = max(0.0, cost - last_cost)
    delta_in = max(0, input_tokens - last_in)
    delta_out = max(0, output_tokens - last_out)
    setattr(actor, _RECORDED, (cost, input_tokens, output_tokens))
    if delta_cost or delta_in or delta_out:
        actor.record_llm_cost(
            delta_cost, input_tokens=delta_in, output_tokens=delta_out, model=model, provider="ag2"
        )
    return delta_cost
