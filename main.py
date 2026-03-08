"""
AgentFlow - Entry Point
"""
import sys
import asyncio

# Windows: MUST be set before any async library is imported or started
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Fix Unicode output in Windows terminal (cp1252 -> utf-8)
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import logging
import argparse
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agentflow.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


async def build_system(args):
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

    if args.llm == "anthropic":
        provider = AnthropicProvider(api_key=os.getenv("ANTHROPIC_API_KEY"))
    elif args.llm == "openai":
        provider = OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY"))
    elif args.llm == "ollama":
        provider = OllamaProvider(model=args.ollama_model)
    elif args.llm == "nim":
        provider = NIMProvider(
            model=args.nim_model,
            api_key=os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY"),
        )
    else:
        provider = None
        logger.warning("No LLM provider set. Agents will have limited capabilities.")

    # ── Build the ActorSystem first (MQTT starts here) ────────────────────────
    system = ActorSystem(
        mqtt_broker=args.mqtt_broker,
        mqtt_port=args.mqtt_port,
    )
    # MQTT client must exist before factories run so injected actors can publish
    system._mqtt_client = await __import__(
        "agentflow.core.registry", fromlist=["_MQTTPublisher"]
    )._MQTTPublisher.create(args.mqtt_broker, args.mqtt_port)

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
        .supervise("anomaly-detector",      make_anomaly_agent, strategy=SupervisorStrategy.ONE_FOR_ONE,  max_restarts=5,  restart_delay=1.0)
    )

    # Supervisor.start() spawns all actors via their factories and starts the watch loop
    await system.supervisor.start()

    # Convenience references for the caller
    main_actor = system.registry.find_by_name("main")

    logger.info("AgentFlow system started. Supervision tree active.")
    return system, main_actor


async def main():
    parser = argparse.ArgumentParser(description="AgentFlow - Multi-Agent Framework")
    parser.add_argument("--interface", choices=["cli", "rest", "discord", "whatsapp"], default="cli")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--llm", choices=["anthropic", "openai", "ollama", "nim", "none"], default="anthropic")
    parser.add_argument("--ollama-model", default="llama3")
    parser.add_argument("--nim-model", default="meta/llama-3.3-70b-instruct",
                        help="NVIDIA NIM model, e.g. meta/llama-3.3-70b-instruct or deepseek-ai/deepseek-r1")
    parser.add_argument("--discord-token", default=os.getenv("DISCORD_BOT_TOKEN", ""))
    parser.add_argument("--mqtt-broker", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    args = parser.parse_args()

    system, main_actor = await build_system(args)

    from agentflow.interfaces.chat_interfaces import (
        CLIInterface, RESTInterface, DiscordInterface, WhatsAppInterface
    )

    if args.interface == "cli":
        iface = CLIInterface(main_actor)
        await asyncio.gather(iface.run(), system.run_forever())
    elif args.interface == "rest":
        iface = RESTInterface(main_actor, port=args.port, api_key=os.getenv("API_KEY"))
        await asyncio.gather(iface.run(), system.run_forever())
    elif args.interface == "discord":
        if not args.discord_token:
            logger.error("DISCORD_BOT_TOKEN not set.")
            sys.exit(1)
        iface = DiscordInterface(main_actor, token=args.discord_token)
        await asyncio.gather(iface.run(), system.run_forever())
    elif args.interface == "whatsapp":
        iface = WhatsAppInterface(
            main_actor,
            account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            from_number=os.getenv("TWILIO_WHATSAPP_NUMBER", ""),
            port=args.port,
        )
        await asyncio.gather(iface.run(), system.run_forever())


if __name__ == "__main__":
    asyncio.run(main())