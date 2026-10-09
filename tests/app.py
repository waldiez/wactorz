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
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import wactorz._bootstrap  # noqa: F401  side effect: Windows event-loop + console encoding
from wactorz import config, pipelines, plugins, retention
from wactorz.agents.lookup import find_main_actor
from wactorz.agents.prompts.assemble import PromptFragment
from wactorz.agents.prompts.fragments import DEFAULT_FRAGMENTS
from wactorz.agents.prompts.home_assistant_prompts import HOME_ASSISTANT_FRAGMENT
from wactorz.broker_certificates import prepare_broker_files
from wactorz.cli import get_args
from wactorz.config import CONFIG, RETENTION_OUTBOX_DAYS, AppConfig
from wactorz.core import cancellation, own_tasks
from wactorz.core.cancellation import cancel_all_until_done, cancel_until_done
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.paths import ensure_state_dir, resolve_state_dir, set_state_dir
from wactorz.core.state_lock import StateInUseError, StateLock
from wactorz.dev_reload import start_reloader
from wactorz.errors import StartupError
from wactorz.monitoring.log_buffer import install as install_log_buffer
from wactorz.monitoring.log_buffer import uninstall as uninstall_log_buffer
from wactorz.monitoring.log_setup import install_fallback, setup_logging, uninstall_fallback
from wactorz.monitoring.loop_lag import LoopLagMonitor
from wactorz.orchestration import MainOrchestrator
from wactorz.web import runtime
from wactorz.web.auth import exposure_refusal

if TYPE_CHECKING:
    from wactorz.agents.llm_agent import LLMProvider
    from wactorz.core.registry import ActorSystem
    from wactorz.orchestration import Orchestrator

logger = logging.getLogger(__name__)

#: Set as the shutdown sequence begins, so the signal handler stops asking the app
#: task to stop once it is stopping. A threading.Event rather than an asyncio one:
#: the Windows fallback runs the handler between bytecodes, outside the loop.
_shutting_down = threading.Event()

#: Watches the event loop from a thread, and says in the log where it is when it
#: stops running.
_loop_lag = LoopLagMonitor()
#: Held while the system runs, so nothing else runs on its state directory.
_state_lock = StateLock(".")

#: The ActorSystem this process is running, from the moment it is built until
#: shutdown has stopped it. What :func:`system` answers with.
_current_system: "ActorSystem | None" = None

#: The interface setting for no chat interface of the system's own: the
#: dashboard and any companion channels only. What :func:`serve` runs with
#: unless it is given an interface, so a library call never reads the host's stdin.
HEADLESS = "none"

#: The names the system's own agents go by, whether or not this run starts
#: them. The supervisor keeps one entry per name, so a plugin or pipeline agent
#: under one of these would replace the built-in; it is refused instead.
BUILT_IN_AGENT_NAMES = frozenset(
    {
        "main",
        "monitor",
        "installer",
        "catalog",
        "home-assistant-agent",
        "home-assistant-map-agent",
        "home-assistant-state-bridge",
        runtime.IO_GATEWAY_ID,
    }
)


def startable_plugins(found: Iterable[plugins.AgentPlugin]) -> list[plugins.AgentPlugin]:
    """The plugins to start with the system: those for autostart, under a name of their own.

    One under a built-in agent's name is left out with an error that names it,
    as the catalogue leaves out a plugin that a packaged recipe's name shadows.
    """
    chosen: list[plugins.AgentPlugin] = []
    for plugin in found:
        if not plugin.autostart:
            continue
        if plugin.name in BUILT_IN_AGENT_NAMES:
            logger.error(
                "[plugins] Agent %r not started: a built-in agent has that name. Rename it.",
                plugin.name,
            )
            continue
        chosen.append(plugin)
    return chosen


def startable_pipelines(found: Iterable[pipelines.Pipeline]) -> list[pipelines.Pipeline]:
    """The pipelines to start: those none of whose agents takes a built-in agent's name.

    A pipeline is all or nothing, since a step missing from it breaks the rest,
    so a clash leaves out the whole pipeline, with an error naming the agents.
    """
    chosen: list[pipelines.Pipeline] = []
    for pipe in found:
        clashes = sorted(set(pipe.agent_names) & BUILT_IN_AGENT_NAMES)
        if clashes:
            logger.error(
                "[pipelines] Pipeline %r not started: %s is a built-in agent's name. Rename it.",
                pipe.name,
                ", ".join(clashes),
            )
            continue
        chosen.append(pipe)
    return chosen


