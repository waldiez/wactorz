"""Start Wactorz with the summariser and the model it should use.

The minimal profile builds no model on its own, so one is named here:
`LLM_PROVIDER` if set, else Ollama on localhost. `LLM_PROVIDER=fake` answers
every note with a canned line, for a dry run without a model.
"""

import os

from agent import summarise

import wactorz

if __name__ == "__main__":
    wactorz.run(
        agents=[summarise],
        minimal=True,
        llm=os.environ.get("LLM_PROVIDER", "ollama"),
        state_dir="./state",
    )
