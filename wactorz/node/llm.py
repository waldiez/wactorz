"""The LLM, as reached from a node.

The API key stays on main. An agent running here calls ``agent.llm.chat(...)``
exactly as it would anywhere, and the call goes out over the broker to main's
``main/llm_request`` bridge, which makes the real request and answers on a reply
topic. So the same generated program runs on a node and on main, and no node is
ever deployed with a credential.

This is a provider rather than a second LLM interface for that reason: the
interface generated code sees is :class:`~wactorz.agents.dynamic.api.LLMInterface`
in both places, and only what sits underneath it differs.
"""

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ..agents.llm.base import LLMProvider

if TYPE_CHECKING:
    from .agent import NodeAgent

logger = logging.getLogger(__name__)

#: How long a bridged call waits for main to answer. Generous rather than tight:
#: the request is queued here, travels to main, waits on a model and comes back,
#: and cutting a slow-but-healthy call short only trades one failure for another.
REQUEST_TIMEOUT_S = 60.0


class BridgeProvider(LLMProvider):
    """An LLM provider whose model runs on main, reached over MQTT."""

    def __init__(self, agent: "NodeAgent") -> None:
        self._agent = agent

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        """Ask main to make this call, and wait for the answer it publishes back.

        The usage it reports is whatever main sent. The bridge has historically
        replied with the text alone, in which case the tokens are counted where
        they were spent — on main — rather than against the agent that asked for
        them. Reading it here is what lets that be fixed from the other side
        without touching a node.
        """
        timeout = float(kwargs.get("timeout", REQUEST_TIMEOUT_S))
        reply = await self._agent.ask_main(
            "main/llm_request",
            {"messages": messages, "system": system},
            timeout=timeout,
        )
        if not isinstance(reply, dict):
            return (str(reply) if reply is not None else ""), {}
        usage = reply.get("usage")
        return str(reply.get("text", "")), usage if isinstance(usage, dict) else {}


async def request_over_mqtt(
    agent: "NodeAgent", topic: str, payload: dict[str, Any], timeout: float
) -> Any:
    """Publish a request that names a reply topic, and wait for the reply on it.

    The future is keyed by the reply topic in the actor's own
    ``_result_futures``, which is the same convention ``AgentAPI.send_to`` uses
    for a remote call from main — so the runner has one place to deliver a reply
    to, whichever kind of request asked for it.

    ``asyncio.wait_for`` rather than ``asyncio.timeout``: this runs on Python
    3.10, where the latter does not exist. The future is cancelled on timeout
    and the key is dropped either way, so a reply that arrives afterwards finds
    nothing waiting and is logged rather than resolving a call that gave up.
    """
    reply_topic = f"nodes/{agent.node}/reply/{uuid.uuid4().hex[:8]}"
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    agent._result_futures[reply_topic] = future
    try:
        await agent._mqtt_publish(
            topic,
            {
                **payload,
                "_reply_topic": reply_topic,
                "agent": agent.name,
                "node": agent.node,
            },
        )
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[%s] %s timed out after %ss", agent.name, topic, timeout)
        return None
    finally:
        agent._result_futures.pop(reply_topic, None)
