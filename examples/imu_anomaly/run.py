"""Start Wactorz with the detector, the dashboard on, and nothing else.

`minimal=True` starts the monitor and the given agents only: no orchestrator,
catalogue or installer, so no model API key is needed. Leave it out to run
the detector beside the full system, where chat can ask it questions.
"""

from agent import detect

import wactorz

if __name__ == "__main__":
    wactorz.run(agents=[detect], web=True, minimal=True, state_dir="./state")
