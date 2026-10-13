"""A LangGraph graph as a Wactorz agent.

LangGraph decides how a ticket is handled: classify it, set a priority, draft
a reply. Wactorz keeps the graph running against a topic, restarts it if it
crashes, shows it on the dashboard, and lets the planner and the rules wire
it to other agents. The graph runs on the system's model when there is one,
through `me.llm`, and falls back to keyword rules without.

A LangChain chat model can be used instead, and its spend still reaches the
dashboard: pass the `CostCallback` from `wactorz.core.integrations.langchain` in the
graph's config, as `_run_graph` does.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import wactorz
from wactorz.core.integrations.langchain import CostCallback

#: Dollars per million input and output tokens, for the LangChain models the
#: callback may see. Extend for the models you use.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}

CATEGORIES = ("outage", "billing", "feature", "question")

#: Keywords that decide a category without a model.
RULES = {
    "outage": ("down", "stopped", "outage", "crash", "not working", "error"),
    "billing": ("invoice", "charge", "refund", "payment", "price"),
    "feature": ("could you add", "feature", "would be nice", "support for"),
}


class Triage(TypedDict, total=False):
    ticket: dict[str, Any]
    category: str
    priority: str
    reply: str


def classify_by_rules(text: str) -> str:
    lowered = text.lower()
    for category, words in RULES.items():
        if any(word in lowered for word in words):
            return category
    return "question"


def prioritise(state: Triage) -> Triage:
    """Outages first; everything else by what the customer said."""
    text = str(state["ticket"].get("text", "")).lower()
    if state.get("category") == "outage" or "urgent" in text:
        return {"priority": "high"}
    if state.get("category") == "billing":
        return {"priority": "medium"}
    return {"priority": "low"}


def build_graph(llm: Any = None) -> Any:
    """The compiled graph: classify → prioritise → draft.

    ``llm`` is the system's provider, when the agent has one; the model-backed
    nodes use it and the rule-backed ones take over without it.
    """

    async def classify(state: Triage) -> Triage:
        text = str(state["ticket"].get("text", ""))
        if llm is None:
            return {"category": classify_by_rules(text)}
        answer, _usage = await llm.complete(
            [{"role": "user", "content": text}],
            system=f"Classify the support ticket as one of {', '.join(CATEGORIES)}. "
            "Answer with the category alone.",
        )
        category = answer.strip().lower().strip(".")
        return {"category": category if category in CATEGORIES else classify_by_rules(text)}

    async def draft(state: Triage) -> Triage:
        ticket = state["ticket"]
        if llm is None:
            return {
                "reply": f"Thanks for reporting this ({state['category']}, {state['priority']} "
                "priority). We are on it and will get back to you shortly."
            }
        answer, _usage = await llm.complete(
            [{"role": "user", "content": str(ticket.get("text", ""))}],
            system="Draft a two-sentence reply to this support ticket. Be concrete and kind.",
        )
        return {"reply": answer.strip()}

    graph = StateGraph(Triage)
    graph.add_node("classify", classify)
    graph.add_node("prioritise", prioritise)
    graph.add_node("draft", draft)
    graph.add_edge(START, "classify")
    graph.add_edge("classify", "prioritise")
    graph.add_edge("prioritise", "draft")
    graph.add_edge("draft", END)
    return graph.compile()


@wactorz.agent(
    name="ticket-triage",
    subscribes="tickets/new",
    publishes="tickets/triaged",
    description="Triages support tickets with a LangGraph graph: category, priority, draft reply.",
    capabilities=["triage", "langgraph"],
    input_schema={"id": "str", "text": "str"},
    output_schema={"id": "str", "category": "str", "priority": "str", "reply": "str"},
    requires={"packages": ["langgraph"]},
    # A model call per node: let a few tickets be in flight rather than queue.
    concurrency=4,
)
async def triage(ticket: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Run one ticket through the graph; the graph is built once and kept."""
    if not ticket.get("text"):
        return None
    graph = me.options.get("_graph")
    if graph is None:
        graph = build_graph(me.llm)
        me.options["_graph"] = graph
    result = await _run_graph(graph, {"ticket": ticket}, me)
    me.persist("tickets_total", int(me.recall("tickets_total", 0)) + 1)
    return {
        "id": ticket.get("id"),
        "category": result["category"],
        "priority": result["priority"],
        "reply": result["reply"],
    }


async def _run_graph(graph: Any, state: Triage, me: wactorz.FunctionAgent) -> Triage:
    """Invoke with the cost callback, so a LangChain model inside is accounted for."""
    return await graph.ainvoke(state, config={"callbacks": [CostCallback(me, PRICES)]})
