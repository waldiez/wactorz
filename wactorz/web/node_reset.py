"""Asking the nodes to reset what their agents keep, and hearing back.

A reset of chat history, agent state or counters clears main's records, and
main's own agents, at once. An agent on a node keeps its conversation, its state
and its counters there, in memory and in its own file, so the node is asked to
clear them too. Its answer is waited for, a bounded time, so the reset can say
which nodes did their part rather than leave the reader to assume all of them did.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

from aiomqtt import MqttError

from ..core.mqtt import mqtt_client, user_properties, v5_kwargs
from ..core.node_publications import admit
from ..core.node_signing import signed_publish_kwargs

logger = logging.getLogger(__name__)

#: How long a reset waits for the nodes it asked. Generous for one round trip
#: over the broker; a node slower than this is reported as not having answered,
#: not asked again.
ANSWER_TIMEOUT_S = 10.0

#: What a reset reports for each node it concerned.
CLEARED = "cleared"
NO_ANSWER = "no answer"
OFFLINE = "offline"
OLDER_RELEASE = "older release"


async def reset_on_nodes(
    main_actor: Any, scope: str, agent: str | None, on: dict[str, set[str]]
) -> dict[str, str]:
    """Have each node concerned reset `scope` for `agent`, or for all its agents.

    `on` is which agents each node runs. A node that has not reported lately is
    not asked: the request is not kept for it, so asking would only wait out the
    timeout. Nor is one from a release that does not reset its agents, which
    would never answer at all.
    """
    concerned = sorted(node for node, names in on.items() if agent is None or agent in names)
    outcome: dict[str, str] = {}
    asked: list[str] = []
    for node in concerned:
        if not main_actor.nodes.is_online(node):
            outcome[node] = OFFLINE
        elif not main_actor.nodes.takes_resets(node):
            outcome[node] = OLDER_RELEASE
        else:
            asked.append(node)
    if asked:
        outcome.update(await _ask(main_actor, scope, agent, asked))
    return outcome


async def _ask(main_actor: Any, scope: str, agent: str | None, nodes: list[str]) -> dict[str, str]:
    """Send the reset to `nodes` and wait for each to answer it."""
    request = uuid.uuid4().hex
    answers = dict.fromkeys(nodes, NO_ANSWER)
    try:
        async with mqtt_client(
            main_actor._mqtt_broker, main_actor._mqtt_port, **v5_kwargs()
        ) as client:
            # Subscribed before asking, so an answer cannot arrive unheard.
            for node in nodes:
                await client.subscribe(f"nodes/{node}/reset_done", qos=1)
            for node in nodes:
                topic = f"nodes/{node}/reset"
                payload = json.dumps({"scope": scope, "agent": agent, "request": request})
                await client.publish(topic, payload, qos=1, **signed_publish_kwargs(topic, payload))
            await asyncio.wait_for(_hear(client, request, answers), ANSWER_TIMEOUT_S)
    # Spelled this way on purpose: the two are one object from 3.11 and separate
    # on 3.10, where only this spelling catches what `wait_for` raises.
    except asyncio.TimeoutError:
        pass
    except (OSError, MqttError) as exc:
        logger.warning("[reset] Could not ask the nodes to reset %s: %s", scope, exc)
    return answers


async def _hear(client: Any, request: str, answers: dict[str, str]) -> None:
    """Mark each node that answers `request`, until every one has.

    An answer counts only signed with its node's key and quoting this request:
    anything else on the topic is someone else's, or an earlier reset's.
    """
    async for msg in client.messages:
        topic = str(msg.topic)
        parts = topic.split("/")
        node = parts[1] if len(parts) == 3 else ""
        if node not in answers:
            continue
        payload = bytes(msg.payload or b"")
        if not admit(node, topic, payload, user_properties(msg), "a reset answer"):
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("request") != request:
            continue
        answers[node] = CLEARED
        if all(state == CLEARED for state in answers.values()):
            return
