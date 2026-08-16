"""Application assembly and run loop.

Builds the actor system (LLM provider, MQTT, persistence, supervision tree) and
runs the selected interface. Parsed arguments are supplied by :mod:`wactorz.cli`.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import cast

import wactorz._bootstrap  # noqa: F401  side effect: Windows event-loop + console encoding
from wactorz.agents.lookup import find_main_actor
from wactorz.config import CONFIG
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.paths import ensure_state_dir
from wactorz.dev_reload import start_reloader
from wactorz.monitoring.log_buffer import install as install_log_buffer
from wactorz.monitoring.log_setup import setup_logging
from wactorz.web.auth import exposure_refusal

logger = logging.getLogger(__name__)


async def _start_web_ui(
    port: int, mqtt_broker: str, mqtt_port: int, actor_registry=None, persistence_db=None
) -> None:
    """Start the monitor web server as a quiet background asyncio task."""
    from wactorz.web import runtime
    from wactorz.web.app import main as run_server

    runtime.MQTT_BROKER = mqtt_broker
    runtime.MQTT_PORT = mqtt_port
    runtime.WS_PORT = port

    # Wire the registry in so chat is routed directly — no IOAgent needed
    if actor_registry is not None:
        runtime.set_registry(actor_registry)
    if persistence_db is not None:
        runtime.set_db(persistence_db)

    for _name in ("wactorz.web", "aiohttp.access", "aiohttp.server"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    asyncio.create_task(run_server())


def _print_ready_banner(port: int) -> None:
    """Print where to point a browser, once everything is up.

    Last, not first: the web server binds before the agents start, so logging the
    URL at bind time buried it under the whole supervision tree coming up. A
    reader wants the address at the point the thing is actually usable.

    ``print`` rather than ``logger``: this is the one line a person is meant to
    act on, and when the login ceremony lands it gains a one-time link — which
    must reach the terminal without also being written to the unrotated log file
    or shipped onward by a metrics exporter.
    """
    from wactorz.web import login, static_site

    lines = [f"Dashboard   http://localhost:{port}/"]
    if static_site.DOCS_SITE.is_dir():
        lines.append(f"Docs        http://localhost:{port}/docs/")
    sign_in = login.sign_in_line(port)
    if sign_in is not None:
        lines.append(sign_in)
    width = max(len(line) for line in lines) + 4
    print("\n    ┌" + "─" * width + "┐")
    for line in lines:
        print(f"    │  {line.ljust(width - 4)}  │")
    print("    └" + "─" * width + "┘\n", flush=True)


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
    from wactorz.core.mqtt import broker_exposure_warning
    from wactorz.core.registry import ActorSystem
    from wactorz.llm_factory import create_provider, parse_overrides, provider_for
    from wactorz.web import auth

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
        # The parsed table rather than the raw string: what a site resolves to
        # is the thing worth seeing, and an entry that was dropped as malformed
        # or unknown is visible by its absence.
        overrides = parse_overrides(CONFIG.llm_overrides)
        logger.info(
            "LLM: %s/%s | temperature=%s%s",
            llm,
            getattr(provider, "model", None) or getattr(provider, "model_name", "?"),
            temperature,
            (
                " | overrides: "
                + ", ".join(f"{site}={spec}" for site, spec in sorted(overrides.items()))
                if overrides
                else ""
            ),
        )

    # ── Resolve the durable state directory (honours WACTORZ_STATE_DIR) ───────
    _sd = ensure_state_dir()

    # Said once, before the first connection: the broker is where spawn code
    # travels, so an exposed one is a bigger surface than it looks.
    _broker_host = args.mqtt_broker or CONFIG.mqtt_host
    _exposure = broker_exposure_warning(_broker_host, CONFIG.mqtt_username)
    if _exposure:
        logger.warning("[startup] %s", _exposure)

    # Said once too: a key short enough to guess is worth hearing about while
    # there is still a terminal open to read it.
    _weak_key = auth.weak_key_warning(CONFIG.api_key)
    if _weak_key:
        logger.warning("[startup] %s", _weak_key)

    # ── Build the ActorSystem first (MQTT starts here) ────────────────────────
    system = ActorSystem(
        mqtt_broker=args.mqtt_broker or CONFIG.mqtt_host,
        mqtt_port=args.mqtt_port or CONFIG.mqtt_port,
        state_dir=_sd,
    )
    # MQTT client must exist before factories run so injected actors can publish
    system._mqtt_client = await MQTTPublisher.create(
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

    # ── Initialise persistence layer (SQLite + Pickle) ──────────────────────
    from wactorz.core.persistence import PersistenceAPI, init_persistence

    _db, _pickle_store = init_persistence(
        db_path=os.path.join(_sd, "wactorz.db"),
        state_dir=_sd,
        run_migration=True,
    )
    logger.info("Persistence layer initialised (SQLite + Pickle)")

    # ── Factory helpers (called fresh on each (re)start by the Supervisor) ────
    def _wire_persistence(actor: Actor) -> Actor:
        """Attach the unified persistence API to an actor."""
        actor._persistence_api = PersistenceAPI(_db, _pickle_store, actor.name)
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

    main_actor = find_main_actor(system.registry)
    if not main_actor:
        logger.error("Failed to find the main actor.")
        sys.exit(1)

    logger.info("Wactorz system started. Supervision tree active.")
    return system, main_actor, _db


def _install_signal_handlers() -> None:
    """Make SIGTERM shut down the way Ctrl-C already does.

    Cancelling this task unwinds into the ``finally`` below, which stops the
    supervisor, the actors and the broker connection. Without a handler SIGTERM
    does nothing at all when the process is PID 1 — the kernel applies no default
    action there — so ``docker stop`` and ``systemctl stop`` waited out their
    timeout and killed the process mid-write instead.

    ``add_signal_handler`` is POSIX-only; Windows gets ``signal.signal``, which
    runs the callback in the main thread between bytecodes rather than on the
    loop, hence ``call_soon_threadsafe``.
    """
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - app() is always run as a task
        return
    loop = asyncio.get_running_loop()
    stopping = False

    def _request_stop(*_: object) -> None:
        nonlocal stopping
        if stopping:
            logger.warning("Shutdown already in progress — waiting for actors to stop.")
            return
        stopping = True
        logger.info("Shutdown signal received — stopping actors.")
        loop.call_soon_threadsafe(task.cancel)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            signal.signal(sig, _request_stop)


async def app(args: argparse.Namespace):
    # First, so both cover startup as well as steady state. setup_logging builds
    # the handlers with redaction already attached, so no record reaches an
    # unfiltered console or log file.
    setup_logging()
    install_log_buffer()

    # Before anything binds, and at the *process* root rather than in one
    # server's startup. Three servers read `CONFIG.bind_host` — the monitor, the
    # REST API and the WhatsApp webhook — so a check that lived in the monitor
    # alone left the REST interface serving chat and lifecycle commands to the
    # network in exactly the configuration this refusal exists to stop.
    refusal = exposure_refusal(CONFIG.bind_host, CONFIG.api_key)
    if refusal:
        logger.error("[startup] %s", refusal)
        raise SystemExit(1)

    if args.reload:
        start_reloader(logger)

    _install_signal_handlers()

    system, main_actor, _db = await build_system(args)

    from wactorz.core.persistence import close_persistence
    from wactorz.monitoring.influx import setup_influx, shutdown_influx
    from wactorz.monitoring.otel import setup_otel, shutdown_otel

    setup_otel(lambda: system.registry)
    setup_influx()

    if not getattr(args, "no_monitor", False):
        _print_ready_banner(args.monitor_port)

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
        elif interface == "tui":
            try:
                from wactorz.tui.app import run_async as _tui_run
            except ImportError:
                logger.error("TUI needs the 'tui' extra — pip install 'wactorz[tui]'")
                sys.exit(1)
            bots = [asyncio.create_task(c) for c in _run_all(companions)]
            try:
                await _tui_run()
            finally:
                for bot in bots:
                    bot.cancel()
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
        # Last: actors write state as they stop, so the connection has to outlive
        # them. Closing checkpoints the WAL rather than leaving -wal/-shm behind
        # for the next start to recover.
        close_persistence()
