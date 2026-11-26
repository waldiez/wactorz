# SPDX-License-Identifier: Apache-2.0
"""A person's answer in words, sent on to the agent that asked when it runs on a node.

The dashboard takes such an answer in the server's process and hands it to an
agent there itself. One on a node is reached through main, over the node's
signed control topic ``nodes/<node>/answer``: never the agent's own answer
topic, where anything on the broker could write the person's words.

Everything that makes that safe is checked again for every answer, not once
when the question was asked: that main places the agent on that node, that
the node is up, says it takes answers in words, and enforces main's signature.
A question can wait a long time, and any of those can change meanwhile.
"""

from typing import Any, Protocol

from ...core.outbound import Answer
from ...core.topics import node_topic
from .hosts import VoiceHost
from .nodes import NodeManager
from .voices import placed_node

#: Why an answer cannot go while its agent is being moved: neither copy is sure
#: to be the one that runs, and words given to the wrong one are lost.
BEING_MOVED = "The agent that asked is being moved just now; answer again once the move is done."


class WordsHost(VoiceHost, Protocol):
    """What forwarding an answer needs of main: its nodes, and its own signed publish."""

    nodes: NodeManager

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...


async def forward_words(host: WordsHost, agent_name: str, answer: Answer) -> str | None:
    """Send ``answer``, the person's words, to the node ``agent_name`` runs on; or say why not.

    Returns None once it is published, signed. The agent records it and then
    publishes it on its own answer topic, which is what closes the question on
    every dashboard; a node that refuses it leaves the question open.
    """
    if host.migration.being_moved(agent_name):
        return BEING_MOVED
    node = placed_node(host, agent_name, answer.agent_id)
    if not node:
        return (
            "The agent that asked runs neither in this server's process nor on a node main knows."
        )
    if not host.nodes.is_online(node):
        return f"Node '{node}', where the agent that asked runs, is not answering just now."
    refused = host.nodes.why_no_words(node)
    if refused:
        return f"Cannot answer it in words: {refused}."
    await host._mqtt_publish(
        node_topic(node, "answer"),
        {"agent": agent_name, "actor_id": answer.agent_id, "answer": answer.to_payload()},
        qos=1,
    )
    return None
