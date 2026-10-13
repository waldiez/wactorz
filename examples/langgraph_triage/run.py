"""Start Wactorz with the triage graph.

Without a model the graph classifies by keywords. Name one to have it read
the tickets: `LLM_PROVIDER=ollama`, or a hosted provider with its key.
"""

import os

from agent import triage

import wactorz

if __name__ == "__main__":
    wactorz.run(
        agents=[triage],
        minimal=True,
        llm=os.environ.get("LLM_PROVIDER", "none"),
        state_dir="./state",
    )
