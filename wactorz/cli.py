import asyncio
import argparse
import logging
import os
import sys
import threading
import time

from pathlib import Path

_ = str(Path(__file__).parent)
if sys.path[0] != _:
	sys.path.insert(0, _)


from wactorz.config import CONFIG

# Windows: MUST be set before any async library is imported or started
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Fix Unicode output in Windows terminal (cp1252 -> utf-8)
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("wactorz.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


_RELOAD_PATTERNS = {".py", ".json", ".yaml", ".yml"}
_RELOAD_IGNORE   = {"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
_PKG_DIR         = Path(__file__).resolve().parent   # wactorz/
_RELOAD_CWD      = os.getcwd()


def _start_reloader() -> None:
    """Watch wactorz/ for source changes and restart the process via os.execv."""
    try:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer

        class _RestartHandler(FileSystemEventHandler):
            def __init__(self) -> None:
                super().__init__()
                self._timer: threading.Timer | None = None
                self._last = 0.0

            def _should_watch(self, path: str) -> bool:
                p = Path(path)
                if not any(p.name.endswith(ext) for ext in _RELOAD_PATTERNS):
                    return False
                return not any(part in _RELOAD_IGNORE for part in p.parts)

            def _schedule(self) -> None:
                if time.time() - self._last < 2.0:
                    return
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(0.5, self._restart)
                self._timer.start()

            def _restart(self) -> None:
                self._last = time.time()
                logger.info("[reload] restarting …")
                try:
                    os.chdir(_RELOAD_CWD)
                    time.sleep(0.1)
                    os.execv(sys.executable, [sys.executable] + sys.argv)  # nosec
                except Exception as exc:
                    logger.error("[reload] restart failed: %s", exc)
                    os._exit(1)  # nosec

            def on_modified(self, event: FileSystemEvent) -> None:
                if not event.is_directory and self._should_watch(str(event.src_path)):
                    logger.info("[reload] changed: %s", event.src_path)
                    self._schedule()

            def on_created(self, event: FileSystemEvent) -> None:
                if not event.is_directory and self._should_watch(str(event.src_path)):
                    self._schedule()

            def on_deleted(self, event: FileSystemEvent) -> None:
                if not event.is_directory and self._should_watch(str(event.src_path)):
                    self._schedule()

        observer = Observer()
        observer.schedule(_RestartHandler(), str(_PKG_DIR), recursive=True)
        observer.daemon = True
        observer.start()
        logger.info("[reload] watching %s", _PKG_DIR)

    except ImportError:
        logger.warning("[reload] watchdog not installed — pip install watchdog")


def get_args():
	parser = argparse.ArgumentParser(description="Wactorz - Multi-Agent Framework")
	parser.add_argument("--interface", choices=["cli", "rest", "discord", "whatsapp", "telegram"])
	parser.add_argument("--port", type=int)
	parser.add_argument("--llm", choices=["anthropic", "openai", "ollama", "nim", "gemini", "none"])
	parser.add_argument("--ollama-model",
	                    help="Ollama model name (e.g. llama3, mistral)")
	parser.add_argument("--nim-model",
	                    help="NVIDIA NIM model, e.g. meta/llama-3.3-70b-instruct or deepseek-ai/deepseek-r1")
	parser.add_argument("--gemini-model",
	                    default="gemini-2.5-flash",
	                    help="Google Gemini model (default: gemini-2.5-flash). Options: gemini-2.5-flash-lite, gemini-2.5-pro, gemini-3.1-pro")
	parser.add_argument("--discord-token")
	parser.add_argument("--telegram-token")
	parser.add_argument("--telegram-allowed-user-id", type=int)
	parser.add_argument("--mqtt-broker")
	parser.add_argument("--mqtt-port", type=int)
	parser.add_argument("--monitor-port", type=int, default=8888,
	                    help="Port for the background web UI / monitor server (default: 8888)")
	parser.add_argument("--no-monitor", action="store_true",
	                    help="Disable the background web UI server")
	parser.add_argument("--reload", action="store_true",
	                    help="Watch wactorz/ for changes and auto-restart (dev mode)")
	args, _ = parser.parse_known_args()

	return args


async def _start_web_ui(port: int, mqtt_broker: str, mqtt_port: int, actor_registry=None) -> None:
    """Start the monitor web server as a quiet background asyncio task."""
    import logging as _log
    import wactorz.monitor_server as _ms

    _ms.MQTT_BROKER  = mqtt_broker
    _ms.MQTT_PORT    = mqtt_port
    _ms.WS_PORT      = port

    # Wire the registry in so chat is routed directly — no IOAgent needed
    if actor_registry is not None:
        _ms.registry = actor_registry

    for _name in ("wactorz.monitor_server", "aiohttp.access", "aiohttp.server"):
        _log.getLogger(_name).setLevel(_log.WARNING)

    asyncio.create_task(_ms.main())
    logger.info("Web UI →  http://localhost:%d", port)
    if _ms.DOCS_SITE.is_dir():
        logger.info("Docs   →  http://localhost:%d/docs/", port)


async def build_system(args: argparse.Namespace):
    from wactorz.core.registry import ActorSystem
    from wactorz.core.actor import SupervisorStrategy
    from wactorz.agents.main_actor import MainActor
    from wactorz.agents.monitor_agent import MonitorActor
    from wactorz.agents.installer_agent import InstallerAgent
    from wactorz.agents.io_agent import IOAgent
    from wactorz.agents.manual_agent import ManualAgent
    from wactorz.agents.catalog_agent import CatalogAgent
    from wactorz.agents.llm_agent import (
        AnthropicProvider, OpenAIProvider, OllamaProvider,
        NIMProvider, GeminiProvider,
    )
    from wactorz.agents.home_assistant_agent import HomeAssistantAgent
    from wactorz.agents.home_assistant_map_agent import HomeAssistantMapAgent
    from wactorz.agents.home_assistant_state_bridge_agent import HomeAssistantStateBridgeAgent

    llm = args.llm or CONFIG.llm_provider
    if llm == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY") or CONFIG.llm_api_key
        provider = AnthropicProvider(model=CONFIG.llm_model, api_key=api_key)
    elif llm == "openai":
        api_key = os.getenv("OPENAI_API_KEY") or CONFIG.llm_api_key
        provider = OpenAIProvider(model=CONFIG.llm_model, api_key=api_key)
    elif llm == "ollama":
        ollama_model = args.ollama_model or CONFIG.llm_model
        provider = OllamaProvider(model=ollama_model, base_url=CONFIG.ollama_url)
    elif llm == "nim":
        nim_model = args.nim_model or CONFIG.llm_model
        provider = NIMProvider(
            model=nim_model,
            api_key=CONFIG.nim_api_key or CONFIG.nvidia_api_key,
        )
    elif llm == "gemini":
        gemini_model = args.gemini_model or CONFIG.llm_model or "gemini-2.5-flash"
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or CONFIG.llm_api_key
        provider = GeminiProvider(model=gemini_model, api_key=api_key)
    else:
        provider = None
        logger.warning("No LLM provider set. Agents will have limited capabilities.")

    # ── Build the ActorSystem first (MQTT starts here) ────────────────────────
    system = ActorSystem(
        mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
        mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
    )
    # MQTT client must exist before factories run so injected actors can publish
    system._mqtt_client = await __import__(
        "wactorz.core.registry", fromlist=["_MQTTPublisher"]
    )._MQTTPublisher.create(args.mqtt_broker or CONFIG.mqtt_host, args.mqtt_port or CONFIG.mqtt_port)

    # ── Factory helpers (called fresh on each (re)start by the Supervisor) ────
    def make_provider():
        return provider   # stateless — same instance is fine

    def make_main():
        return MainActor(llm_provider=make_provider(), name="main",
                         persistence_dir="./state")

    def make_monitor():
        return MonitorActor(check_interval=15.0, heartbeat_timeout=60.0,
                            auto_restart=False, persistence_dir="./state")

    def make_installer():
        return InstallerAgent(name="installer", persistence_dir="./state")

    def make_manual_agent():
        return ManualAgent(llm_provider=make_provider(), name="manual-agent",
                           persistence_dir="./state")

    def make_ha_agent():
        return HomeAssistantAgent(llm_provider=make_provider(),
                                  name="home-assistant-agent",
                                  persistence_dir="./state")

    def make_ha_map_agent():
        return HomeAssistantMapAgent(
            name="home-assistant-map-agent",
            persistence_dir="./state",
        )

    def make_ha_state_bridge():
        return HomeAssistantStateBridgeAgent(
            name="home-assistant-state-bridge",
            persistence_dir="./state",
        )

    def make_io_agent():
        return IOAgent(name="io-agent", persistence_dir="./state")


    def make_catalog():
        return CatalogAgent(name="catalog", persistence_dir="./state")

    # ── Register critical actors under the Supervisor ─────────────────────────
    (
        system.supervisor
        .supervise("main",                       make_main,          strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=2.0)
        .supervise("monitor",                    make_monitor,       strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=1.0)
        .supervise("io-agent",                   make_io_agent,      strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=1.0)
        .supervise("installer",                  make_installer,     strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=3,  restart_delay=2.0)
        .supervise("manual-agent",               make_manual_agent,  strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
        .supervise("home-assistant-agent",       make_ha_agent,      strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
        .supervise("home-assistant-map-agent",   make_ha_map_agent,  strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
        .supervise("home-assistant-state-bridge",make_ha_state_bridge, strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=5, restart_delay=1.0)
        .supervise("catalog",                    make_catalog,       strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=2.0)
    )

    await system.supervisor.start()

    main_actor = system.registry.find_by_name("main")

    logger.info("Wactorz system started. Supervision tree active.")
    return system, main_actor


async def app():
	args = get_args()
	if args.reload:
	    _start_reloader()
	system, main_actor = await build_system(args)

	if not args.no_monitor:
	    await _start_web_ui(
	        port=args.monitor_port,
	        mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
	        mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
	        actor_registry=system.registry,
	    )

	from wactorz.interfaces.chat_interfaces import (
	    CLIInterface, RESTInterface, DiscordInterface, WhatsAppInterface, TelegramInterface
	)

	interface = args.interface or CONFIG.interface
	if interface == "cli":
	    iface = CLIInterface(main_actor)
	    await asyncio.gather(iface.run(), system.run_forever())
	elif interface == "rest":
	    port = args.port or CONFIG.port
	    iface = RESTInterface(main_actor, port=port, api_key=CONFIG.api_key)
	    await asyncio.gather(iface.run(), system.run_forever())
	elif interface == "discord":
	    discord_token = args.discord_token or CONFIG.discord_token
	    if not discord_token:
	        logger.error("DISCORD_BOT_TOKEN not set.")
	        sys.exit(1)
	    iface = DiscordInterface(main_actor, token=discord_token)
	    await asyncio.gather(iface.run(), system.run_forever())
	elif interface == "whatsapp":
	    port = args.port or CONFIG.port
	    iface = WhatsAppInterface(
	        main_actor,
	        account_sid=CONFIG.twilio_account_sid,
	        auth_token=CONFIG.twilio_auth_token,
	        from_number=CONFIG.twilio_whatsapp_number,
	        port=port,
	    )
	    await asyncio.gather(iface.run(), system.run_forever())
	elif interface == "telegram":
	    telegram_token = args.telegram_token or CONFIG.telegram_token
	    if not telegram_token:
	        logger.error("TELEGRAM_BOT_TOKEN not set.")
	        sys.exit(1)
	    allowed_user_id = args.telegram_allowed_user_id or CONFIG.telegram_allowed_user_id or None
	    iface = TelegramInterface(main_actor, token=telegram_token, allowed_user_id=allowed_user_id)
	    await asyncio.gather(iface.run(), system.run_forever())


def main():
	asyncio.run(app())


if __name__ == "__main__":
	main()