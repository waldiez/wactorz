"""An agent that uses the system's language model.

Notes arrive on MQTT; the agent asks the model for a one-line summary and
publishes it. `me.llm` is the provider the system runs on, chosen by
`LLM_PROVIDER` or `wactorz.run(..., llm=...)`, with the same cost limit and
the same `LLM_OVERRIDES` routing as the built-in agents. The function is
`async` so it can await the model.
"""

import wactorz

SYSTEM_PROMPT = (
    "You summarise notes. Answer with one sentence, no preamble, no quotes. "
    "Keep names, numbers and dates."
)


@wactorz.agent(
    name="note-summariser",
    subscribes="notes/raw/#",
    publishes="notes/summary",
    description="Summarises each note it hears in one sentence, with the model.",
    capabilities=["summarisation", "llm"],
    input_schema={"text": "str"},
    output_schema={"summary": "str", "cost_usd": "float"},
)
async def summarise(note: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Summarise one note; nothing is published for an empty one or without a model."""
    # JSON `{"text": ...}`, or a plain-text publish, which arrives as `{"raw": text}`.
    text = str(note.get("text") or note.get("raw") or "").strip()
    if not text:
        return None
    if me.llm is None:
        await me.log("No model: start with llm=... or LLM_PROVIDER set", level="warning")
        return None
    summary, usage = await me.llm.complete(
        [{"role": "user", "content": text}], system=SYSTEM_PROMPT
    )
    cost = float(usage.get("cost_usd", 0.0))
    me.persist("cost_usd_total", float(me.recall("cost_usd_total", 0.0)) + cost)
    me.persist("notes_total", int(me.recall("notes_total", 0)) + 1)
    return {"summary": summary.strip(), "cost_usd": round(cost, 6), "chars": len(text)}
