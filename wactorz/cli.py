"""Command-line entry point: argument parsing and process launch.

System assembly and the run loop live in :mod:`wactorz.app`; the dev reloader in
:mod:`wactorz.dev_reload`; import-time setup in :mod:`wactorz._bootstrap`.
"""

import argparse
import asyncio
import os

import wactorz._bootstrap  # noqa: F401  side effects: import path, platform, root logging
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
        "--gemini-model",
        default="gemini-2.5-flash",
        help="Google Gemini model (default: gemini-2.5-flash). Options: gemini-2.5-flash-lite, gemini-2.5-pro, gemini-3.1-pro",
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

    asyncio.run(app(get_args()))


if __name__ == "__main__":
    main()
