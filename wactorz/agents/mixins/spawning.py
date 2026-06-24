"""
SpawnMixin — the single, shared implementation of local agent spawning.

Both ``MainActor`` and ``PlannerAgent`` previously carried their own near-copies
of this logic (``MainActor._spawn_from_config`` and ``PlannerAgent._spawn_agent``),
which had drifted apart: the planner's copy silently skipped migrated-state
injection, TopicContract auto-wiring, and the ``trusted`` safety flag. This mixin
collapses both into one superset implementation so a "dynamic agent" spawned by
the planner behaves identically to one spawned by main.

Design constraints that make this NOT a fully symmetric merge:

  * The spawn registry has a single owner — ``main``. Writing it must always
    land in main's persistence, never the caller's. So the registry write is a
    hook, ``_register_spawn``: when ``self`` defines ``_save_to_spawn_registry``
    (only ``MainActor`` does) it writes directly; otherwise it routes the config
    to main over the registry. This replaces the planner's old
    ``_register_with_main``.

  * The user's timezone lives in main's facts. ``_resolve_user_timezone`` reads
    ``self.get_user_facts()`` when present (main), else looks main up via the
    registry (planner). This replaces the planner's inline registry hop.

  * Remote / node spawning stays on ``MainActor`` (``_spawn_remote``) — the
    planner has no concept of nodes. ``MainActor._spawn_from_config`` keeps the
    ``node`` branch and delegates only the LOCAL path here.

Everything the mixin touches beyond those hooks is on the ``Actor`` base class
(``self.spawn``, ``self._registry``, ``self._persistence_dir``, ``self.send``,
``self.actor_id``, ``self._result_futures``) or set by both subclasses
(``self.llm``), so the mixin rests on a stable shared surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Any

from ...core.actor import Actor, MessageType

logger = logging.getLogger(__name__)


class _SpawnPlaceholder:
    """Returned when an agent is being installed+spawned in the background.

    Truthy stand-in so callers can report "spawning…" before the real actor
    exists. Carries only the name.
    """

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<_SpawnPlaceholder {self.name!r}>"


class SpawnMixin:
    """Shared local-spawn behaviour. Mix into any ``Actor`` subclass."""

    # ── Public entry point ─────────────────────────────────────────────────

    async def _spawn_local_from_config(
        self,
        config: dict,
        *,
        register: bool = True,
        blocking_install: bool = False,
    ) -> Optional[Actor]:
        """Spawn one agent locally from a spawn-config dict.

        Parameters
        ----------
        register:
            When True, record the config in main's spawn registry on success
            (via the ``_register_spawn`` hook) so it survives restarts.
        blocking_install:
            Dynamic agents only. When False (main's default), missing packages
            install in the BACKGROUND and a ``_SpawnPlaceholder`` is returned
            immediately. When True (the planner's pipeline path), the install
            blocks until complete and the real actor is returned — pipelines
            need the agent live before the next step runs.

        Returns the spawned ``Actor``, an existing actor (idempotent path), a
        ``_SpawnPlaceholder`` (background install), or ``None`` on failure.
        """
        name = config.get("name", "spawned-agent")

        # ── Idempotency / replace ──────────────────────────────────────────
        existing = self._find_existing(name)
        if existing is not None:
            if not config.get("replace", False):
                logger.info(
                    f"[{self.name}] '{name}' already exists "
                    f"(use replace=true to update)."
                )
                return existing
            await self._stop_for_replace(existing, name)

        agent_type    = config.get("type", "dynamic")
        code          = config.get("code", "").strip()
        system_prompt = config.get("system_prompt", "").strip()

        # ── Route to the right agent class ─────────────────────────────────
        if agent_type == "ha_actuator":
            actor = await self._spawn_ha_actuator(config, name)
        elif agent_type == "scheduled":
            actor = await self._spawn_scheduled_agent(config, name)
        elif agent_type == "llm" or (not code and system_prompt):
            # Implicit-llm route: a config with a system prompt but no code is
            # an LLM agent even if 'type' was left at the "dynamic" default.
            actor = await self._spawn_llm_agent(config, name)
        elif code:
            actor = await self._spawn_dynamic_agent(
                config, name, code, blocking_install=blocking_install
            )
        else:
            logger.warning(
                f"[{self.name}] Spawn config for '{name}' has neither code "
                f"nor system_prompt — nothing to spawn."
            )
            return None

        if actor is not None and register:
            self._register_spawn(config)

        return actor

    # ── Per-type spawn helpers ─────────────────────────────────────────────

    async def _spawn_ha_actuator(self, config: dict, name: str) -> Optional[Actor]:
        """Spawn a HomeAssistantActuatorAgent (type: ha_actuator).

        Collision handling is standardized on the agent NAME (matching every
        other agent type), not on automation_id. The original automation_id is
        preserved in the actuator config regardless of any name suffixing.
        """
        from ..home_assistant_actuator_agent import (
            HomeAssistantActuatorAgent,
            ActuatorConfig,
            ActuatorAction,
            ActuatorCondition,
        )

        if self._registry and self._registry.find_by_name(name):
            import hashlib
            import time
            suffix = hashlib.md5(f"{name}{time.time()}".encode()).hexdigest()[:4]
            name = f"{name}-{suffix}"

        actuator_cfg = ActuatorConfig(
            automation_id    = config.get("automation_id", name),
            description      = config.get("description", ""),
            mqtt_topics      = config.get("mqtt_topics", []),
            actions          = [ActuatorAction.from_dict(a) for a in config.get("actions", [])],
            conditions       = [ActuatorCondition.from_dict(c) for c in config.get("conditions", [])],
            detection_filter = config.get("detection_filter"),
            cooldown_seconds = float(config.get("cooldown_seconds", 10.0)),
        )
        logger.info(f"[{self.name}] Spawning HomeAssistantActuatorAgent '{name}'")
        return await self.spawn(
            HomeAssistantActuatorAgent,
            config          = actuator_cfg,
            name            = name,
            persistence_dir = str(self._persistence_dir.parent),
        )

    async def _spawn_scheduled_agent(self, config: dict, name: str) -> Optional[Actor]:
        """Spawn a ScheduledAgent. The schedule spec is validated in __init__,
        so a malformed config raises ValueError here, before registration runs.
        The user's preferred timezone is injected so "5pm" means 5pm where the
        user lives; an explicit 'tz' in the spec still wins downstream.
        """
        from ..scheduled_agent import ScheduledAgent

        schedule_spec = config.get("schedule")
        if not isinstance(schedule_spec, dict):
            logger.warning(
                f"[{self.name}] Cannot spawn '{name}': missing or invalid 'schedule' dict"
            )
            return None

        publish_topic = config.get("publish_topic") or f"schedule/{name}/fired"
        try:
            actor = await self.spawn(
                ScheduledAgent,
                name            = name,
                schedule        = schedule_spec,
                timezone        = self._resolve_user_timezone(),
                publish_topic   = publish_topic,
                description     = config.get("description", ""),
                persistence_dir = str(self._persistence_dir.parent),
            )
            logger.info(
                f"[{self.name}] Spawned ScheduledAgent '{name}' "
                f"({schedule_spec.get('type')} → {publish_topic})"
            )
            return actor
        except ValueError as e:
            logger.error(f"[{self.name}] Invalid schedule for '{name}': {e}")
            return None
        except Exception as e:
            logger.error(f"[{self.name}] Failed to spawn ScheduledAgent '{name}': {e}")
            return None

    async def _spawn_llm_agent(self, config: dict, name: str) -> Optional[Actor]:
        """Spawn an LLMAgent — chat, Q&A, reasoning. Applies any migrated state
        before spawn so on_start()'s recall() finds conversation_history etc.
        """
        await self._apply_initial_state(name, config)

        from ..llm_agent import LLMAgent
        logger.info(f"[{self.name}] Spawning LLM agent '{name}'")
        return await self.spawn(
            LLMAgent,
            name            = name,
            llm_provider    = self.llm,
            system_prompt   = config.get("system_prompt", "You are a helpful assistant."),
            persistence_dir = str(self._persistence_dir.parent),
        )

    async def _spawn_dynamic_agent(
        self, config: dict, name: str, code: str, *, blocking_install: bool = False
    ):
        """Spawn a DynamicAgent — sensors, pipelines, tools. If the config
        declares packages that aren't importable yet, either install in the
        background (default) and return a placeholder, or block until installed
        (``blocking_install=True``) and return the real actor.
        """
        packages = config.get("install", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split() if p.strip()]

        needed = self._packages_needing_install(packages) if packages else []

        if not needed:
            return await self._do_spawn_dynamic(config, name, code)

        if blocking_install:
            # Pipeline path: the next step may depend on this agent being live.
            logger.info(f"[{self.name}] Installing {needed} for '{name}' (blocking)…")
            await self._install_packages(needed, agent_name=name)
            return await self._do_spawn_dynamic(config, name, code)

        # Default path: don't block the response — install + spawn in background.
        logger.info(f"[{self.name}] Scheduling background install+spawn for '{name}': {needed}")
        asyncio.create_task(self._install_then_spawn(config, name, code, needed))
        return _SpawnPlaceholder(name)

    async def _install_then_spawn(self, config: dict, name: str, code: str, packages: list):
        """Background task: install packages, then spawn, then register.

        Emits the same dashboard log frames main historically published so the
        activity feed still shows background installs/spawns. ``_mqtt_publish``
        is on the Actor base; guarded so the mixin stays testable without it.
        """
        import time
        publish = getattr(self, "_mqtt_publish", None)
        try:
            if publish is not None:
                await publish(
                    f"agents/{self.actor_id}/logs",
                    {"type": "log", "message": f"Installing {packages} for {name}…",
                     "timestamp": time.time()},
                )
            await self._install_packages(packages, agent_name=name)
            actor = await self._do_spawn_dynamic(config, name, code)
            if actor is not None:
                self._register_spawn(config)
                if publish is not None:
                    await publish(
                        f"agents/{self.actor_id}/logs",
                        {"type": "spawned", "message": f"'{name}' spawned after install",
                         "child_name": name, "timestamp": time.time()},
                    )
                logger.info(f"[{self.name}] Background spawn complete: {name}")
        except Exception as e:
            logger.error(f"[{self.name}] Background install+spawn failed for '{name}': {e}")

    async def _do_spawn_dynamic(self, config: dict, name: str, code: str) -> Optional[Actor]:
        """Construct and start the DynamicAgent. Applies migrated state first,
        passes the ``trusted`` flag (catalog agents skip the safety validator),
        and registers a TopicContract when the config declares pub/sub topics so
        the agent is eligible for auto-wiring on the TopicBus.
        """
        await self._apply_initial_state(name, config)

        from ..dynamic_agent import DynamicAgent
        actor = await self.spawn(
            DynamicAgent,
            name            = name,
            code            = code,
            poll_interval   = float(config.get("poll_interval") or 1.0),
            description     = config.get("description", ""),
            input_schema    = config.get("input_schema", {}),
            output_schema   = config.get("output_schema", {}),
            llm_provider    = self.llm,
            persistence_dir = str(self._persistence_dir.parent),
            trusted         = bool(config.get("trusted", False)),
        )

        if actor is not None and (config.get("publishes") or config.get("subscribes")):
            try:
                from ...core.topic_bus import TopicContract, get_topic_bus
                contract = TopicContract.from_spawn_config({**config, "actor_id": actor.actor_id})
                bus = get_topic_bus()
                if bus:
                    bus.register_contract(contract)
                    logger.info(
                        f"[{self.name}] Registered TopicContract for '{name}': "
                        f"pub={contract.publishes} sub={contract.subscribes}"
                    )
            except Exception as e:
                logger.debug(f"[{self.name}] TopicContract registration skipped: {e}")

        return actor

    # ── Package install ────────────────────────────────────────────────────

    @staticmethod
    def _packages_needing_install(packages: list[str]) -> list[str]:
        """Subset of ``packages`` not already importable in this process. The
        import name often differs from the pip name (opencv-python → cv2), so
        this is a heuristic; re-installing an present package is a cheap no-op.
        """
        import importlib
        needed = []
        for pkg in packages:
            import_name = pkg.replace("-", "_").split("[")[0]
            try:
                importlib.import_module(import_name)
            except ImportError:
                needed.append(pkg)
        return needed

    async def _install_packages(self, packages: list[str], agent_name: str = "?") -> None:
        """Install packages by delegating to the 'installer' agent and blocking
        until it replies (or a 120s timeout). No-op when nothing is needed or
        the registry/installer is unavailable.
        """
        if not packages or not self._registry:
            return

        needed = self._packages_needing_install(packages)
        if not needed:
            logger.info(
                f"[{self.name}] All packages for '{agent_name}' already available: {packages}"
            )
            return

        installer = self._registry.find_by_name("installer")
        if not installer:
            logger.warning(
                f"[{self.name}] installer agent not found — cannot install {needed} "
                f"for '{agent_name}'. Agent may crash on import."
            )
            return

        import uuid
        task_id = f"install_{uuid.uuid4().hex[:8]}"
        future = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future
        try:
            logger.info(f"[{self.name}] Installing {needed} for '{agent_name}' via installer…")
            await self.send(installer.actor_id, MessageType.TASK, {
                "action":   "install",
                "packages": needed,
                "task":     task_id,
                "_task_id": task_id,
                "reply_to": self.actor_id,
            })
            try:
                result = await asyncio.wait_for(future, timeout=120.0)
                logger.info(
                    f"[{self.name}] Install result for '{agent_name}': "
                    f"{result.get('message', result)}"
                )
                if result.get("failed"):
                    logger.warning(
                        f"[{self.name}] Failed to install: {result['failed']} "
                        f"— '{agent_name}' may not work correctly"
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{self.name}] Install timed out for {needed} — proceeding anyway; "
                    f"'{agent_name}' may crash on import"
                )
        finally:
            self._result_futures.pop(task_id, None)

    # ── Migrated state ─────────────────────────────────────────────────────

    async def _apply_initial_state(self, name: str, config: dict) -> None:
        """Write a migrated state snapshot into the per-agent persistence layer
        BEFORE the agent spawns, so its on_start()/recall() sees the shipped
        conversation_history / counters. Pops ``_initial_state`` from the config
        so it isn't persisted into the spawn registry. No-op without a snapshot.
        """
        snapshot = config.pop("_initial_state", None)
        if not snapshot or not isinstance(snapshot, dict):
            return

        try:
            from ...core.persistence import (
                PersistenceAPI, get_db, get_redis, get_pickle_store,
            )
        except Exception as e:
            logger.debug(
                f"[{self.name}] PersistenceAPI not importable — legacy state "
                f"injection for '{name}': {e}"
            )
            try:
                from pathlib import Path
                import pickle
                safe = name.replace("/", "_").replace("\\", "_")
                pdir = Path(str(self._persistence_dir.parent)) / safe
                pdir.mkdir(parents=True, exist_ok=True)
                with open(pdir / "state.pkl", "wb") as fh:
                    pickle.dump(snapshot, fh)
                logger.info(
                    f"[{self.name}] Wrote {len(snapshot)} migrated key(s) to "
                    f"{pdir / 'state.pkl'} for '{name}' (legacy path)"
                )
            except Exception as e2:
                logger.warning(f"[{self.name}] Legacy state injection failed for '{name}': {e2}")
            return

        db, redis, pkl = get_db(), get_redis(), get_pickle_store()
        if not (db and redis and pkl):
            logger.warning(
                f"[{self.name}] PersistenceAPI stores not initialised — "
                f"cannot apply migrated state for '{name}'"
            )
            return

        api = PersistenceAPI(db, redis, pkl, name)
        applied = api.load_snapshot(snapshot, replace=True)
        logger.info(
            f"[{self.name}] Applied migrated state for '{name}': "
            f"{applied['sqlite']} SQLite, {applied['redis']} Redis, "
            f"{applied['pickle']} pickle key(s)"
        )

    # ── Hooks / shared helpers ─────────────────────────────────────────────

    def _find_existing(self, name: str) -> Optional[Actor]:
        """Return a live actor for ``name`` if one exists, consulting both the
        registry and the supervisor's in-flight specs (which catch a spawn whose
        registration hasn't completed during a parallel-startup race).
        """
        existing = self._registry.find_by_name(name) if self._registry else None
        if existing is None and self._registry is not None:
            sup = getattr(self._registry, "_supervisor_ref", None)
            if sup is not None:
                spec = getattr(sup, "_specs", {}).get(name)
                if (
                    spec is not None
                    and getattr(spec, "actor", None) is not None
                    and not getattr(spec, "retired", False)
                ):
                    existing = spec.actor
        return existing

    async def _stop_for_replace(self, existing: Actor, name: str) -> None:
        """Stop and unregister an existing agent before spawning its
        replacement. The new agent reuses the same deterministic actor_id and
        republishes its manifest, so the retained MQTT topic is left intact.
        """
        logger.info(f"[{self.name}] Replacing '{name}' with updated code…")
        try:
            if self._registry:
                await self._registry.unregister(existing.actor_id)
            await existing.stop()
            # Drop the cached manifest so a list query in the brief window before
            # the new agent republishes doesn't show stale data. Only MainActor
            # has this cache; guard for peers (planner) that don't.
            manifests = getattr(self, "_agent_manifests", None)
            if manifests is not None:
                manifests.pop(name, None)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"[{self.name}] Error stopping old '{name}': {e}")

    def _resolve_user_timezone(self) -> Optional[str]:
        """Resolve the user's preferred timezone. MainActor owns the facts and
        reads them directly; peers (planner) read them from main via the
        registry. Returns None when unavailable.
        """
        own_facts = getattr(self, "get_user_facts", None)
        if callable(own_facts):
            try:
                return own_facts().get("pref_timezone")
            except Exception:
                return None
        if self._registry is not None:
            main = self._registry.find_by_name("main")
            if main is not None and hasattr(main, "get_user_facts"):
                try:
                    return main.get_user_facts().get("pref_timezone")
                except Exception:
                    return None
        return None

    def _register_spawn(self, config: dict) -> None:
        """Record a spawn in main's spawn registry so it survives restarts.

        The registry is main-owned: ``MainActor`` defines
        ``_save_to_spawn_registry`` and writes directly; any other actor routes
        the config to main over the registry. (Replaces the planner's old
        ``_register_with_main``.)
        """
        own_saver = getattr(self, "_save_to_spawn_registry", None)
        if callable(own_saver):
            own_saver(config)
            return
        if self._registry is None:
            return
        main = self._registry.find_by_name("main")
        if main is not None and hasattr(main, "_save_to_spawn_registry"):
            main._save_to_spawn_registry(config)
            logger.info(
                f"[{self.name}] Registered '{config.get('name')}' with main's spawn registry"
            )