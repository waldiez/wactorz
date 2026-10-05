# An AG2 conversation as an agent

Two AG2 (AutoGen) agents, a writer and a critic, improve a draft between
them. Wactorz runs that conversation for every draft that arrives on a topic,
keeps it supervised, and shows its cost on the dashboard. This is the shape for
anything built with AG2, a Waldiez flow included: the conversation stays as it
is, Wactorz runs it.

## Files

| File | Role |
| ---- | ---- |
| `agent.py` | `make_agents()`: the writer and the critic. `review`: the conversation as an agent on `drafts/new`, publishing on `drafts/reviewed`. |
| `run.py` | Starts Wactorz with that agent. |
| `publish_draft.py` | Publishes a draft: the built-in one, or the text you pass. |

## Run it

```bash
pip install 'wactorz[ag2]'
cd examples/ag2_review
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... python run.py   # the system's model drives the chat
LLM_PROVIDER=ollama python run.py                            # or a local one, free
python run.py                                  # no model: the agents exchange default replies
python publish_draft.py                        # in another terminal; or pass your own text
mosquitto_sub -t drafts/reviewed
```

## How it works

```python
@wactorz.agent(
    name="ag2-review", subscribes="drafts/new", publishes="drafts/reviewed", concurrency=2
)
async def review(draft: dict, me: wactorz.FunctionAgent) -> dict | None:
    writer, critic = make_agents(llm_config(me))  # AG2's config from the system's model
    result = await writer.a_initiate_chat(critic, message=draft["text"], max_turns=3, silent=True)
    cost = record_usage(me, [writer, critic])  # the chat's spend, on the agent's card
    return {"id": draft["id"], "text": last_writer_message(result), "cost_usd": cost}
```

`llm_config(me)` turns the provider Wactorz runs on into AG2's configuration:
Anthropic and Gemini through AG2's clients of the same name (their SDKs, which
Wactorz's own extras already install), OpenAI, NIM and Ollama through the
OpenAI-compatible protocol, Ollama at no cost. Without a model the agent says
so on the feed and the draft comes back as it is.

`a_initiate_chat` is AG2's own async entry point, so the conversation runs on
the event loop without blocking the other agents. `record_usage` from
`wactorz.core.integrations.ag2` reads AG2's usage summary and reports the difference since
the last report to the actor, which feeds the dashboard's counters and the
system's cost limit. The agents are made fresh per draft, so one conversation
never leaks into the next; `concurrency=2` lets two drafts run at once.

Written for AG2 0.x (`import autogen`). AG2 1.x is a different package layout
and is not covered here.
