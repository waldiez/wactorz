"""Main answering LLM calls for agents running on other machines.

An edge node holds no API key. An agent there publishes its request to
`main/llm_request` with a topic to answer on; main runs the call with its own
provider and publishes the text back. Keeping the keys on one machine is the
whole point — a node can be stolen, and it carries nothing worth having.

**Every path replies.** A caller is waiting on a future that only its own
timeout will end, so no provider and a failed call both answer with text saying
so rather than answering nothing.

**Only a node main deployed is answered, and only on its own reply topics.**
Anything on the broker can publish a request, and main answers with an account
the broker lets write anywhere -- so a request names the node it comes from,
carries a signature made with that node's key (see
:func:`wactorz.core.node_signing.sign_request`), and gets its reply on a topic
under ``nodes/<that node>/reply/``. A reply topic anywhere else is refused
outright; a request without a valid signature follows ``WACTORZ_NODE_SIGNING``,
as a command to a node does: ``enforce`` answers with an error, ``warn``
answers and says so in chat.

Calls made here spend main's budget, so their usage is folded into main's
totals. Attribution is per node rather than per agent: the request does not
carry the remote agent's actor id, which is what the per-agent metrics path
keys on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Protocol

from ...config import NODE_SIGNING
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from ...core.node_signing import request_signed_for

if TYPE_CHECKING:
    from ...core.actor import ActorState

logger = logging.getLogger(__name__)

#: The topic remote agents publish their requests to.
REQUEST_TOPIC = "main/llm_request"

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: How many answered requests to remember, for the duplicate check below. It
#: catches a broker's redelivery, which follows its original closely, and a
#: signed request captured and replayed later to spend main's budget -- the
#: signature covers the reply topic, so a replay carries the same one. Bounded,
#: so a long-running main does not accumulate them without end; a reply topic
#: is a few dozen bytes.
ANSWERED_MEMORY = 10_000

#: How many nodes to remember having reported, per kind of report. The node in a
#: request is whatever the sender wrote, so without a bound a client inventing
#: names would grow this for as long as main runs. One forgotten is at worst
#: reported once more.
REPORTED_MEMORY = 1024

#: The reply topics a node's requests use: its own namespace, then a random id.
_REPLY_TOPIC = re.compile(r"nodes/(?P<node>[^/#+]+)/reply/[0-9a-f]+")

#: What an unsigned request is told when main refuses it.
REFUSED_UNSIGNED = "[LLM error: request not signed for this node — deploy the node again]"


class BridgeHost(Protocol):
    """What the bridge needs from the actor that owns it.

    Typing-only, in the spirit of `mixins/host.py`. The provider and the totals
    are read rather than passed in because both change while the bridge runs.
    """

    name: str
    state: ActorState
    llm: Any
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    _mqtt_broker: str
    _mqtt_port: int

    def _persist_cost(self) -> None: ...

    def _queue_notification(self, notice: dict[str, Any]) -> None: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...


class LLMBridge:
    """Serves `main/llm_request` from the host's LLM provider."""

    def __init__(self, host: BridgeHost) -> None:
        self.host = host
        #: Reply topics already answered, newest last -- see :meth:`answer`.
        self._answered: OrderedDict[str, None] = OrderedDict()
        #: Nodes already reported for an unsigned or misdirected request, so a
        #: node that sends many says so once rather than on every call. Newest
        #: last, and bounded -- see `REPORTED_MEMORY`.
        self._reported: OrderedDict[str, None] = OrderedDict()

    async def listen(self) -> None:
        """Answer requests until the actor stops.

        Reconnects on failure. The first failure is logged at warning and
        repeats of the same one at debug: a broker that is down stays down, and
        a line per retry buries the outage that caused them.
        """
        host = self.host
        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "llm"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe(REQUEST_TOPIC, qos=1)
                    logger.info("[main] LLM bridge listening on %s", REQUEST_TOPIC)
                    last_error = None
                    async for message in client.messages:
                        try:
                            data = json.loads(message.payload.decode())
                        except Exception as exc:
                            logger.debug("[main] Undecodable bridge request: %s", exc)
                            continue
                        if isinstance(data, dict):
                            await self.answer(data)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] LLM bridge listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] LLM bridge listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def answer(self, data: dict[str, Any]) -> None:
        """Run one request and publish the result to the topic it named.

        A request with no reply topic is dropped: there is nowhere to answer,
        and the caller cannot be waiting on something it did not ask for.
        """
        reply_topic = data.get("_reply_topic")
        if not reply_topic:
            return
        node_name = str(data.get("node") or "")
        agent_name = data.get("agent", "remote-agent")
        if not reply_topic_for(str(reply_topic), node_name):
            # Main would publish there with an account that may write anywhere,
            # on behalf of whoever asked: refused, and nothing is sent at all.
            self._report_once(
                f"topic:{node_name}",
                "[main] LLM bridge: refused a request from %r naming %r to reply on %r, "
                "which is not that node's reply topic",
                agent_name,
                node_name,
                reply_topic,
            )
            return

        # QoS 1 is at-least-once: the broker redelivers anything it did not see
        # acknowledged, so a drop between receipt and acknowledgement replays the
        # request. Most handlers on the durable topics are naturally idempotent;
        # this one is not -- a replay spends main's LLM budget a second time for
        # an answer already published. The reply topic carries a uuid per
        # request, which makes it the correlation id this needs.
        if reply_topic in self._answered:
            logger.info("[main] LLM bridge: ignoring a redelivered request for %s", reply_topic)
            return
        self._answered[reply_topic] = None
        while len(self._answered) > ANSWERED_MEMORY:
            self._answered.popitem(last=False)

        if not request_signed_for(data, node_name):
            if NODE_SIGNING == "enforce":
                self._report_once(
                    f"unsigned:{node_name}",
                    "[main] LLM bridge: refused an unsigned request from %r on %r "
                    "(WACTORZ_NODE_SIGNING=enforce); deploy the node again",
                    agent_name,
                    node_name,
                )
                await self.host._mqtt_publish(reply_topic, {"text": REFUSED_UNSIGNED})
                return
            self._warn_unsigned(node_name, agent_name)

        logger.info("[main] LLM bridge: request from %r on %r", agent_name, node_name)

        text = await self._complete(data, agent_name, node_name)
        await self.host._mqtt_publish(reply_topic, {"text": text})
        logger.info(
            "[main] LLM bridge: replied to %r (%s chars) → %s",
            agent_name,
            len(text),
            reply_topic,
        )

    def _first_report(self, key: str) -> bool:
        """Whether ``key`` has not been reported yet, remembering it if so."""
        if key in self._reported:
            return False
        self._reported[key] = None
        while len(self._reported) > REPORTED_MEMORY:
            self._reported.popitem(last=False)
        return True

    def _report_once(self, key: str, message: str, *args: Any) -> None:
        """Log a refusal at warning the first time for ``key``, at debug after that."""
        if self._first_report(key):
            logger.warning(message, *args)
        else:
            logger.debug(message, *args)

    def _warn_unsigned(self, node_name: str, agent_name: str) -> None:
        """Say in chat, once per node, that it asked unsigned and was answered anyway."""
        if not self._first_report(f"answered-unsigned:{node_name}"):
            return
        logger.warning(
            "[main] LLM bridge: answered an unsigned request from %r on %r "
            "(WACTORZ_NODE_SIGNING=warn)",
            agent_name,
            node_name,
        )
        self.host._queue_notification(
            {
                "severity": "warning",
                "message": (
                    f"Node '{node_name}' asked main's LLM without a signature, and was answered "
                    "because WACTORZ_NODE_SIGNING=warn. Deploy it again so it signs its "
                    f"requests: /deploy {node_name}"
                ),
            }
        )

    async def _complete(self, data: dict[str, Any], agent_name: str, node_name: str) -> str:
        """The model's answer, or text describing why there is none."""
        try:
            if self.host.llm is None:
                logger.warning(
                    "[main] LLM bridge: request from %r but main has no LLM provider",
                    agent_name,
                )
                return "[LLM error: no provider configured on main]"

            messages = data.get("messages")
            if not (messages and isinstance(messages, list)):
                messages = [{"role": "user", "content": data.get("prompt", "")}]
            system = data.get("system", "") or (
                f"You are {agent_name}, an AI agent running on node {node_name}."
            )

            response, usage = await self.host.llm.complete(messages=messages, system=system)
            self._record_usage(usage)
            return response if isinstance(response, str) else str(response)
        except Exception as exc:
            logger.exception("[main] LLM bridge error for %r", agent_name)
            return f"LLM error: {exc}"

    def _record_usage(self, usage: dict[str, Any]) -> None:
        """Fold a call's usage into the host's totals.

        Best-effort: a provider that reports usage in an unexpected shape should
        cost the caller its answer, not the whole reply.
        """
        try:
            self.host.total_input_tokens += usage.get("input_tokens", 0)
            self.host.total_output_tokens += usage.get("output_tokens", 0)
            self.host.total_cost_usd += usage.get("cost_usd", 0.0)
            self.host._persist_cost()
        except Exception as exc:
            logger.debug("[main] Recording bridge usage failed: %s", exc)


def reply_topic_for(reply_topic: str, node: str) -> bool:
    """Whether ``reply_topic`` is one of ``node``'s own reply topics."""
    match = _REPLY_TOPIC.fullmatch(reply_topic)
    return bool(match and node and match.group("node") == node)
