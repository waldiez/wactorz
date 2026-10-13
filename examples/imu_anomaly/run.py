"""Start Wactorz with the detector and the dashboard on.

By default the minimal profile: the monitor and the given agents only, no
orchestrator, catalogue or installer, so no model API key is needed.

    python run.py

With ``--with-main`` the full system starts around the detector: main, the
planner and the catalogue, so chat can ask the detector for a verdict, main
lists it among the agents it can use, and the planner places it as a pipeline
step. A model is needed for main and the planner to think; ``--llm fake``
(or ``LLM_PROVIDER=fake``) runs them on scripted answers, which is enough to
ask the detector directly and to list what is registered.

    python run.py --with-main --llm fake
    LLM_PROVIDER=ollama python run.py --with-main
"""

import argparse

from agent import detect

import wactorz


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--with-main",
        action="store_true",
        help="start main, the planner and the catalogue around the detector",
    )
    parser.add_argument(
        "--llm",
        default=None,
        help="the model provider for main and the planner (default: LLM_PROVIDER); "
        "'fake' needs no API key",
    )
    args = parser.parse_args(argv)
    wactorz.run(
        agents=[detect],
        web=True,
        minimal=not args.with_main,
        llm=args.llm,
        state_dir="./state",
    )


if __name__ == "__main__":
    main()
