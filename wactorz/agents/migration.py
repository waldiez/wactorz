"""Bringing an agent back from the node that was running it.

A migration home asks the node for everything it holds — the spawn config and
the persistent state — and spawns the agent locally from what comes back.

**The config in that message is executed**: it carries the agent's code. The
token is what makes that acceptable. Main mints one per migration it starts, and
only a message quoting it is acted on; it is consumed on use and expires, so a
replayed message spawns nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ..core.mqtt import mqtt_client

if TYPE_CHECKING:
    from .nodes import NodeHost

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: How long a migration's return token stays usable.
#:
#: A node that never answers would otherwise leave its token in the pending map
#: for the life of the process. Five minutes is far longer than a migration
#: takes and short enough that the map stays small.
TOKEN_TTL_S = 300.0

#: Identifies the code synthesized for an LLM agent sent out to a node, so it
#: can be dropped when the agent comes back. Hand-written code never has it.
BRIDGE_CODE_MARKER = "Auto-generated LLM bridge"


class Migration:
    """Agents in flight between main and a node."""

    def __init__(self, host: NodeHost | None = None) -> None:
        self.host = host
        #: return token -> the migration main started and is waiting on.
        self.pending_returns: dict[str, dict[str, Any]] = {}

    async def state_return_listener(self) -> None:
        """Follow `nodes/+/state_return` until the actor stops."""
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(host._mqtt_broker, host._mqtt_port) as client:
                    await client.subscribe("nodes/+/state_return")
                    logger.info("[main] Subscribed to state_return topics.")
                    last_error = None
                    async for message in client.messages:
                        await self.receive_state_return(str(message.topic), message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] state_return listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] state_return listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def receive_state_return(self, topic: str, payload: bytes | None) -> None:
        """Take an agent back from the node that was running it.

        The config in this message is executed — it carries the agent's code.
        The token is what makes that acceptable: main mints one per migration it
        starts, and only a message quoting it is acted on. Consumed on use, so a
        replay finds nothing waiting.
        """
        if not payload:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return

        self._expire_tokens()
        token = data.get("return_token", "")
        if not token or token not in self.pending_returns:
            logger.warning(
                "[main] state_return with unknown/expired token %r from %s — ignoring",
                f"{token[:8]}…",
                topic,
            )
            return

        started = self.pending_returns.pop(token)
        agent_name = data.get("agent") or started.get("agent_name", "?")
        from_node = started.get("from_node", "?")
        cfg = data.get("config") or {}
        state = data.get("state") or {}

        if not cfg or not isinstance(cfg, dict):
            logger.warning(
                "[main] state_return for %r from %r has no config — cannot spawn locally",
                agent_name,
                from_node,
            )
            return

        await self._respawn_locally(agent_name, from_node, cfg, state)

    def _expire_tokens(self) -> None:
        """Forget tokens whose migration never completed."""
        now = time.time()
        for token, started in list(self.pending_returns.items()):
            if now - started.get("started_at", 0) > TOKEN_TTL_S:
                self.pending_returns.pop(token, None)

    @staticmethod
    def local_spawn_config(
        agent_name: str, cfg: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """The returned config, rewritten to run here.

        The node is dropped because the agent is coming home, and leaving it on
        would send it straight back. `replace` is set so a leftover local
        instance of the same name is swapped rather than colliding.

        An LLM agent loses the code synthesized for it on the way out: locally
        the type routes to the real class and the code is ignored, so keeping it
        would put something misleading in the spawn registry. The marker comment
        identifies it; hand-written code never carries one.
        """
        local_cfg = dict(cfg)
        local_cfg.pop("node", None)
        local_cfg.pop("_initial_state", None)
        if state:
            local_cfg["_initial_state"] = state
        local_cfg["replace"] = True
        local_cfg.setdefault("name", agent_name)
        if local_cfg.get("type") == "llm" and BRIDGE_CODE_MARKER in (local_cfg.get("code") or ""):
            local_cfg.pop("code", None)
        return local_cfg

    async def _respawn_locally(
        self,
        agent_name: str,
        from_node: str,
        cfg: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        """Spawn the returned agent here, and say whether it worked.

        A failure is announced rather than logged alone: the agent is now on
        neither machine, so silence loses it.
        """
        host = self.host
        if host is None:
            return
        local = self.local_spawn_config(agent_name, cfg, state)
        earned = host._restore_earned_trust(agent_name, local)
        logger.info(
            "[main] Received state_return for %r from %r (%s state key(s)) — spawning locally",
            agent_name,
            from_node,
            len(state) if isinstance(state, dict) else 0,
        )
        try:
            await host._spawn_from_config(local, save=True, from_registry=earned)
        except Exception as exc:
            logger.exception(
                "[main] Local re-spawn after state_return failed for %r: %s", agent_name, exc
            )
            self._announce(
                f"Migration of '{agent_name}' from '{from_node}' → local FAILED: {exc}", "warning"
            )
            return
        self._announce(f"Migration of '{agent_name}' from '{from_node}' → local succeeded.", "info")

    def _announce(self, message: str, severity: str) -> None:
        host = self.host
        if host is None:
            return
        host._queue_notification(
            {
                "_monitor_notification": True,
                "message": message,
                "severity": severity,
                "timestamp": time.time(),
            }
        )
