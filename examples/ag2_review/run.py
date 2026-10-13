"""Start Wactorz with the AG2 review agent and the model it should use.

The minimal profile builds no model on its own, so one is named here:
`LLM_PROVIDER` and its key, as for every other Wactorz start. Without one the
conversation runs on default replies and the draft comes back unchanged.
"""

import os

from agent import review

import wactorz

if __name__ == "__main__":
    wactorz.run(
        agents=[review],
        minimal=True,
        llm=os.environ.get("LLM_PROVIDER", "none"),
        state_dir="./state",
    )
