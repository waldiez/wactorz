"""Keeping the spawn registry in step with a program a node repaired.

An agent whose code fails can be repaired by the LLM while it runs. On a node
that repair lives on the node: the registry entry main would rebuild the agent
from still holds the program that failed, so a node that reboots is sent the
break again and pays to fix it a second time.

The obvious fix — the node publishes its new code and main files it — is the one
this must not do. Main executes what a node sends it, so an unsolicited write
would let a single compromised node put code into the registry for any agent it
hosts, silently, and that code runs wherever the agent goes next. It is the
lateral path node-to-node migration was removed to close.

So the node volunteers a *notice* and never the program:

    node → nodes/<node>/code_changed   {agent}            a hint, no code
    main → nodes/<node>/code_request   {agent, token}     signed, main-initiated
    node → nodes/<node>/code_return    {agent, token, code}

Main acts on the last one only for a token it minted, has not yet spent and has
not let expire — the same rule :mod:`.migration` follows for the config a
returning agent carries. A forged notice therefore buys nothing: main asks the
node, and the node answers with what it is really running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)

if TYPE_CHECKING:
    from .hosts import CodeRefreshHost

logger = logging.getLogger(__name__)

#: How long a question stays open. A node answers in the time one MQTT round
#: trip takes; this is long enough for one that is busy or briefly away, and
#: short enough that an unanswered question does not sit in the map.
TOKEN_TTL_S = 120.0

#: How long to wait before reconnecting a dropped subscription.
RECONNECT_DELAY_S = 5.0

#: How often main will ask about the same agent. A notice is unauthenticated —
#: anything on the broker can send one — and each question main asks is a signed
#: message, which `node_signing.next_sequence` records to disk before sending,
#: on the stated assumption that control messages "come at an operator's pace".
#: Nothing else main sends is driven by a node, so without a floor here a
#: spammer sets that pace. Ten seconds is below any real repair, which costs an
#: LLM round trip first, and far above a flood.
ASK_INTERVAL_S = 10.0


class CodeRefresh:
    """Asks a node for a repaired program, and files the answer."""

    def __init__(self, host: CodeRefreshHost | None = None) -> None:
        self.host = host
        #: token -> the question main asked and is waiting on.
        self.pending: dict[str, dict[str, Any]] = {}
        #: (node, agent) -> when main last asked, so a flood of notices about
        #: one agent does not become a flood of questions. Keyed by pairs that
        #: are in the spawn registry, so it is bounded by the agents that exist.
        self._last_asked: dict[tuple[str, str], float] = {}

    # ── Asking ────────────────────────────────────────────────────────────

    async def note_change(self, node: str, agent_name: str) -> None:
        """A node says an agent's program changed. Ask it what the program is.

        The notice is not trusted and not required to be true. What it does is
        make main ask, which is the step that makes the answer actionable.
        """
        host = self.host
        if host is None or not node or not agent_name:
            return
        if not self._is_ours(node, agent_name):
            logger.warning(
                "[%s] %r says %r changed, but that is not an agent main placed there — ignoring",
                host.name,
                node,
                agent_name,
            )
            return

        self._expire()
        if not self._worth_asking(node, agent_name):
            return
        now = time.time()
        self._last_asked[node, agent_name] = now
        token = secrets.token_hex(8)
        self.pending[token] = {
            "agent": agent_name,
            "node": node,
            "asked_at": now,
        }
        # Signed on the way out, because it is a node control topic: see
        # `MainActor._publish_properties`.
        await host._mqtt_publish(
            f"nodes/{node}/code_request",
            {"agent": agent_name, "token": token, "timestamp": time.time()},
            qos=1,
        )

    def _worth_asking(self, node: str, agent_name: str) -> bool:
        """Whether to ask about this agent now, or let a question already do.

        Two reasons not to. One is already open, so a second would race it and
        one of the two answers would be refused as the odd one out. Or the last
        was very recent, which no real sequence of repairs produces — each costs
        an LLM round trip — but a stream of forged notices does.
        """
        if any(
            asked["node"] == node and asked["agent"] == agent_name
            for asked in self.pending.values()
        ):
            logger.debug("[code] Already asking %r about %r", node, agent_name)
            return False
        since = time.time() - self._last_asked.get((node, agent_name), 0.0)
        if since < ASK_INTERVAL_S:
            logger.debug("[code] Asked %r about %r %.1fs ago — leaving it", node, agent_name, since)
            return False
        return True

    def _is_ours(self, node: str, agent_name: str) -> bool:
        """Whether the registry says main put this agent on that node.

        The answer will overwrite a registry entry, so the entry has to exist
        and has to name the node that is offering to change it. A node cannot
        speak for an agent on another node, or for one main never placed.
        """
        host = self.host
        if host is None:
            return False
        entry = host._get_spawn_registry().get(agent_name)
        return bool(entry) and (entry.get("node") or "") == node

    # ── Answering ─────────────────────────────────────────────────────────

    async def receive_code_return(self, topic: str, payload: bytes | None) -> None:
        """File the program a node sent, if main asked for it.

        The code here is executed the next time the agent is spawned. The token
        is what makes that acceptable: main minted it for one question about one
        agent on one node, and spends it here so a replay finds nothing.
        """
        host = self.host
        if host is None or not payload:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return

        self._expire()
        token = str(data.get("token") or "")
        asked = self.pending.get(token)
        if asked is None:
            logger.warning(
                "[%s] code_return on %s with an unknown or expired token — ignoring",
                host.name,
                topic,
            )
            return

        # The answer has to be to the question that was asked: same agent, same
        # node. A token is for one of each, so one that arrives naming something
        # else is not the answer to it.
        #
        # Checked before the token is spent, and that order is the point. The
        # token travels in the request, in the clear, so anyone who can read the
        # node's topics can quote it back. Spending it on a misaddressed answer
        # would let them cancel the exchange: main would have no question left
        # and would refuse the node's real answer a moment later. Dropped
        # without spending, the question stands and the right answer still lands.
        node = topic.split("/")[1] if "/" in topic else ""
        if data.get("agent") != asked["agent"] or node != asked["node"]:
            logger.warning(
                "[%s] code_return quotes a token for %r on %r but names %r on %r — ignoring",
                host.name,
                asked["agent"],
                asked["node"],
                data.get("agent"),
                node,
            )
            return

        # Addressed correctly, so this is the answer — spent whether or not it
        # turns out to carry a usable program. There is nothing to ask again:
        # the node said what it is running, and the next repair brings the next
        # notice.
        self.pending.pop(token, None)

        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            return

        entry = dict(host._get_spawn_registry().get(asked["agent"]) or {})
        if not entry or entry.get("code") == code:
            return
        entry["code"] = code
        entry["_code_fixed_at"] = time.time()
        host._save_to_spawn_registry(entry)
        logger.info(
            "[%s] %r on %r repaired itself; its registry entry now holds the program it runs.",
            host.name,
            asked["agent"],
            asked["node"],
        )

    def _expire(self) -> None:
        """Drop questions a node never answered."""
        now = time.time()
        for token, asked in list(self.pending.items()):
            if now - asked.get("asked_at", 0) > TOKEN_TTL_S:
                self.pending.pop(token, None)

    # ── The subscription ──────────────────────────────────────────────────

    async def listener(self) -> None:
        """Follow the two topics this exchange uses, until the actor stops."""
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "coderefresh"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe("nodes/+/code_changed", qos=1)
                    await client.subscribe("nodes/+/code_return", qos=1)
                    logger.info("[%s] Subscribed to node code topics.", host.name)
                    last_error = None
                    async for message in client.messages:
                        topic = str(message.topic)
                        if topic.endswith("/code_return"):
                            await self.receive_code_return(topic, message.payload)
                        else:
                            await self._on_notice(topic, message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[%s] code listener error: %s. Reconnecting in %ss…",
                        host.name,
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _on_notice(self, topic: str, payload: bytes | None) -> None:
        if not payload:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return
        node = topic.split("/")[1] if "/" in topic else ""
        await self.note_change(node, str(data.get("agent") or ""))
