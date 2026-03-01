"""
MonitorActor - Watches all registered actors for health.
"""

import asyncio
import logging
import time
from typing import Optional

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)


class MonitorActor(Actor):

    def __init__(
        self,
        check_interval: float = 15.0,
        heartbeat_timeout: float = 60.0,  # 2x the heartbeat interval (10s) with headroom
        auto_restart: bool = False,
        **kwargs,
    ):
        kwargs.setdefault("name", "monitor")
        super().__init__(**kwargs)
        self.check_interval    = check_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.auto_restart      = auto_restart
        self.protected         = True

        self._last_seen:   dict[str, float] = {}
        self._alert_state: dict[str, bool]  = {}

    async def on_start(self):
        # Seed last_seen for all actors already running so we don't get
        # instant "inf" alerts on startup
        if self._registry:
            now = time.time()
            for actor in self._registry.all_actors():
                if actor.actor_id != self.actor_id:
                    self._last_seen[actor.actor_id] = now

        self._tasks.append(asyncio.create_task(self._monitor_loop()))
        logger.info(f"[{self.name}] Monitor started. check_interval={self.check_interval}s")

    async def handle_message(self, msg: Message):
        # Any message from an actor counts as "alive"
        if msg.sender_id and msg.sender_id != self.actor_id:
            self._last_seen[msg.sender_id] = time.time()
            if self._alert_state.get(msg.sender_id):
                logger.info(f"[{self.name}] Actor {msg.sender_id[:8]} recovered.")
                self._alert_state[msg.sender_id] = False

    async def _monitor_loop(self):
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(self.check_interval)
                await self._ping_all_actors()
                await self._check_all_actors()
                await self._publish_system_health()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Monitor loop error: {e}")

    async def _ping_all_actors(self):
        """Send a STATUS_REQUEST to every actor so they reply and update last_seen."""
        if self._registry is None:
            return
        for actor in self._registry.all_actors():
            if actor.actor_id != self.actor_id:
                try:
                    await self.send(actor.actor_id, MessageType.STATUS_REQUEST, None)
                except Exception:
                    pass

    async def _check_all_actors(self):
        if self._registry is None:
            return

        now = time.time()
        for actor in self._registry.all_actors():
            if actor.actor_id == self.actor_id:
                continue

            # Seed any actor we haven't seen yet — give them a full timeout window
            if actor.actor_id not in self._last_seen:
                self._last_seen[actor.actor_id] = now
                continue

            # Also update last_seen if the actor is RUNNING and was recently started
            # (covers race where actor starts after monitor's on_start seed)
            if actor.state == ActorState.RUNNING:
                start_age = now - (actor.metrics.start_time or now)
                if start_age < self.heartbeat_timeout:
                    # Actor just started — give it a full timeout window from now
                    self._last_seen[actor.actor_id] = max(
                        self._last_seen[actor.actor_id], now - start_age
                    )

            gap = now - self._last_seen[actor.actor_id]

            if gap > self.heartbeat_timeout and actor.state == ActorState.RUNNING:
                if not self._alert_state.get(actor.actor_id):
                    self._alert_state[actor.actor_id] = True
                    await self._fire_alert(actor, gap)
                    if self.auto_restart:
                        await self._attempt_restart(actor)
            else:
                if self._alert_state.get(actor.actor_id) and gap <= self.heartbeat_timeout:
                    self._alert_state[actor.actor_id] = False

    async def _fire_alert(self, actor: Actor, gap: float):
        alert = {
            "actor_id":     actor.actor_id,
            "name":         actor.name,
            "last_seen_ago": gap,
            "state":        actor.state.value,
            "timestamp":    time.time(),
            "severity":     "warning" if gap < 60 else "critical",
        }
        logger.warning(f"[{self.name}] ALERT: {actor.name} unresponsive for {gap:.0f}s")
        # Publish only to agent-specific topic — monitor_server aggregates into system/alerts
        await self._mqtt_publish(f"agents/{actor.actor_id}/alert", alert)

    async def _attempt_restart(self, actor: Actor):
        logger.info(f"[{self.name}] Attempting restart of {actor.name}")
        try:
            await actor.start()
        except Exception as e:
            logger.error(f"[{self.name}] Restart failed for {actor.name}: {e}")

    async def _publish_system_health(self):
        if self._registry is None:
            return
        now    = time.time()
        actors = self._registry.all_actors()
        health = {
            "timestamp":    now,
            "total_actors": len(actors),
            "running":  sum(1 for a in actors if a.state == ActorState.RUNNING),
            "stopped":  sum(1 for a in actors if a.state == ActorState.STOPPED),
            "failed":   sum(1 for a in actors if a.state == ActorState.FAILED),
            "actors": [
                {
                    "id":           a.actor_id,
                    "name":         a.name,
                    "state":        a.state.value,
                    "last_seen_ago": now - self._last_seen.get(a.actor_id, now),
                }
                for a in actors
            ],
        }
        await self._mqtt_publish("system/health", health)