def system() -> "ActorSystem | None":
    """The running :class:`~wactorz.core.registry.ActorSystem`, or ``None`` outside a run.

    For a program that embeds Wactorz and wants at its actors: the registry
    (``system().registry.find_by_name(...)``), the supervisor, the broker
    publisher. Set as soon as the system is built, so a host that started
    :func:`serve` as a task can reach it once that task is under way, and
    cleared when the run is over.
    """
    return _current_system


async def _start_web_ui(
    port: int,
    mqtt_broker: str,
    mqtt_port: int,
    actor_registry=None,
    persistence_db=None,
    system: "ActorSystem | None" = None,
) -> None:
    """Start the monitor web server as a quiet background asyncio task."""
    from wactorz.web.app import main as run_server

    runtime.MQTT_BROKER = mqtt_broker
    runtime.MQTT_PORT = mqtt_port
    runtime.WS_PORT = port

    # Wire the registry in so chat is routed directly
    if actor_registry is not None:
        runtime.set_registry(actor_registry)
    if system is not None:
        runtime.set_system(system)
    if persistence_db is not None:
        runtime.set_db(persistence_db)

    for _name in ("wactorz.web", "aiohttp.access", "aiohttp.server"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    runtime.server_task = asyncio.create_task(run_server(), name="monitor")


#: How long shutdown waits for the monitor to stop, asking again as it goes. Well
#: past its ordinary cleanup, which is closing the broker listener and the server.
MONITOR_STOP_TIMEOUT_S = 10.0


async def _stop_web_ui() -> None:
    """Stop the monitor task this process started, if it started one.

    Here rather than left to ``asyncio.run``, which cancels whatever is still
    running once ``app()`` returns and then waits on it without a limit. On
    Python 3.10 and 3.11 that one request can be lost inside the broker listener,
    when it lands while a subscribe is completing, and the process then never
    exits. :func:`cancel_until_done` asks again until the task has stopped.
    """
    task, runtime.server_task = runtime.server_task, None
    if task is None:
        return
    if not await cancel_until_done(task, timeout=MONITOR_STOP_TIMEOUT_S):
        logger.warning("[shutdown] The monitor did not stop within %gs.", MONITOR_STOP_TIMEOUT_S)


#: How long shutdown waits for the tasks nothing else stopped, asking again as it goes.
LEFTOVER_STOP_TIMEOUT_S = 10.0


async def _stop_leftover_tasks() -> None:
    """Stop every task of ours still running, asking again for any that lose the request.

    What ``asyncio.run`` does once ``app()`` returns, done here with
    :func:`cancel_until_done` instead of a single cancellation and an open-ended
    wait. A broker connection an agent opened and nothing closed, such as a topic
    stream window, can lose that one request on Python 3.10 and 3.11 and hold the
    process open.

    Ours, not every task on the loop: a host program that lent us its loop has
    tasks of its own on it, started before or during the run, and those are
    left alone. See :mod:`wactorz.core.own_tasks`.
    """
    current = asyncio.current_task()
    await _stop_tasks([task for task in own_tasks.own_tasks() if task is not current])


async def _stop_tasks(
    tasks: list[asyncio.Task[Any]], timeout: float = LEFTOVER_STOP_TIMEOUT_S
) -> None:
    """Stop `tasks` together, and name any that are still running at the end."""
    stuck = await cancel_all_until_done(tasks, timeout=timeout)
    if stuck:
        logger.warning(
            "[shutdown] %d task(s) did not stop within %gs: %s",
            len(stuck),
            timeout,
            ", ".join(task.get_name() for task in stuck),
        )


def _print_ready_banner(port: int) -> None:
    """Print where to point a browser, once everything is up.

    Last, not first: the web server binds before the agents start, so logging the
    URL at bind time buried it under the whole supervision tree coming up. A
    reader wants the address at the point the thing is actually usable.

    ``print`` rather than ``logger``: this is the one line a person is meant to
    act on, and when the login ceremony lands it gains a one-time link — which
    must reach the terminal without also being written to the unrotated log file
    or shipped onward by a metrics exporter.

    Behind Home Assistant's ingress there is no terminal. What is printed goes
    into the add-on's log, which Home Assistant keeps and shows, and
    ``localhost`` there is the inside of a container nobody browses to. So the
    banner points at the panel, which signs the user in itself, and no sign-in
    link is made at all.
    """
    from wactorz.web import login, static_site

    if config.INGRESS_ENABLED:
        lines = ["Open Wactorz from the Home Assistant sidebar."]
    else:
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


def home_assistant_agents_enabled(settings: AppConfig) -> bool:
    """Whether the Home Assistant agents are part of this system.

    ``WACTORZ_HA_AGENTS`` decides outright with ``on`` or ``off``; ``auto``
    starts them when Home Assistant is configured and leaves them out when it
    is not, so a deployment without one is not told about lights.
    """
    if settings.ha_agents == "on":
        return True
    if settings.ha_agents == "off":
        return False
    return bool(settings.ha_url and settings.ha_token)


def prompt_fragments_for(settings: AppConfig) -> tuple[PromptFragment, ...]:
    """What main's and the planner's prompts speak of on this installation.

    Each integration's fragment is included exactly when its agents are part
    of the system, so the same test decides both: a deployment whose Home
    Assistant agents do not start is not told about lights either. Decided
    once here and handed to main, which hands it to the planners it spawns.
    """
    fragments = []
    for fragment in DEFAULT_FRAGMENTS:
        if fragment is HOME_ASSISTANT_FRAGMENT and not home_assistant_agents_enabled(settings):
            continue
        fragments.append(fragment)
    return tuple(fragments)


#: The extra that installs each provider's SDK, for the message when it is missing.
_PROVIDER_EXTRAS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "nim": "openai",
    "gemini": "google",
}


