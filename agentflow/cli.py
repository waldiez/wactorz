

import asyncio
import argparse
import logging
import os
import sys
import asyncio

from pathlib import Path

_ = str(Path(__file__).parent)
if sys.path[0] != _:
	sys.path.insert(0, _)


from agentflow.config import CONFIG

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
        logging.FileHandler("agentflow.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def get_args():
	parser = argparse.ArgumentParser(description="AgentFlow - Multi-Agent Framework")
	parser.add_argument("--interface", choices=["cli", "rest", "discord", "whatsapp"])
	parser.add_argument("--port", type=int)
	parser.add_argument("--llm", choices=["anthropic", "openai", "ollama", "nim", "none"])
	parser.add_argument("--ollama-model")
	parser.add_argument("--nim-model",
	                    help="NVIDIA NIM model, e.g. meta/llama-3.3-70b-instruct or deepseek-ai/deepseek-r1")
	parser.add_argument("--discord-token")
	parser.add_argument("--mqtt-broker")
	parser.add_argument("--mqtt-port", type=int)
	parser.add_argument("--monitor-port", type=int, default=8888,
	                    help="Port for the background web UI / monitor server (default: 8888)")
	parser.add_argument("--no-monitor", action="store_true",
	                    help="Disable the background web UI server")
	args, _ = parser.parse_known_args()

	return args


async def _start_web_ui(port: int, mqtt_broker: str, mqtt_port: int, actor_registry=None) -> None:
    """Start the monitor web server as a quiet background asyncio task."""
    import logging as _log
    import agentflow.monitor_server as _ms

    _ms.MQTT_BROKER  = mqtt_broker
    _ms.MQTT_PORT    = mqtt_port
    _ms.WS_PORT      = port

    # Wire the registry in so chat is routed directly — no IOAgent needed
    if actor_registry is not None:
        _ms.registry = actor_registry

    for _name in ("agentflow.monitor_server", "aiohttp.access", "aiohttp.server"):
        _log.getLogger(_name).setLevel(_log.WARNING)

    asyncio.create_task(_ms.main())
    logger.info("Web UI →  http://localhost:%d", port)
    if _ms.DOCS_SITE.is_dir():
        logger.info("Docs   →  http://localhost:%d/docs/", port)


async def build_system(args: argparse.Namespace):
    # CONFIG = get_config()
    from agentflow.core.registry import ActorSystem
    from agentflow.core.actor import SupervisorStrategy
    from agentflow.agents.main_actor import MainActor
    from agentflow.agents.monitor_agent import MonitorActor
    from agentflow.agents.code_agent import CodeAgent
    from agentflow.agents.ml_agent import AnomalyDetectorAgent
    from agentflow.agents.installer_agent import InstallerAgent
    from agentflow.agents.io_agent import IOAgent
    from agentflow.agents.manual_agent import ManualAgent
    from agentflow.agents.llm_agent import AnthropicProvider, OpenAIProvider, OllamaProvider, NIMProvider
    from agentflow.agents.home_assistant_agent import HomeAssistantAgent
    from agentflow.agents.home_assistant_map_agent import HomeAssistantMapAgent
    from agentflow.agents.home_assistant_state_bridge_agent import HomeAssistantStateBridgeAgent

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
        "agentflow.core.registry", fromlist=["_MQTTPublisher"]
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

    def make_code_agent():
        return CodeAgent(llm_provider=make_provider(), name="code-agent",
                         execution_mode="subprocess", persistence_dir="./state")

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

    def make_anomaly_agent():
        return AnomalyDetectorAgent(name="anomaly-detector", continuous=False,
                                    persistence_dir="./state")

    # ── Register critical actors under the Supervisor ─────────────────────────
    #
    # Strategy guide used here:
    #   main      — ONE_FOR_ONE: its crash doesn't require restarting others.
    #               High max_restarts (10) because it's the user-facing brain.
    #
    #   monitor   — ONE_FOR_ONE: independent health watcher; restart it alone.
    #
    #   installer — ONE_FOR_ONE: stateless pip runner; restart it alone.
    #               Lower max_restarts (3) — repeated failures mean something
    #               is wrong with the environment, not a transient glitch.
    #
    #   code-agent, manual-agent, home-assistant-agent — ONE_FOR_ONE with
    #               moderate budgets: specialist agents that are independent.
    #
    #   anomaly-detector — ONE_FOR_ONE: sensor pipeline, restart alone.
    #
    (
        system.supervisor
        .supervise("main",                  make_main,          strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=2.0)
        .supervise("monitor",               make_monitor,       strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=1.0)
        .supervise("io-agent",              make_io_agent,      strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=10, restart_delay=1.0)
        .supervise("installer",             make_installer,     strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=3,  restart_delay=2.0)
        .supervise("code-agent",            make_code_agent,    strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
        .supervise("manual-agent",          make_manual_agent,  strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
        .supervise("home-assistant-agent",  make_ha_agent,      strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
        .supervise("home-assistant-map-agent", make_ha_map_agent, strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=5, restart_delay=1.0)
        .supervise("home-assistant-state-bridge", make_ha_state_bridge, strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=5, restart_delay=1.0)
        .supervise("anomaly-detector",      make_anomaly_agent, strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
    )

    # Supervisor.start() spawns all actors via their factories and starts the watch loop
    await system.supervisor.start()

    # Convenience references for the caller
    main_actor = system.registry.find_by_name("main")

    logger.info("AgentFlow system started. Supervision tree active.")
    return system, main_actor

async def app():
	args = get_args()
	system, main_actor = await build_system(args)

	if not args.no_monitor:
	    await _start_web_ui(
	        port=args.monitor_port,
	        mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
	        mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
	        actor_registry=system.registry,
	    )

	from agentflow.interfaces.chat_interfaces import (
	    CLIInterface, RESTInterface, DiscordInterface, WhatsAppInterface
	)

	# CONFIG = get_config()
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


def main():
	asyncio.run(app())


if __name__ == "__main__":
	main()
