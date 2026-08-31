"""Main answering LLM calls for agents running on other machines.

An edge node holds no API key. An agent there publishes its request to
`main/llm_request` with a topic to answer on; main runs the call with its own
provider and publishes the text back. Keeping the keys on one machine is the
whole point — a node can be stolen, and it carries nothing worth having.

**Every path replies.** A caller is waiting on a future that only its own
timeout will end, so no provider and a failed call both answer with text saying
so rather than answering nothing.

Calls made here spend main's budget, so their usage is folded into main's
totals. Attribution is per node rather than per agent: the request does not
carry the remote agent's actor id, which is what the per-agent metrics path
keys on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from ...core.mqtt import mqtt_client

if TYPE_CHECKING:
    from ...core.actor import ActorState

logger = logging.getLogger(__name__)

#: The topic remote agents publish their requests to.
REQUEST_TOPIC = "main/llm_request"

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0


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

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...


class LLMBridge:
    """Serves `main/llm_request` from the host's LLM provider."""

    def __init__(self, host: BridgeHost) -> None:
        self.host = host

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
                async with mqtt_client(host._mqtt_broker, host._mqtt_port) as client:
                    await client.subscribe(REQUEST_TOPIC)
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

        agent_name = data.get("agent", "remote-agent")
        node_name = data.get("node", "?")
        logger.info("[main] LLM bridge: request from %r on %r", agent_name, node_name)

        text = await self._complete(data, agent_name, node_name)
        await self.host._mqtt_publish(reply_topic, {"text": text})
        logger.info(
            "[main] LLM bridge: replied to %r (%s chars) → %s",
            agent_name,
            len(text),
            reply_topic,
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
