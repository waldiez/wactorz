"""Getting something out: publishing to MQTT, delegating, alerting, logging."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...core.actor import MessageType
from ...core.mqtt import mqtt_client
from ..lookup import find_main_actor

if TYPE_CHECKING:
    from .hosts import ApiHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = ApiHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class MessagingMixin(_Host):
    """Mixed into AgentAPI; reads the actor through `self._actor`."""

    async def publish(self, topic: str, data: Any):
        """Publish data to an MQTT topic. Auto-registers topic in capability manifest
        and TopicBus contract so the agent is discoverable without explicit declare_contract().
        On every publish, captures the actual payload schema (field names + types)
        so the planner and other agents know the real field names — not guesses.
        """
        await self._actor._mqtt_publish(topic, data)

        is_new_topic = topic not in self._published_topics

        # ── Auto-capture observed schema from real payloads ────────────────
        # This solves the "temp" vs "temperature" vocabulary mismatch:
        # the schema reflects what the code ACTUALLY publishes.
        # Uses TopicContract.update_observed() — a proper dataclass field,
        # not monkey-patched attributes.
        try:
            from ...core.topic_bus import TopicContract, get_topic_bus

            bus = get_topic_bus()
            if bus:
                existing = bus.registry.get(self.name)
                if existing:
                    if is_new_topic and topic not in existing.publishes:
                        existing.publishes.append(topic)
                    # Record actual field names on every publish (first call
                    # per topic populates; subsequent calls are no-ops if
                    # fields haven't changed, but cheap either way)
                    if isinstance(data, dict):
                        existing.update_observed(topic, data)
                        # Also keep produces_schema in sync
                        for k, v in (
                            existing.observed_samples.get(topic, {}).get("fields", {}).items()
                        ):
                            existing.produces_schema[k] = v
                    bus.registry.register(existing)
                elif is_new_topic:
                    # Create minimal contract from published topics
                    contract = TopicContract(
                        name=self.name,
                        publishes=list(self._published_topics | {topic}),
                        actor_id=self.actor_id,
                        node=getattr(self._actor, "_node", None),
                    )
                    if isinstance(data, dict):
                        contract.update_observed(topic, data)
                        # Bootstrap produces_schema from observed
                        contract.produces_schema = dict(
                            contract.observed_samples.get(topic, {}).get("fields", {})
                        )
                    bus.register_contract(contract)
        except Exception:
            pass  # TopicBus unavailable — not fatal

        if is_new_topic:
            self._published_topics.add(topic)
            await self._publish_manifest()

    async def publish_detection(self, data: Any):
        """Convenience: publish to agents/{id}/detections"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/detections", data)

    async def publish_result(self, data: Any):
        """Convenience: publish to agents/{id}/result"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/result", data)

    async def log(self, message: str, level: str = "info"):
        """Add a message to the event log visible in the dashboard."""
        # Encode safely for Windows terminals that can't handle all unicode
        safe_msg = message.encode("ascii", errors="replace").decode("ascii")
        getattr(logger, level, logger.info)(f"[{self.name}] {safe_msg}")
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )

    async def alert(self, message: str, severity: str = "warning"):
        """Trigger an alert visible in the dashboard."""
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/alert",
            {
                "actor_id": self._actor.actor_id,
                "name": self.name,
                "message": message,
                "severity": severity,
                "timestamp": time.time(),
            },
        )

    async def notify_user(self, text: str):
        """Push a user-facing chat message to the chat panel (see Actor.notify_user).
        Use this — not log() or alert() — when the user should see the message in
        chat, e.g. when a long task finishes or an autonomous agent has news.
        """
        await self._actor.notify_user(text)

    async def send_to(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any | None:
        """Send a TASK to another agent by name and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox
          2. Remote node via MQTT — agents/by-name/{name}/task with reply topic
          3. Returns error dict if the agent is unknown in both

        Works with local DynamicAgent/LLMAgent AND remote _RemoteAgent on any node.
        """
        registry = self._actor._registry
        if not registry:
            logger.warning("[%s] send_to: no registry", self.name)
            return None

        target = registry.find_by_name(agent_name)

        if target:
            # ── Local path ────────────────────────────────────────────────────
            import uuid as _uuid

            task_id = str(_uuid.uuid4())[:8]
            future = asyncio.get_event_loop().create_future()
            self._actor._result_futures[task_id] = future
            if not isinstance(payload, dict):
                payload = {"message": payload, "text": str(payload)}
            payload = dict(payload)
            payload["_task_id"] = task_id
            payload["_reply_to"] = self._actor.actor_id
            await self._actor.send(target.actor_id, MessageType.TASK, payload)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] send_to '%s' timed out after %ss", self.name, agent_name, timeout
                )
                return {"error": f"Timeout waiting for '{agent_name}'"}
            finally:
                self._actor._result_futures.pop(task_id, None)

        # ── Remote path: find agent on a known node ───────────────────────────
        remote_node = None
        main = find_main_actor(registry)
        if main:
            for node_name, nd in main._known_nodes.items():
                if agent_name in nd.get("agents", []):
                    remote_node = node_name
                    break

        if not remote_node:
            logger.warning(
                "[%s] send_to: agent '%s' not found locally or remotely", self.name, agent_name
            )
            return {"error": f"Agent '{agent_name}' not found"}

        import uuid as _uuid

        reply_topic = f"agents/by-name/{self.name}/reply/{_uuid.uuid4().hex[:8]}"

        if not isinstance(payload, dict):
            payload = {"message": payload, "text": str(payload)}
        payload = dict(payload)
        payload["_reply_topic"] = reply_topic
        payload["_remote_task"] = True

        future = asyncio.get_event_loop().create_future()
        if not hasattr(self._actor, "_result_futures"):
            self._actor._result_futures = {}
        self._actor._result_futures[reply_topic] = future

        await self._actor._mqtt_publish(f"agents/by-name/{agent_name}/task", payload)

        async def _wait_reply():
            try:
                broker = getattr(self._actor, "_mqtt_broker", "localhost")
                port = getattr(self._actor, "_mqtt_port", 1883)
                async with mqtt_client(broker, port) as client:
                    await client.subscribe(reply_topic)
                    async for msg in client.messages:
                        try:
                            import json as _json

                            data = _json.loads(msg.payload.decode())
                            if not future.done():
                                future.set_result(data)
                        except Exception:
                            pass
                        return
            except Exception as e:
                if not future.done():
                    future.set_exception(e)

        reply_task = asyncio.create_task(_wait_reply())
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] send_to '%s' on '%s' timed out after %ss",
                self.name,
                agent_name,
                remote_node,
                timeout,
            )
            return {"error": f"Timeout waiting for remote '{agent_name}'"}
        finally:
            reply_task.cancel()
            self._actor._result_futures.pop(reply_topic, None)

    async def send_to_many(self, tasks: list[tuple[str, Any]], timeout: float = 60.0) -> list:
        """Send tasks to multiple agents IN PARALLEL and collect all results.

        tasks: list of (agent_name, payload) tuples
        Returns list of results in the same order.

        Example:
            results = await agent.send_to_many([
                ("weather-agent", {"city": "Athens"}),
                ("news-agent",    {"topic": "AI"}),
            ])
            weather, news = results[0], results[1]
        """
        coros = [self.send_to(name, payload, timeout) for name, payload in tasks]
        return list(await asyncio.gather(*coros, return_exceptions=True))

    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any | None:
        """Alias for send_to() — cleaner name for planner/coordinator agents."""
        return await self.send_to(agent_name, payload, timeout=timeout)
