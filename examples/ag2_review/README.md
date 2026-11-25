# An AG2 conversation as an agent

Two AG2 agents, a writer and a critic, improve a draft between them. Wactorz
runs that conversation for every draft that arrives on a topic, keeps it
supervised, gives it the model the system is configured with, and shows its
cost on the dashboard. This is the shape for anything built with AG2: the
agents stay as they are, Wactorz runs them. Written for AG2 1.x (`import ag2`).

## Files

| File | Role |
| ---- | ---- |
| `agent.py` | `make_agents()`: the writer and the critic. `review`: the conversation as an agent on `drafts/new`, publishing on `drafts/reviewed`. |
| `run.py` | Starts Wactorz with that agent and the model `LLM_PROVIDER` names. |
| `publish_draft.py` | Publishes a draft: the built-in one, or the text you pass. |

## Run it

```bash
pip install 'wactorz[ag2]'
cd examples/ag2_review
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... python run.py   # the system's model drives the agents
LLM_PROVIDER=openai OPENAI_API_KEY=... python run.py
LLM_PROVIDER=ollama python run.py                            # a local model, free
python run.py                                  # no model: canned replies, the draft comes back as it is
python publish_draft.py                        # in another terminal; or pass your own text
mosquitto_sub -t drafts/reviewed
```

AG2 talks to each provider through that provider's SDK, and asks for it
through an extra of its own, at a version floor of its own: install
`ag2[anthropic]`, `ag2[openai]` (also for Ollama and NIM, which speak the
OpenAI protocol) or `ag2[gemini]` for the provider you run. Without it the
agent answers with `AG2's openai client is not usable ... pip install
'ag2[openai]'` and nothing else happens.

The cost on the card is priced as Wactorz prices its own calls, from the
LiteLLM catalogue or Wactorz's built-in table, at each model's base rate:
AG2 reports a whole run's tokens, so no one request's prompt is known to
decide a long-prompt tier. For prices of your own, negotiated or
self-hosted, pass `prices={"gpt-5.4-mini": (0.75, 4.50)}` to `record_reply`:
dollars per million input and output tokens, by model family, which wins
for the models it names. A model priced nowhere is counted at no cost, with
its tokens still shown.

## How it works

```python
@wactorz.agent(
    name="ag2-review", subscribes="drafts/new", publishes="drafts/reviewed", concurrency=2
)
async def review(draft: dict, me: wactorz.FunctionAgent) -> dict | None:
    writer, critic = make_agents(model_config(me))  # AG2 agents on the system's model
    reply = await writer.ask(f"Improve this draft:\n\n{draft['text']}")
    verdict = await critic.ask(f"Review this draft:\n\n{reply.body}")
    while APPROVED not in verdict.body and turns < 2 * MAX_TURNS:
        reply = await reply.ask(f"A reviewer said:\n\n{verdict.body}\n\nRevise accordingly.")
        verdict = await verdict.ask(f"The revised draft:\n\n{reply.body}")
    cost = await record_reply(me, reply) + await record_reply(me, verdict)
    return {"id": draft["id"], "text": reply.body, "turns": turns, "cost_usd": cost}
```

- **`model_config(me)`**, from `wactorz.core.integrations.ag2`, turns the
  provider Wactorz runs on into AG2's configuration: Anthropic and Gemini
  through AG2's clients of the same name, OpenAI, NIM and Ollama through the
  OpenAI protocol, Ollama at no cost. Without a model the agents run on AG2's
  test client with canned replies, and the agent says so on the feed.
- **`ask` and `reply.ask`** are AG2's own: a fresh conversation, and a
  follow-up in the same one. Both agents keep their conversation for the
  whole review, so the writer sees its earlier versions and the critic
  whether its points were taken.
- **`record_reply`** reads AG2's usage report for a reply, the tokens by
  model, prices them, and reports them to the actor, which feeds
  the dashboard's counters and the system's cost limit. A reply's report
  covers the whole conversation behind it, which is why each agent's is
  recorded once, at the end.
- **`concurrency=2`** lets two drafts run at once; the agents are made fresh
  per draft, so one conversation never leaks into the next.
