# A language-model agent

Notes arrive on MQTT. An agent asks the model for a one-line summary of each and
publishes it. This shows how a function declared with `@wactorz.agent` reaches
the model the system runs on, and what that costs.

## Files

| File | Role |
| ---- | ---- |
| `agent.py` | The agent: an `async` function subscribed to `notes/raw/#`, publishing on `notes/summary`, calling `me.llm`. |
| `run.py` | Starts Wactorz with that agent and the model named by `LLM_PROVIDER`, in the minimal profile. |
| `publish_note.py` | Publishes a few notes, one of them as plain text. |

## Run it

You need a broker on `localhost:1883` (`docker compose up mosquitto` from the
repository root works), the package installed (`pip install wactorz`), and a
model. The quickest is Ollama with a small model pulled; a hosted provider needs
its extra and its key:

```bash
cd examples/llm_notes
python run.py                                   # Ollama on localhost
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... python run.py   # pip install 'wactorz[anthropic]'
LLM_PROVIDER=fake python run.py                 # no model: a canned answer, for a dry run
python publish_note.py                          # in another terminal
mosquitto_sub -t 'notes/summary'
```

The feed shows one line per summary. Every call through `me.llm` is counted on
the agent's card, tokens and cost, and against the system's cost limit, with
nothing to write in the function; Ollama reports a cost of zero, a hosted
provider its real one.

## How it works

```python
@wactorz.agent(name="note-summariser", subscribes="notes/raw/#", publishes="notes/summary")
async def summarise(note: dict, me: wactorz.FunctionAgent) -> dict | None:
    text = str(note.get("text") or note.get("raw") or "").strip()
    if not text or me.llm is None:
        return None
    summary, usage = await me.llm.complete(
        [{"role": "user", "content": text}], system=SYSTEM_PROMPT
    )
    return {"summary": summary.strip(), "cost_usd": usage["cost_usd"]}
```

`me.llm` is the system's provider: the one `LLM_PROVIDER` or `run(llm=...)`
names, routed through `LLM_OVERRIDES` like every other call site (set
`LLM_OVERRIDES=dynamic=ollama:llama3` to give decorated agents a cheaper model
than the orchestrator), and subject to the same daily cost limit, so a runaway
topic cannot run up a bill. `complete()` returns the text and a usage dict with
the token counts and the cost, and the agent records that usage on its own
card as the call returns. A plain-text publish arrives as `{"raw": text}`, so
the agent accepts both.

The function is `async def`, so it runs on the event loop and awaits the model
without holding anything else up. A plain `def` would run on a worker thread,
which is right for a local model that computes rather than waits.

Under the minimal profile no model is built unless one is named, which is why
`run.py` passes `llm=`. Without `minimal=True` the agent runs beside the full
system and uses whatever model it is configured with.
