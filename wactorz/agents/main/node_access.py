# SPDX-License-Identifier: Apache-2.0
"""Saying when a node's agent uses a topic the broker's access list keeps from nodes.

A broker answers nothing to a publish it refuses at QoS 0, which is how agents
send data, and a subscription it refuses simply stays quiet. So an agent on a
node whose code picks a topic outside the list runs, reports itself healthy,
and nothing it sends arrives. Its manifest still reaches main, on a topic every
node may use, and names what the agent publishes and subscribes to: that is
where main reads it, and says so in chat, once per agent and topic.

Only where this server wrote the list (`broker_certificates`): on a broker it
does not write the files for, what a node may use is not main's to know, and
nothing is said.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ...broker_certificates import node_data_topics, node_is_under_access_list
from ...core.broker_accounts import node_may, node_topic_addable
from ...core.topics import display_topic

logger: logging.Logger = logging.getLogger(__name__)

Use = Literal["publish", "subscribe"]


class NoticeHost(Protocol):
    """What saying something in chat needs from main."""

    def _queue_notification(self, notice: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Refused:
    """One topic an agent on a node uses and the access list refuses."""

    use: Use
    #: As it goes on the wire.
    topic: str


def refused_topics(
    node: str,
    publishes: Iterable[str],
    subscribes: Iterable[str],
    node_topics: Iterable[tuple[str, str]],
) -> list[Refused]:
    """The topics among ``publishes`` and ``subscribes`` the access list refuses ``node``."""
    data = tuple(node_topics)
    uses: tuple[tuple[Use, Iterable[str]], ...] = (
        ("publish", publishes),
        ("subscribe", subscribes),
    )
    return [
        Refused(use, topic)
        for use, topics in uses
        for topic in topics
        if isinstance(topic, str) and topic and not node_may(node, use, topic, data)
    ]


def _sentence(name: str, node: str, refused: Refused) -> str:
    """What to tell the operator about one refused topic, and what to do about it."""
    shown = display_topic(refused.topic)
    if refused.use == "publish":
        said = (
            f"Agent '{name}' on node '{node}' publishes to `{shown}`, which the broker's access "
            "list does not let a node publish to, so nothing it sends there arrives."
        )
        entry = shown
    else:
        said = (
            f"Agent '{name}' on node '{node}' subscribes to `{shown}`, which the broker's access "
            "list does not let a node read, so it hears nothing there."
        )
        entry = f"read:{shown}"
    if node_topic_addable(refused.topic):
        return f"{said} To allow it, add `{entry}` to WACTORZ_NODE_TOPICS and restart Wactorz."
    return f"{said} That topic is one every node is kept from: the agent has to use another."


class RefusedTopics:
    """Tells the operator of each refused topic once, however often its agent's manifest arrives."""

    def __init__(self) -> None:
        #: What has been said: the node, the agent's name, and the refused use of a topic.
        self.said: set[tuple[str, str, Refused]] = set()

    def say(
        self,
        host: NoticeHost,
        name: str,
        node: str,
        publishes: Iterable[str],
        subscribes: Iterable[str],
    ) -> None:
        """Say in chat what ``name`` on ``node`` uses that the access list refuses, if not yet said."""
        if not node_is_under_access_list(node):
            return
        try:
            node_topics = node_data_topics()
        except ValueError:
            # A setting that could not be read wrote no list: what the broker
            # holds is then an earlier one, which main cannot answer for.
            return
        for refused in refused_topics(node, publishes, subscribes, node_topics):
            key = (node, name, refused)
            if key in self.said:
                continue
            self.said.add(key)
            message = _sentence(name, node, refused)
            logger.warning("[main] %s", message)
            host._queue_notification(
                {
                    "severity": "warning",
                    "about": f"refused-topic:{node}:{name}:{refused.use}:{refused.topic}",
                    "message": message,
                }
            )
