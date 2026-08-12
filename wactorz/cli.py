"""Command-line entry point: argument parsing and process launch.

System assembly and the run loop live in :mod:`wactorz.app`; the dev reloader in
:mod:`wactorz.dev_reload`; import-time setup in :mod:`wactorz._bootstrap`.
"""

import argparse
import asyncio
import os

import wactorz._bootstrap  # noqa: F401  side effect: Windows event-loop + console encoding
from wactorz.config import CONFIG


def get_args():
    parser = argparse.ArgumentParser(description="Wactorz - Multi-Agent Framework")
    parser.add_argument("--interface", choices=["cli", "rest", "discord", "whatsapp", "telegram"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--llm", choices=["anthropic", "openai", "ollama", "nim", "gemini", "none"])
    parser.add_argument("--ollama-model", help="Ollama model name (e.g. llama3, mistral)")
    parser.add_argument(
        "--nim-model",
        help="NVIDIA NIM model, e.g. meta/llama-3.3-70b-instruct or deepseek-ai/deepseek-r1",
    )
    parser.add_argument(
        # No default, matching --ollama-model and --nim-model: argparse fills a
        # default in whether or not the flag was passed, so one here overrode
        # LLM_MODEL on every run and pinned Gemini to a single model for good.
        "--gemini-model",
        help="Google Gemini model (default: LLM_MODEL), e.g. gemini-3.6-flash",
    )
    parser.add_argument("--discord-token")
    parser.add_argument("--mqtt-broker")
    parser.add_argument("--mqtt-port", type=int)
    parser.add_argument("--telegram-token")
    parser.add_argument("--telegram-allowed-user-id", type=int)
    parser.add_argument(
        "--monitor-port",
        type=int,
        default=int(os.getenv("MONITOR_PORT", str(CONFIG.ws_port))),
        help="Port for the background web UI / monitor server (default: 8888)",
    )
    parser.add_argument(
        "--no-monitor", action="store_true", help="Disable the background web UI server"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Watch wactorz/ for changes and auto-restart (dev mode)",
    )
    args, _ = parser.parse_known_args()

    return args


def main():
    from wactorz.app import app

    try:
        asyncio.run(app(get_args()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A signal shuts down by cancelling the app task, which unwinds through
        # its own `finally` — the actors are already stopped by the time the
        # cancellation surfaces here. Swallow it so an intentional stop exits 0
        # and silently, rather than printing a traceback and reporting failure
        # to whatever supervises the process.
        pass


if __name__ == "__main__":
    main()
