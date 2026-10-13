# A LangGraph graph as an agent

LangGraph decides how each support ticket is handled; Wactorz keeps that
decision running against a topic, supervised, on the dashboard, and wired to
whatever acts on the result. The two do different jobs: a graph is something
you call, an agent is something that stays up.

## Files

| File | Role |
| ---- | ---- |
| `agent.py` | `build_graph()`: classify → prioritise → draft reply. `triage`: the graph as an agent on `tickets/new`, publishing on `tickets/triaged`, four tickets in flight at once. |
| `run.py` | Starts Wactorz with that agent. |
| `publish_ticket.py` | Publishes a few tickets. |

## Run it

```bash
pip install 'wactorz[langgraph]'
cd examples/langgraph_triage
python run.py                                 # keyword rules, no model
LLM_PROVIDER=ollama python run.py             # the system's model reads the tickets
python publish_ticket.py                      # in another terminal
mosquitto_sub -t tickets/triaged
```

## How it works

```python
@wactorz.agent(
    name="ticket-triage",
    subscribes="tickets/new",
    publishes="tickets/triaged",
    requires={"packages": ["langgraph"]},
    concurrency=4,
)
async def triage(ticket: dict, me: wactorz.FunctionAgent) -> dict | None:
    graph = me.options.get("_graph") or build_graph(me.llm)  # compiled once, kept on the actor
    me.options["_graph"] = graph
    result = await graph.ainvoke(
        {"ticket": ticket}, config={"callbacks": [CostCallback(me, PRICES)]}
    )
    return {"id": ticket["id"], **result}
```

Three things to notice:

- **The graph uses the system's model.** Its model-backed nodes call `me.llm`,
  the provider Wactorz is configured with, so every call lands on the agent's
  card, tokens and cost, and counts against the cost limit, with nothing to
  write in the nodes. Ollama reports a cost of zero; a hosted provider its
  real one. Without a model the rule-backed nodes take over.
- **`concurrency=4`.** A graph with model calls waits more than it computes.
  By default an agent handles one message of a topic at a time, in order; with
  `concurrency` set, up to four tickets run at once and order is not kept,
  which is right for tickets and wrong for a sensor stream.
- **`CostCallback`.** If a node uses a LangChain chat model instead of
  `me.llm`, the callback reports its tokens and, for models listed in
  `PRICES`, their cost, so the agent's card shows the spend either way.

The graph's checkpointer, if you add one, can keep its SQLite file in
`me.state_dir`, the agent's own directory, which survives restarts.
