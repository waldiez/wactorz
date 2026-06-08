"""
MonitorActor — System health observer and user notifier.

Responsibilities:
  1. Heartbeat monitoring — detect unresponsive actors
  2. Error event subscription — receive structured errors from agents/{id}/errors
  3. Severity classification and user notification via MainActor
  4. Recovery status tracking (recovered / still degraded)

Single restart authority:
  The Supervisor (registry.py) is the ONLY component that restarts actors.
  The Monitor is a pure observer: it watches, classifies, and notifies the user.
  It never calls actor.stop() / actor.start() itself.

  Having two independent restart loops causes races:
    - Monitor kills an actor mid-LLM-fix → fix arrives to a dead actor
    - Supervisor sees FAILED and spawns a fresh copy at the same time
    - _consecutive_errors on the new instance resets to 0, confusing thresholds
    - Monitor's restart_attempts counter is keyed by actor_id, so it loses
      track across Supervisor-driven restarts (new instance = new actor_id)

  Separation of concerns: Supervisor heals, Monitor reports.
"""

import asyncio
import logging
import time
from typing import Optional

import psutil

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)

# How long a critical notification is suppressed before re-notifying (seconds)
_NOTIFY_COOLDOWN = 120.0


class MonitorActor(Actor):

    def __init__(
        self,
        check_interval:    float = 15.0,
        heartbeat_timeout: float = 60.0,
        auto_restart:      bool  = False,   # kept for API compat, ignored
        **kwargs,
    ):
        kwargs.setdefault("name", "monitor")
        super().__init__(**kwargs)
        self.check_interval    = check_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.protected         = True

        self._last_seen:      dict[str, float] = {}
        self._alert_state:    dict[str, bool]  = {}

        # Error event registry: actor_id -> latest error event dict
        self._error_registry: dict[str, dict]  = {}
        # Cooldown: actor_id -> last time we notified main
        self._last_notified:  dict[str, float] = {}

        # Cached Process object — cpu_percent(interval=None) tracks a delta
        # between consecutive calls on the SAME instance; creating a new one
        # each time always returns 0.0.
        self._proc = psutil.Process()

    async def on_start(self):
        if self._registry:
            now = time.time()
            for actor in self._registry.all_actors():
                if actor.actor_id != self.actor_id:
                    self._last_seen[actor.actor_id] = now

        # Prime the baseline on the cached instance so the first real reading
        # is meaningful (cpu_percent needs two samples on the same object).
        try:
            self._proc.cpu_percent(interval=None)
        except Exception:
            pass

        self._tasks.append(asyncio.create_task(self._monitor_loop()))
        logger.info(f"[{self.name}] Monitor started. check_interval={self.check_interval}s")

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        # Any message from an actor counts as a liveness signal
        if msg.sender_id and msg.sender_id != self.actor_id:
            self._last_seen[msg.sender_id] = time.time()
            if self._alert_state.get(msg.sender_id):
                logger.info(f"[{self.name}] Actor {msg.sender_id[:8]} recovered.")
                self._alert_state[msg.sender_id] = False

        # Structured error event forwarded from agents/{id}/errors
        if msg.type == MessageType.TASK and isinstance(msg.payload, dict):
            if msg.payload.get("_monitor_error_event"):
                await self._handle_error_event(msg.payload)

    # ── Monitor loop ───────────────────────────────────────────────────────

    async def _monitor_loop(self):
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(self.check_interval)
                await self._ping_all_actors()
                await self._check_all_actors()
                await self._check_error_registry()
                await self._publish_system_health()
                await self._publish_host_stats()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Monitor loop error: {e}")

    async def _ping_all_actors(self):
        if not self._registry:
            return
        for actor in self._registry.all_actors():
            if actor.actor_id != self.actor_id:
                try:
                    await self.send(actor.actor_id, MessageType.STATUS_REQUEST, None)
                except Exception:
                    pass

    async def _check_all_actors(self):
        if not self._registry:
            return
        now = time.time()
        for actor in self._registry.all_actors():
            if actor.actor_id == self.actor_id:
                continue
            if actor.actor_id not in self._last_seen:
                self._last_seen[actor.actor_id] = now
                continue

            # Use heartbeat timestamp as stronger liveness signal when available
            if actor.state == ActorState.RUNNING:
                start_age = now - (actor.metrics.start_time or now)
                if start_age < self.heartbeat_timeout:
                    self._last_seen[actor.actor_id] = max(
                        self._last_seen[actor.actor_id], now - start_age
                    )
            hb = getattr(actor.metrics, "last_heartbeat", None)
            if hb and hb > self._last_seen.get(actor.actor_id, 0):
                self._last_seen[actor.actor_id] = hb

            gap = now - self._last_seen[actor.actor_id]
            if gap > self.heartbeat_timeout and actor.state == ActorState.RUNNING:
                if not self._alert_state.get(actor.actor_id):
                    self._alert_state[actor.actor_id] = True
                    await self._fire_heartbeat_alert(actor, gap)
                    # No restart here — Supervisor's heartbeat-silence detector
                    # (HEARTBEAT_TIMEOUT=35s) will catch this independently.
            else:
                if self._alert_state.get(actor.actor_id) and gap <= self.heartbeat_timeout:
                    self._alert_state[actor.actor_id] = False

    # ── Error event handling ───────────────────────────────────────────────

    async def _handle_error_event(self, event: dict):
        """
        Observer-only: classify severity, fire MQTT alert, notify user.
        The Supervisor is the single restart authority — never call
        actor.stop() / actor.start() here.
        """
        actor_id = event.get("actor_id", "")
        name     = event.get("name", actor_id[:8])
        phase    = event.get("phase", "unknown")
        error    = event.get("error", "")
        severity = event.get("severity", "warning")
        fatal    = event.get("fatal", False)
        degraded = event.get("degraded", False)
        consec   = event.get("consecutive", 1)

        self._error_registry[actor_id] = event

        logger.warning(
            f"[{self.name}] Error event from '{name}': "
            f"phase={phase} severity={severity} consecutive={consec}"
        )

        # Always fire low-level MQTT alert for dashboards
        await self._fire_error_alert(event)

        # ── Notify user (cooldown-gated) ───────────────────────────────────
        if fatal:
            msg = (
                f"**{name}** failed during *{phase}* and cannot run: `{error}`. "
                f"The Supervisor will retry if budget allows; otherwise the agent "
                f"needs its code fixed."
            )
            await self._notify_main(actor_id, name, msg, severity="critical")

        elif severity == "critical" or degraded:
            msg = (
                f"**{name}** is crashing in *{phase}* ({consec}x): `{error}`. "
                f"The Supervisor is handling restarts automatically."
            )
            await self._notify_main(actor_id, name, msg, severity="critical")

        # severity == "warning" -> just the MQTT alert, no user notification

    async def _check_error_registry(self):
        """Notify user when a previously degraded agent has recovered."""
        for actor_id, event in list(self._error_registry.items()):
            actor = self._find_actor(actor_id)
            name  = event.get("name", actor_id[:8])
            if actor and hasattr(actor, "_consecutive_errors") and actor._consecutive_errors == 0:
                del self._error_registry[actor_id]
                await self._notify_main(
                    actor_id, name,
                    f"**{name}** has recovered and is running normally again. ✅",
                    severity="info",
                )

    # ── User notification ──────────────────────────────────────────────────

    async def _notify_main(
        self,
        actor_id: str,
        agent_name: str,
        message: str,
        severity: str = "warning",
    ):
        now = time.time()
        cooldown = self._last_notified.get(actor_id, 0)
        if (now - cooldown) < _NOTIFY_COOLDOWN and severity != "info":
            return

        self._last_notified[actor_id] = now

        if not self._registry:
            return
        main = self._registry.find_by_name("main")
        if not main:
            return

        try:
            await self.send(main.actor_id, MessageType.TASK, {
                "_monitor_notification": True,
                "agent_name":  agent_name,
                "message":     message,
                "severity":    severity,
                "timestamp":   now,
            })
            logger.info(f"[{self.name}] Notified main about '{agent_name}': {message[:80]}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to notify main: {e}")

    # ── Alerting ───────────────────────────────────────────────────────────

    async def _fire_heartbeat_alert(self, actor: Actor, gap: float):
        alert = {
            "actor_id":      actor.actor_id,
            "name":          actor.name,
            "last_seen_ago": gap,
            "state":         actor.state.value,
            "timestamp":     time.time(),
            "severity":      "warning" if gap < 120 else "critical",
        }
        logger.warning(f"[{self.name}] ALERT: {actor.name} unresponsive for {gap:.0f}s")
        await self._mqtt_publish(f"agents/{actor.actor_id}/alert", alert)

        _infra = {"monitor", "installer", "main", "code-agent",
                  "anomaly-detector", "home-assistant-agent"}
        if actor.name not in _infra:
            await self._notify_main(
                actor.actor_id,
                actor.name,
                f"**{actor.name}** has been unresponsive for {gap:.0f}s — "
                f"the Supervisor will restart it if it stays silent.",
                severity="warning",
            )

    async def _fire_error_alert(self, event: dict):
        await self._mqtt_publish(
            f"agents/{event.get('actor_id', 'unknown')}/alert",
            {
                "actor_id":  event.get("actor_id"),
                "name":      event.get("name"),
                "message":   f"[{event.get('phase')}] {event.get('error')}",
                "severity":  event.get("severity", "warning"),
                "timestamp": time.time(),
            },
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _find_actor(self, actor_id: str) -> Optional[Actor]:
        if not self._registry:
            return None
        for a in self._registry.all_actors():
            if a.actor_id == actor_id:
                return a
        return None

    async def _publish_system_health(self):
        if not self._registry:
            return
        now    = time.time()
        actors = self._registry.all_actors()
        health = {
            "timestamp":    now,
            "total_actors": len(actors),
            "running":  sum(1 for a in actors if a.state == ActorState.RUNNING),
            "stopped":  sum(1 for a in actors if a.state == ActorState.STOPPED),
            "failed":   sum(1 for a in actors if a.state == ActorState.FAILED),
            "degraded": len(self._error_registry),
            "actors": [
                {
                    "id":            a.actor_id,
                    "name":          a.name,
                    "state":         a.state.value,
                    "last_seen_ago": now - self._last_seen.get(a.actor_id, now),
                    "consecutive_errors": getattr(a, "_consecutive_errors", 0),
                    "error_phase":        getattr(a, "_error_phase", ""),
                }
                for a in actors
            ],
        }
        await self._mqtt_publish("system/health", health)

    async def _publish_host_stats(self):
        try:
            cpu_pct = self._proc.cpu_percent(interval=None)
            mem_info = self._proc.memory_info()
            mem_used_mb = mem_info.rss / 1024 / 1024
            mem_total_mb = psutil.virtual_memory().total / 1024 / 1024
            stats = {
                "cpu":          cpu_pct,
                "mem_used_mb":  mem_used_mb,
                "mem_total_mb": mem_total_mb,
                "timestamp":    time.time(),
            }
            await self._mqtt_publish("system/host", stats)
        except Exception as e:
            logger.debug(f"[{self.name}] host stats error: {e}")