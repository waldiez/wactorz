"""
AgentFlow - Entry Point
"""
import logging
import argparse
import os
import sys
import asyncio

from pathlib import Path

try:
    from agentflow.config import CONFIG
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
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


async def build_system(args: argparse.Namespace):
    # CONFIG = get_config()
    from agentflow.core.registry import ActorSystem
    from agentflow.core.actor import SupervisorStrategy
    from agentflow.agents.main_actor import MainActor
    from agentflow.agents.monitor_agent import MonitorActor
    from agentflow.agents.code_agent import CodeAgent
    from agentflow.agents.ml_agent import AnomalyDetectorAgent
    from agentflow.agents.installer_agent import InstallerAgent
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



# def cli_main() -> None:
#     """Entry point for the `agentflow` console script."""
#     from agentflow.cli import get_parser
#     asyncio.run(main())


if __name__ == "__main__":
    from agentflow.cli import app
    asyncio.run(app())