def _missing_sdk_message(llm: str, exc: ImportError) -> str:
    """Why the provider cannot be built, and the two ways out."""
    extra = _PROVIDER_EXTRAS.get(llm)
    install = f"pip install 'wactorz[{extra}]'" if extra else f"install {exc.name or 'its SDK'}"
    return (
        f"LLM provider {llm!r} needs an SDK that is not installed ({exc}). "
        f"Either {install}, or run without a model: LLM_PROVIDER=none, "
        f'--llm none, or wactorz.run(..., llm="none").'
    )


async def _build_provider(args: argparse.Namespace, minimal: bool) -> "LLMProvider | None":
    """The model the system runs on, as the arguments and the profile say, or ``None``.

    The minimal profile runs nothing that needs a model, so one is built only
    when asked for by name: the default provider's SDK is an optional extra,
    and a deployment that brought its own agents need not have it. A provider
    whose SDK is not installed is a :class:`StartupError` that says what to
    install, or how to run without one.
    """
    from wactorz.llm_factory import create_provider, parse_overrides

    llm = args.llm or ("none" if minimal else CONFIG.llm_provider)
    model_flag = {
        "ollama": args.ollama_model,
        "nim": args.nim_model,
        "gemini": args.gemini_model,
    }.get(llm)
    try:
        # In a thread: building a provider imports its SDK, which on a small
        # machine with a cold disk takes seconds, and nothing else of the
        # start-up has to wait behind it.
        provider = await asyncio.to_thread(create_provider, llm, model_flag)
    except ValueError:
        provider = None
    except ImportError as exc:
        raise StartupError(_missing_sdk_message(llm, exc)) from exc
    if provider is None:
        if minimal:
            logger.info("Minimal profile: no model; the agents run on their own.")
        else:
            logger.warning("No LLM provider set. Agents will have limited capabilities.")
        return None
    # One deterministic startup line so the active model and sampling
    # settings are visible without digging through provider dashboards.
    temperature = "provider default" if CONFIG.llm_temperature is None else CONFIG.llm_temperature
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
    return provider


