"""
MonitorActor — System health watcher with error-event driven recovery.

Responsibilities:
  1. Heartbeat monitoring — detect unresponsive actors
  2. Error event subscription — receive structured errors from agents/{id}/errors
  3. Recovery decisions:
       - "warning"  → log + notify main
       - "critical" → attempt restart + notify main
       - "fatal"    → notify main, mark agent DEGRADED, do NOT restart (bad code)
  4. User notification — escalate to MainActor in plain language so the user hears
     about problems through the conversation, not just log files
"""

import asyncio
import logging
import time
from typing import Optional

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)

# How long a fatal/compile error is suppressed before re-notifying (seconds)
_NOTIFY_COOLDOWN = 120.0


class MonitorActor(Actor):

    def __init__(
        self,
        check_interval:    float = 15.0,
        heartbeat_timeout: float = 60.0,
        auto_restart:      bool  = False,
        **kwargs,
    ):
        kwargs.setdefault("name", "monitor")
        super().__init__(**kwargs)
        self.check_interval    = check_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.auto_restart      = auto_restart
        self.protected         = True

        self._last_seen:      dict[str, float] = {}
        self._alert_state:    dict[str, bool]  = {}

        # Error event registry: actor_id → latest error event dict
        self._error_registry: dict[str, dict]  = {}
        # Cooldown: actor_id → last time we notified main about it
        self._last_notified:  dict[str, float] = {}
        # Track which actors we've attempted to restart this session
        self._restart_attempts: dict[str, int] = {}

    async def on_start(self):
        if self._registry:
            now = time.time()
            for actor in self._registry.all_actors():
                if actor.actor_id != self.actor_id:
                    self._last_seen[actor.actor_id] = now

        self._tasks.append(asyncio.create_task(self._monitor_loop()))
        logger.info(f"[{self.name}] Monitor started. check_interval={self.check_interval}s")

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        # Heartbeat — any message counts as alive
        if msg.sender_id and msg.sender_id != self.actor_id:
            self._last_seen[msg.sender_id] = time.time()
            if self._alert_state.get(msg.sender_id):
                logger.info(f"[{self.name}] Actor {msg.sender_id[:8]} recovered.")
                self._alert_state[msg.sender_id] = False

        # Structured error event from agents/{id}/errors (routed via MQTT bridge)
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
            if actor.state == ActorState.RUNNING:
                start_age = now - (actor.metrics.start_time or now)
                if start_age < self.heartbeat_timeout:
                    self._last_seen[actor.actor_id] = max(
                        self._last_seen[actor.actor_id], now - start_age
                    )
            # Heartbeat fires every 10s — use as secondary liveness signal
            hb = getattr(actor.metrics, "last_heartbeat", None)
            if hb and hb > self._last_seen.get(actor.actor_id, 0):
                self._last_seen[actor.actor_id] = hb

            gap = now - self._last_seen[actor.actor_id]
            if gap > self.heartbeat_timeout and actor.state == ActorState.RUNNING:
                if not self._alert_state.get(actor.actor_id):
                    self._alert_state[actor.actor_id] = True
                    await self._fire_heartbeat_alert(actor, gap)
                    if self.auto_restart:
                        await self._attempt_restart(actor, reason="heartbeat timeout")
            else:
                if self._alert_state.get(actor.actor_id) and gap <= self.heartbeat_timeout:
                    self._alert_state[actor.actor_id] = False

    # ── Error event handling ───────────────────────────────────────────────

    async def _handle_error_event(self, event: dict):
        """
        Called when an agent publishes a structured error.
        Decides: log / restart / escalate to user.
        """
        actor_id = event.get("actor_id", "")
        name     = event.get("name", actor_id[:8])
        phase    = event.get("phase", "unknown")
        error    = event.get("error", "")
        severity = event.get("severity", "warning")
        fatal    = event.get("fatal", False)
        degraded = event.get("degraded", False)
        consec   = event.get("consecutive", 1)

        # Store in registry for health checks
        self._error_registry[actor_id] = event

        logger.warning(
            f"[{self.name}] Error event from '{name}': "
            f"phase={phase} severity={severity} consecutive={consec}"
        )

        # ── Recovery decision ──────────────────────────────────────────────
        if fatal:
            # Bad code / setup failure — restart won't help without a fix
            msg = (
                f"**{name}** failed during *{phase}* and cannot run: `{error}`. "
                f"The agent needs its code fixed before it can be used."
            )
            await self._notify_main(actor_id, name, msg, severity="critical")
            await self._fire_error_alert(event)

        elif severity == "critical" or degraded:
            # Repeated runtime errors — try a restart
            actor = self._find_actor(actor_id)
            if actor and self._restart_attempts.get(actor_id, 0) < 3:
                self._restart_attempts[actor_id] = self._restart_attempts.get(actor_id, 0) + 1
                restarted = await self._attempt_restart(actor, reason=f"{phase} error (attempt {self._restart_attempts[actor_id]})")
                if restarted:
                    msg = (
                        f"**{name}** kept crashing in *{phase}* ({consec}x), "
                        f"so I restarted it. Latest error: `{error}`."
                    )
                else:
                    msg = (
                        f"**{name}** is crashing repeatedly in *{phase}* "
                        f"and I couldn't restart it. Error: `{error}`."
                    )
            else:
                attempts = self._restart_attempts.get(actor_id, 0)
                msg = (
                    f"**{name}** has failed {consec} times in *{phase}* "
                    f"(restart attempted {attempts}x). Error: `{error}`. "
                    f"It may need its code fixed."
                )
            await self._notify_main(actor_id, name, msg, severity="critical")
            await self._fire_error_alert(event)

        else:
            # Single warning — log and let agent recover on its own
            await self._fire_error_alert(event)

    async def _check_error_registry(self):
        """Periodically re-notify main about persistently degraded agents."""
        now = time.time()
        for actor_id, event in list(self._error_registry.items()):
            last = self._last_notified.get(actor_id, 0)
            if event.get("degraded") and (now - last) > _NOTIFY_COOLDOWN:
                actor = self._find_actor(actor_id)
                name  = event.get("name", actor_id[:8])
                # If agent has recovered (error count reset), clean up registry
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
        """
        Send a structured notification to MainActor so it can relay to the user
        in natural language during their next interaction (or immediately if idle).
        """
        now = time.time()
        cooldown = self._last_notified.get(actor_id, 0)
        if (now - cooldown) < _NOTIFY_COOLDOWN and severity != "info":
            return   # Don't spam

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

        # Notify main only for user-spawned agents
        _infra = {"monitor", "installer", "main", "code-agent",
                  "anomaly-detector", "home-assistant-agent"}
        if actor.name not in _infra:
            await self._notify_main(
                actor.actor_id,
                actor.name,
                f"**{actor.name}** has been unresponsive for {gap:.0f}s.",
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

    # ── Restart ────────────────────────────────────────────────────────────

    async def _attempt_restart(self, actor: Actor, reason: str = "") -> bool:
        logger.info(f"[{self.name}] Restarting '{actor.name}' — reason: {reason}")
        try:
            if actor.state != ActorState.STOPPED:
                await actor.stop()
                await asyncio.sleep(0.5)
            await actor.start()
            self._last_seen[actor.actor_id] = time.time()
            logger.info(f"[{self.name}] '{actor.name}' restarted successfully.")
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Restart of '{actor.name}' failed: {e}")
            return False

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