"""Application assembly and run loop.

Builds the actor system (LLM provider, MQTT, persistence, supervision tree) and
runs the selected interface. Parsed arguments are supplied by :mod:`wactorz.cli`.
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import cast

import wactorz._bootstrap  # noqa: F401  side effects: import path, platform, root logging
from wactorz.config import CONFIG
from wactorz.core.paths import ensure_state_dir
from wactorz.dev_reload import start_reloader

logger = logging.getLogger(__name__)


async def _start_web_ui(
    port: int, mqtt_broker: str, mqtt_port: int, actor_registry=None, persistence_db=None
) -> None:
    """Start the monitor web server as a quiet background asyncio task."""
    from wactorz.web import runtime, static_site
    from wactorz.web.app import main as run_server

    runtime.MQTT_BROKER = mqtt_broker
    runtime.MQTT_PORT = mqtt_port
    runtime.WS_PORT = port
    runtime.MQTT_WS_PORT = int(os.getenv("MQTT_WS_PORT", "9001"))

    # Wire the registry in so chat is routed directly — no IOAgent needed
    if actor_registry is not None:
        runtime.set_registry(actor_registry)
    if persistence_db is not None:
        runtime.set_db(persistence_db)

    for _name in ("wactorz.web", "aiohttp.access", "aiohttp.server"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    asyncio.create_task(run_server())
    logger.info("Web UI →  http://localhost:%d", port)
    if static_site.DOCS_SITE.is_dir():
        logger.info("Docs   →  http://localhost:%d/docs/", port)


async def build_system(args: argparse.Namespace):
    from wactorz.agents.catalog_agent import CatalogAgent
    from wactorz.agents.home_assistant_agent import HomeAssistantAgent
    from wactorz.agents.home_assistant_map_agent import HomeAssistantMapAgent
    from wactorz.agents.home_assistant_state_bridge_agent import HomeAssistantStateBridgeAgent
    from wactorz.agents.installer_agent import InstallerAgent
    from wactorz.agents.io_agent import IOAgent
    from wactorz.agents.llm_agent import LLMProvider
    from wactorz.agents.main_actor import MainActor
    from wactorz.agents.monitor_agent import MonitorActor
    from wactorz.core.actor import Actor, SupervisorStrategy
    from wactorz.core.registry import ActorSystem
    from wactorz.llm_factory import create_provider, provider_for

    llm = args.llm or CONFIG.llm_provider
    model_flag = {
        "ollama": args.ollama_model,
        "nim": args.nim_model,
        "gemini": args.gemini_model,
    }.get(llm)
    try:
        provider = create_provider(llm, model_flag)
    except ValueError:
        provider = None
    if provider is None:
        logger.warning("No LLM provider set. Agents will have limited capabilities.")
    else:
        # One deterministic startup line so the active model and sampling
        # settings are visible without digging through provider dashboards.
        temperature = (
            "provider default" if CONFIG.llm_temperature is None else CONFIG.llm_temperature
        )
        logger.info(
            "LLM: %s/%s | temperature=%s%s",
            llm,
            getattr(provider, "model", None) or getattr(provider, "model_name", "?"),
            temperature,
            f" | overrides: {CONFIG.llm_overrides}" if CONFIG.llm_overrides else "",
        )

    # ── Resolve the durable state directory (honours WACTORZ_STATE_DIR) ───────
    _sd = ensure_state_dir()

    # ── Build the ActorSystem first (MQTT starts here) ────────────────────────
    system = ActorSystem(
        mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
        mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
        state_dir=_sd,
    )
    # MQTT client must exist before factories run so injected actors can publish
    system._mqtt_client = await __import__(
        "wactorz.core.registry", fromlist=["_MQTTPublisher"]
    )._MQTTPublisher.create(
        args.mqtt_broker or CONFIG.mqtt_host,
        args.mqtt_port or CONFIG.mqtt_port,
        db_path=os.path.join(_sd, "mqtt_outbox.db"),
    )

    # ── Initialise TopicBus (reactive pub/sub coordination layer) ─────────────
    # Must be done here because cli bypasses system.start() and goes directly
    # to system.supervisor.start() — so we initialise the bus manually.
    from wactorz.core.topic_bus import init_topic_bus

    system.topic_bus = init_topic_bus(
        mqtt_client=system._mqtt_client,
        mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
        mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
    )
    logger.info("TopicBus initialised")

    # ── Initialise persistence layer (SQLite + Redis + Pickle) ──────────────
    # Replaces pickle-only storage. Redis is optional — falls back to
    # in-memory dict if not running. Run migration once to move existing
    # .pkl data to the new stores.
    from wactorz.core.persistence import PersistenceAPI, init_persistence

    _db, _redis, _pickle_store = init_persistence(
        db_path=os.path.join(_sd, "wactorz.db"),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        state_dir=_sd,
        run_migration=True,
    )
    logger.info(
        "Persistence layer initialised (SQLite + %s + Pickle)",
        "Redis" if not _redis._using_fallback else "in-memory fallback",
    )

    # ── Factory helpers (called fresh on each (re)start by the Supervisor) ────
    def _wire_persistence(actor: Actor) -> Actor:
        """Attach the unified persistence API to an actor."""
        actor._persistence_api = PersistenceAPI(_db, _redis, _pickle_store, actor.name)
        return actor

    def make_provider() -> LLMProvider | None:
        return provider  # stateless — same instance is fine

    def make_main() -> MainActor:
        main_actor = _wire_persistence(
            MainActor(
                llm_provider=provider_for("main", make_provider()),
                name="main",
                persistence_dir=_sd,
            )
        )
        return cast(MainActor, main_actor)

    def make_monitor() -> Actor:
        return _wire_persistence(
            MonitorActor(
                check_interval=15.0,
                heartbeat_timeout=60.0,
                auto_restart=False,
                persistence_dir=_sd,
            )
        )

    def make_installer() -> Actor:
        return _wire_persistence(InstallerAgent(name="installer", persistence_dir=_sd))

    def make_ha_agent() -> Actor:
        return _wire_persistence(
            HomeAssistantAgent(
                llm_provider=provider_for("ha", make_provider()),
                name="home-assistant-agent",
                persistence_dir=_sd,
            )
        )

    def make_ha_map_agent() -> Actor:
        return _wire_persistence(
            HomeAssistantMapAgent(
                name="home-assistant-map-agent",
                persistence_dir=_sd,
            )
        )

    def make_ha_state_bridge() -> Actor:
        return _wire_persistence(
            HomeAssistantStateBridgeAgent(
                name="home-assistant-state-bridge",
                persistence_dir=_sd,
            )
        )

    def make_io_agent() -> Actor:
        return _wire_persistence(IOAgent(name="io-agent", persistence_dir=_sd))

    def make_catalog() -> Actor:
        return _wire_persistence(CatalogAgent(name="catalog", persistence_dir=_sd))

    (
        system.supervisor.supervise(
            "main",
            make_main,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=10,
            restart_delay=2.0,
        )
        .supervise(
            "monitor",
            make_monitor,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=10,
            restart_delay=1.0,
        )
        .supervise(
            "io-agent",
            make_io_agent,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=10,
            restart_delay=1.0,
        )
        .supervise(
            "installer",
            make_installer,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=3,
            restart_delay=2.0,
        )
        .supervise(
            "home-assistant-agent",
            make_ha_agent,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=5,
            restart_delay=1.0,
        )
        .supervise(
            "home-assistant-map-agent",
            make_ha_map_agent,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=5,
            restart_delay=1.0,
        )
        .supervise(
            "home-assistant-state-bridge",
            make_ha_state_bridge,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=5,
            restart_delay=1.0,
        )
        .supervise(
            "catalog",
            make_catalog,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=10,
            restart_delay=2.0,
        )
    )

    # Bind the monitor web UI BEFORE starting the supervisor. Agent startup
    # touches the MQTT broker, and on a slow/unreachable/auth-rejected broker
    # that can stall — previously the UI started *after* supervisor.start(), so a
    # stalled broker left the addon serving a blank page on boot. Starting the UI
    # first means it is always reachable (showing "connecting…" rather than
    # nothing) regardless of broker state. The registry is populated live as
    # agents register, so the overview fills in as they come up.
    if not getattr(args, "no_monitor", False):
        await _start_web_ui(
            port=args.monitor_port,
            mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
            mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
            actor_registry=system.registry,
            persistence_db=_db,
        )

    await system.supervisor.start()

    main_actor = system.registry.find_by_name("main")
    if not main_actor:
        logger.error("Failed to find the main actor.")
        sys.exit(1)

    logger.info("Wactorz system started. Supervision tree active.")
    return system, cast(MainActor, main_actor), _db


async def app(args: argparse.Namespace):
    if args.reload:
        start_reloader(logger)

    system, main_actor, _db = await build_system(args)

    from wactorz.monitoring.influx import setup_influx, shutdown_influx
    from wactorz.monitoring.otel import setup_otel, shutdown_otel

    setup_otel(lambda: system.registry)
    setup_influx()

    # NOTE: the monitor web UI is now started inside build_system(), before the
    # supervisor, so it binds even if the broker stalls agent startup.

    from wactorz.interfaces.chat_interfaces import (
        CLIInterface,
        DiscordInterface,
        RESTInterface,
        TelegramInterface,
        WhatsAppInterface,
        build_social_companions,
    )
    from wactorz.interfaces.chat_interfaces import (
        run_all_interfaces as _run_all,
    )

    interface = args.interface or CONFIG.interface

    # Configured social channels run alongside the primary interface, not
    # instead of it (the dashboard stays primary; the bots ride along).
    companions = build_social_companions(main_actor, interface)

    try:
        if interface == "cli":
            if sys.stdin.isatty():
                iface = CLIInterface(main_actor)
                await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
            else:
                # No TTY (piped/Docker/systemd): input() would raise EOFError on
                # the first read, finishing iface.run() instantly and — paired
                # with run_forever() — tearing the whole system down a second
                # after boot. Skip the interactive loop and just stay up.
                logger.info("stdin is not a TTY — running headless (no interactive CLI)")
                system._running = True
                await asyncio.gather(system.run_forever(), *_run_all(companions))
        elif interface == "rest":
            port = args.port or CONFIG.port
            iface = RESTInterface(main_actor, port=port, api_key=CONFIG.api_key)
            await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
        elif interface == "discord":
            discord_token = args.discord_token or CONFIG.discord_token
            if not discord_token:
                logger.error("DISCORD_BOT_TOKEN not set.")
                sys.exit(1)
            iface = DiscordInterface(
                main_actor,
                token=discord_token,
                allowed_user_ids=CONFIG.discord_allowed_user_ids,
            )
            await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
        elif interface == "whatsapp":
            port = args.port or CONFIG.port
            iface = WhatsAppInterface(
                main_actor,
                account_sid=CONFIG.twilio_account_sid,
                auth_token=CONFIG.twilio_auth_token,
                from_number=CONFIG.twilio_whatsapp_number,
                port=port,
                allowed_numbers=CONFIG.whatsapp_allowed_numbers,
            )
            await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
        elif interface == "telegram":
            telegram_token = args.telegram_token or CONFIG.telegram_token
            if not telegram_token:
                logger.error("TELEGRAM_BOT_TOKEN not set.")
                sys.exit(1)
            iface = TelegramInterface(
                main_actor,
                token=telegram_token,
                allowed_user_id=args.telegram_allowed_user_id or None,
                allowed_user_ids=CONFIG.telegram_allowed_user_ids,
            )
            await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
    except Exception as exc:
        logger.error(f"System error: {exc}", exc_info=True)
    finally:
        shutdown_otel()
        shutdown_influx()
        await system.stop_all()