async def build_system(
    args: argparse.Namespace, on_system: "Callable[[ActorSystem], object] | None" = None
):
    from wactorz.agents.catalog_agent import CatalogAgent
    from wactorz.agents.home_assistant_agent import HomeAssistantAgent
    from wactorz.agents.home_assistant_map_agent import HomeAssistantMapAgent
    from wactorz.agents.home_assistant_state_bridge_agent import HomeAssistantStateBridgeAgent
    from wactorz.agents.installer_agent import InstallerAgent
    from wactorz.agents.llm_agent import LLMProvider
    from wactorz.agents.main.actor import MainActor
    from wactorz.agents.monitor_agent import MonitorActor
    from wactorz.core.actor import Actor, SupervisorStrategy
    from wactorz.core.mqtt import broker_exposure_warning
    from wactorz.core.registry import ActorSystem
    from wactorz.llm_factory import provider_for
    from wactorz.web import auth

    minimal = bool(getattr(args, "minimal", False) or CONFIG.minimal)
    provider = await _build_provider(args, minimal)

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
    global _current_system
    _current_system = system
    if on_system is not None:
        # Handed over before anything is started on it, so a stop that arrives
        # part-way through startup can still stop whatever had started by then.
        on_system(system)
    # MQTT client must exist before factories run so injected actors can publish
    system._mqtt_client = await MQTTPublisher.create(
        args.mqtt_broker or CONFIG.mqtt_host,
        args.mqtt_port or CONFIG.mqtt_port,
        db_path=Path(_sd) / "mqtt_outbox.db",
        dead_letter_days=RETENTION_OUTBOX_DAYS,
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
        db_path=Path(_sd) / "wactorz.db",
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

    prompt_fragments = prompt_fragments_for(CONFIG)

    def make_main() -> MainActor:
        main_actor = _wire_persistence(
            MainActor(
                llm_provider=provider_for("main", make_provider()),
                name="main",
                persistence_dir=_sd,
                prompt_fragments=prompt_fragments,
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

    def make_catalog() -> Actor:
        return _wire_persistence(CatalogAgent(name="catalog", persistence_dir=_sd))

    def pipeline_factories(pipe: pipelines.Pipeline) -> list[tuple[str, Callable[[], Actor]]]:
        """One supervised factory per agent of ``pipe``: its steps, schedule and rules."""
        from wactorz.agents.rule_agent import RuleAgent
        from wactorz.agents.scheduled_agent import ScheduledAgent

        factories: list[tuple[str, Callable[[], Actor]]] = [
            (step.name, make_plugin_factory(step)) for step in pipe.steps
        ]
        if pipe.schedule is not None:

            def make_schedule(
                spec: dict[str, Any] = pipe.schedule, topic: str = pipe.tick_topic
            ) -> Actor:
                return _wire_persistence(
                    ScheduledAgent(
                        name=pipe.schedule_name,
                        schedule=spec,
                        publish_topic=topic,
                        description=f"ticks pipeline {pipe.name}",
                        persistence_dir=_sd,
                    )
                )

            factories.append((pipe.schedule_name, make_schedule))
        for rule_name, rule in zip(pipe.rule_names, pipe.rules, strict=True):

            def make_rule(name: str = rule_name, cfg: Any = rule) -> Actor:
                return _wire_persistence(RuleAgent(cfg, name=name, persistence_dir=_sd))

            factories.append((rule_name, make_rule))
        return factories

    def make_plugin_factory(plugin: plugins.AgentPlugin) -> Callable[[], Actor]:
        def make() -> Actor:
            return _wire_persistence(
                plugin.build(
                    persistence_dir=_sd, llm_provider=provider_for("dynamic", make_provider())
                )
            )

        return make

    if minimal:
        logger.info("Minimal profile: starting the monitor and this deployment's agents only.")
    else:
        system.supervisor.supervise(
            "main",
            make_main,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=10,
            restart_delay=2.0,
        )
    system.supervisor.supervise(
        "monitor",
        make_monitor,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=10,
        restart_delay=1.0,
    )
    if not minimal:
        system.supervisor.supervise(
            "installer",
            make_installer,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=3,
            restart_delay=2.0,
        )
    if home_assistant_agents_enabled(CONFIG):
        (
            system.supervisor.supervise(
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
        )
    else:
        logger.info("Home Assistant agents not started: HA_URL and HA_TOKEN are not set.")
    if not minimal:
        system.supervisor.supervise(
            "catalog",
            make_catalog,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=10,
            restart_delay=2.0,
        )

    # The agents this deployment brings -- WACTORZ_AGENTS, wactorz.agents entry
    # points, wactorz.run(agents=...) -- supervised beside the built-ins, with
    # the same persistence. One that is not for autostart waits in the
    # catalogue to be asked for.
    for plugin in startable_plugins(plugins.discover().values()):
        system.supervisor.supervise(
            plugin.name,
            make_plugin_factory(plugin),
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=5,
            restart_delay=1.0,
        )

    # The pipelines this deployment declares: each step, schedule and rule is
    # an agent of its own, supervised like the plugins above. The definition
    # is the record; nothing of it goes through the spawn registry.
    started_pipelines = startable_pipelines(pipelines.discover().values())
    for pipe in started_pipelines:
        for agent_name, factory in pipeline_factories(pipe):
            system.supervisor.supervise(
                agent_name,
                factory,
                strategy=SupervisorStrategy.ONE_FOR_ONE,
                max_restarts=5,
                restart_delay=1.0,
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
            system=system,
        )

    # After the stores exist and before the agents that write to them: the
    # checkpoint it schedules is the one SQLite would otherwise take inline, on
    # whichever `persist()` crossed its threshold.
    from wactorz.core.persistence import maintenance

    # Before the rotation starts, so it runs ahead of the checkpoint that folds
    # its deletes back into the database.
    maintenance.register("retention", retention.prune)
    maintenance.start()

    await system.supervisor.start()

    main_actor = find_main_actor(system.registry)
    if not main_actor and not minimal:
        raise StartupError("the main actor did not start; see the log above for why")
    install_orchestrator(system, main_actor)
    if main_actor is not None:
        # Recorded beside the planner's rules, so `/rules` lists a declared
        # pipeline and `/rules delete` stops the whole of it.
        known = main_actor.get_pipeline_rules()
        for pipe in started_pipelines:
            if pipe.name not in known:
                main_actor.save_pipeline_rule(pipe.record())

    logger.info("Wactorz system started. Supervision tree active.")
    return system, main_actor, _db


def install_orchestrator(system: "ActorSystem", main_actor: Any) -> "Orchestrator | None":
    """Choose what answers chat for this run, and make it ``runtime.orchestrator``.

    Main's adapter when main runs. Nothing otherwise, so a chat surface finds
    no orchestrator rather than one left behind by an earlier run in the same
    process.
    """
    chosen = MainOrchestrator(system.registry) if main_actor is not None else None
    runtime.set_orchestrator(chosen)
    return chosen


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
    _shutting_down.clear()

    def _keep_asking() -> None:
        """Cancel the app task, and ask again later until its shutdown has begun.

        One request is not always enough. On Python 3.10 and 3.11 a cancellation
        that lands while startup is inside a wait_for is discarded — the broker and
        Home Assistant clients both wait that way — and startup then carries on as
        though nothing had asked it to stop. A timer rather than a task, so the
        shutdown's own sweep of leftover tasks has nothing of this to cancel.
        """
        if task.done() or _shutting_down.is_set():
            return
        task.cancel()
        loop.call_later(cancellation.RECANCEL_AFTER_S, _keep_asking)

    def _request_stop(*_: object) -> None:
        nonlocal stopping
        if stopping:
            logger.warning("Shutdown already in progress — waiting for actors to stop.")
            return
        stopping = True
        logger.info("Shutdown signal received — stopping actors.")
        loop.call_soon_threadsafe(_keep_asking)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            signal.signal(sig, _request_stop)


async def _build_system_or_stop(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """Build the system, and stop what had started if startup does not finish.

    A stop requested during startup arrives as a cancellation inside build_system,
    before app() reaches the try whose finally shuts down. Left there, the agents
    that had started would never be stopped, so their state would not be written,
    and everything still running would get asyncio.run's single cancellation —
    the one a lost request on Python 3.10 and 3.11 turns into a hang.
    """
    started: list[ActorSystem] = []
    try:
        return await build_system(args, on_system=started.append)
    except BaseException:
        await _shut_down(started[0] if started else None)
        raise


async def _shut_down(system: "ActorSystem | None") -> None:
    """Stop everything, in the order that keeps state intact.

    Shared by every way out: a stop once the system is running, one that
    arrives part-way through startup, when some of it never started, and a
    start refused before anything was built. Each step copes with what did
    not happen. Leaves the process as it was found: no thread of ours watching
    the loop, no handler of ours on the root logger, and a host's own tasks
    still running.
    """
    # First of all, before anything is awaited: from here on the signal handler
    # stops asking, so a repeated request never lands inside the shutdown itself.
    _shutting_down.set()
    global _current_system
    _current_system = None
    from wactorz.core.persistence import close_persistence, maintenance

    # First: a scheduled job holds the connection lock while it runs, and the
    # actors below are about to want it to write their state out.
    await maintenance.stop()
    if system is not None:
        await system.stop_all()
    # After the agents, so the dashboard still hears them stop, and before the
    # database closes, because the monitor's broker listener writes chat to it.
    await _stop_web_ui()
    # Then the database: actors write state as they stop, so the connection has to
    # outlive them. Closing checkpoints the WAL rather than leaving -wal/-shm
    # behind for the next start to recover.
    close_persistence()
    _loop_lag.stop()
    # Last, rather than left to asyncio.run: see _stop_leftover_tasks.
    await _stop_leftover_tasks()
    # After everything that might still log a line worth seeing on the dashboard.
    uninstall_log_buffer()
    uninstall_fallback()


async def app(
    args: argparse.Namespace, *, handle_signals: bool = True, configure_logging: bool = True
):
    """Run the system described by ``args`` until it is stopped.

    ``handle_signals`` installs the SIGINT/SIGTERM handlers; a host with a loop
    of its own leaves it off and cancels the task instead. ``configure_logging``
    sets up the process's logging the way the ``wactorz`` command wants it:
    console and file handlers on the root logger, with redaction. A library
    caller leaves that off and keeps its own; the dashboard's log view still
    works, since its buffer is attached either way. Raises
    :class:`~wactorz.errors.StartupError` when the configuration cannot be
    started as it stands.
    """
    # First, so both cover startup as well as steady state. setup_logging builds
    # the handlers with redaction already attached, so no record reaches an
    # unfiltered console or log file.
    if configure_logging:
        setup_logging()
    else:
        # Before the buffer below, which would otherwise silence every warning
        # in a host that configured no logging of its own.
        install_fallback()
    install_log_buffer()
    # From the start, so a startup step that blocks the loop is named too.
    _loop_lag.start()
    # Before the first task of ours, so shutdown can tell ours from a host's on
    # the loop it lent us; kept until the shutdown has stopped them.
    tagger = own_tasks.start()
    try:
        try:
            _check_startable(args, handle_signals=handle_signals)
        except BaseException:
            # Refused, or stopped, before the system was built: what was started
            # above is still undone. From here on each stage shuts down its own.
            await _shut_down(None)
            raise

        system, main_actor, _db = await _build_system_or_stop(args)
        await _run(args, system, main_actor)
    finally:
        _state_lock.release()
        own_tasks.stop(tagger)


def loop_refusal(loop: asyncio.AbstractEventLoop) -> str:
    """Why the system cannot run on ``loop``, or an empty string.

    The broker client watches sockets through ``add_reader``, which a loop
    that never overrode the abstract one cannot do: Windows' proactor loop,
    which uvicorn builds there by default, and which is already running by
    the time a host awaits :func:`serve`. A running loop cannot be swapped, so
    the most the library can do is say so, and how to start differently.
    """
    if getattr(type(loop), "add_reader", None) is not asyncio.AbstractEventLoop.add_reader:
        return ""
    return (
        f"the running event loop ({type(loop).__name__}) cannot watch sockets, which the "
        "broker connection needs. Start the host on a selector loop: with uvicorn, "
        "`--loop asyncio:SelectorEventLoop`; in a script, "
        "`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` "
        "before the loop is made."
    )


def _check_startable(args: argparse.Namespace, *, handle_signals: bool) -> None:
    """What is settled before anything is built; see :func:`app`."""
    refused = loop_refusal(asyncio.get_running_loop())
    if refused:
        raise StartupError(refused)
    # Before anything reads the state directory: a second process on it would
    # write back over the first, and an import into it would be undone.
    _state_lock.directory = Path(resolve_state_dir())
    try:
        _state_lock.acquire()
    except StateInUseError as exc:
        raise StartupError(str(exc)) from None
    # Before anything binds, and at the *process* root rather than in one
    # server's startup. Three servers read `CONFIG.bind_host` — the monitor, the
    # REST API and the WhatsApp webhook — so a check that lived in the monitor
    # alone left the REST interface serving chat and lifecycle commands to the
    # network in exactly the configuration this refusal exists to stop.
    #
    # What a broker of ours reads is written here as well -- its TLS certificate,
    # and the node accounts when those are on -- before the first connection. A CA
    # that cannot be loaded refuses the same way: it would fail every reconnect
    # after this one too.
    refusal = exposure_refusal(CONFIG.bind_host, CONFIG.api_key) or prepare_broker_files()
    if refusal:
        raise StartupError(refusal)  # app() shuts down what it started

    if args.reload:
        start_reloader(logger)

    # A host that embeds the system -- a notebook, a web framework, a ROS node --
    # owns its signals; it stops this by cancelling the task, which unwinds into
    # the same shutdown below.
    if handle_signals:
        _install_signal_handlers()


async def _run_interface(
    args: argparse.Namespace,
    system: "ActorSystem",
    main_actor: Any,
    interface: str,
    companions: list[Any],
) -> None:
    """Run ``interface``, the system and ``companions`` until one of them ends."""
    from wactorz.interfaces.chat_interfaces import (
        CLIInterface,
        DiscordInterface,
        RESTInterface,
        TelegramInterface,
        WhatsAppInterface,
    )
    from wactorz.interfaces.chat_interfaces import (
        run_all_interfaces as _run_all,
    )

    if main_actor is None:
        # The minimal profile: no orchestrator to talk to, so no chat
        # interface. The dashboard, the monitor and the deployment's own
        # agents run until asked to stop.
        system._running = True
        await system.run_forever()
    elif interface == "cli" and sys.stdin.isatty():
        iface = CLIInterface(main_actor)
        await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
    elif interface in ("cli", HEADLESS):
        # No chat interface of our own: what a library call asks for, and
        # what a CLI with no TTY (piped/Docker/systemd) falls back to, since
        # input() would raise EOFError on the first read and, paired with
        # run_forever(), tear the whole system down a second after boot.
        # The dashboard and any companion channels still talk to main.
        if interface == "cli":
            logger.info("stdin is not a TTY — running headless (no interactive CLI)")
        system._running = True
        await asyncio.gather(system.run_forever(), *_run_all(companions))
    elif interface == "rest":
        port = args.port or CONFIG.port
        iface = RESTInterface(main_actor, port=port, api_key=CONFIG.api_key, system=system)
        await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
    elif interface == "discord":
        discord_token = args.discord_token or CONFIG.discord_token
        if not discord_token:
            raise StartupError("DISCORD_BOT_TOKEN not set.")
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
            raise StartupError("TELEGRAM_BOT_TOKEN not set.")
        iface = TelegramInterface(
            main_actor,
            token=telegram_token,
            allowed_user_id=args.telegram_allowed_user_id or None,
            allowed_user_ids=CONFIG.telegram_allowed_user_ids,
        )
        await asyncio.gather(iface.run(), system.run_forever(), *_run_all(companions))
    else:
        raise StartupError(
            f"unknown interface {interface!r}: use cli, rest, discord, whatsapp, "
            f"telegram or {HEADLESS}"
        )


async def _run(args: argparse.Namespace, system: "ActorSystem", main_actor: Any) -> None:
    """Run the built system under the chosen interface until stopped, then shut it down."""
    if not getattr(args, "no_monitor", False):
        _print_ready_banner(args.monitor_port)

    # NOTE: the monitor web UI is now started inside build_system(), before the
    # supervisor, so it binds even if the broker stalls agent startup.

    from wactorz.interfaces.chat_interfaces import build_social_companions

    interface = args.interface or CONFIG.interface

    # Configured social channels run alongside the primary interface, not
    # instead of it (the dashboard stays primary; the bots ride along).
    companions = build_social_companions(main_actor, interface) if main_actor else []

    try:
        await _run_interface(args, system, main_actor, interface, companions)
    except StartupError:
        # The caller's to report: the command exits on it, a host program
        # catches it. Shutdown below still runs.
        raise
    except Exception:
        logger.exception("System error")
    finally:
        await _shut_down(system)


async def serve(
    agents: Iterable[Any] = (),
    *,
    pipelines_: Iterable[pipelines.Pipeline] = (),
    web: bool = True,
    minimal: bool = False,
    monitor_port: int | None = None,
    mqtt_broker: str | None = None,
    mqtt_port: int | None = None,
    llm: str | None = None,
    state_dir: "str | os.PathLike[str] | None" = None,
    interface: str | None = None,
    handle_signals: bool = False,
    configure_logging: bool = False,
) -> None:
    """Run Wactorz inside an event loop that is already running, with the agents it is given.

    For a notebook, a web framework or a ROS node that has a loop of its own:
    ``await wactorz.serve(...)``, or run it as a task and cancel that task to
    stop the system. ``agents`` are Actor subclasses or functions declared with
    :func:`wactorz.agent`; each is supervised beside the built-ins and shown on
    the dashboard, which ``web=False`` leaves off. ``pipelines_`` are what
    :func:`wactorz.pipeline` returned, though declaring one registers it
    already; the argument is for a pipeline built elsewhere. ``minimal=True``
    starts no orchestrator, catalogue or installer, so no model is needed: the
    monitor, the dashboard and the given agents only. The other arguments stand
    in for the command line's; ``state_dir`` is where everything durable is
    kept, as ``WACTORZ_STATE_DIR`` would say, and is set for this process only,
    not written to the environment. ``interface`` is the chat interface to
    run beside the dashboard (``"rest"``, ``"discord"``, ``"telegram"``,
    ``"whatsapp"``, ``"cli"``); by default there is none, so the system never
    reads the host's stdin.

    The agents and pipelines it is given are registered for this run only: a
    later ``serve`` in the same process starts what it is given, not what an
    earlier one was.

    It behaves as a library call: the host keeps its signals unless
    ``handle_signals`` says otherwise, its logging configuration is left alone
    unless ``configure_logging`` asks for the command's, and a configuration
    that cannot be started raises :class:`~wactorz.errors.StartupError` rather
    than exiting the process. Returns when the system has stopped.
    """
    previous = set_state_dir(state_dir) if state_dir is not None else None
    try:
        with plugins.registered(agents), pipelines.registered(pipelines_):
            args = serve_args(
                web=web,
                minimal=minimal,
                monitor_port=monitor_port,
                mqtt_broker=mqtt_broker,
                mqtt_port=mqtt_port,
                llm=llm,
                interface=interface or HEADLESS,
            )
            await app(args, handle_signals=handle_signals, configure_logging=configure_logging)
    finally:
        if state_dir is not None:
            set_state_dir(previous)


def run(
    agents: Iterable[Any] = (),
    *,
    pipelines_: Iterable[pipelines.Pipeline] = (),
    web: bool = True,
    minimal: bool = False,
    monitor_port: int | None = None,
    mqtt_broker: str | None = None,
    mqtt_port: int | None = None,
    llm: str | None = None,
    state_dir: "str | os.PathLike[str] | None" = None,
    interface: str | None = None,
) -> None:
    """Start Wactorz from a script: :func:`serve` on a loop of its own, until stopped.

    Takes the same arguments. Ctrl-C and SIGTERM stop it the way they stop the
    ``wactorz`` command, logging is set up the way the command sets it up, and
    the chat interface is the command's (``INTERFACE``) unless ``interface``
    names another: a script started from a terminal owns that terminal.
    A program that already has an event loop awaits :func:`serve` instead.
    Raises :class:`~wactorz.errors.StartupError` as :func:`serve` does.
    """
    try:
        asyncio.run(
            serve(
                agents,
                pipelines_=pipelines_,
                web=web,
                minimal=minimal,
                monitor_port=monitor_port,
                mqtt_broker=mqtt_broker,
                mqtt_port=mqtt_port,
                llm=llm,
                state_dir=state_dir,
                interface=interface or CONFIG.interface,
                handle_signals=True,
                configure_logging=True,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # An intentional stop; the actors were stopped on the way out.
        pass


def serve_args(
    *,
    web: bool = True,
    minimal: bool = False,
    monitor_port: int | None = None,
    mqtt_broker: str | None = None,
    mqtt_port: int | None = None,
    llm: str | None = None,
    interface: str | None = None,
) -> argparse.Namespace:
    """The settings :func:`serve`'s arguments stand for, in the form :func:`app` reads.

    The command line's defaults with these values set on top, so the two entry
    points agree on every setting without the library call having to spell
    its arguments as flags. Nothing is read from ``sys.argv``.
    """
    args = get_args([])
    args.no_monitor = not web
    args.minimal = minimal
    if monitor_port is not None:
        args.monitor_port = monitor_port
    if mqtt_broker is not None:
        args.mqtt_broker = mqtt_broker
    if mqtt_port is not None:
        args.mqtt_port = mqtt_port
    if llm is not None:
        args.llm = llm
    if interface is not None:
        args.interface = interface
    return args
