# SPDX-License-Identifier: Apache-2.0
"""Stopping an agent on a node, and starting it again there.

An agent on a node runs in another process, on another machine, under that
node's control: main reaches it only by asking the node. A stop asks the node to
stop it, which keeps its state file; a start is the agent's spawn sent again
from the config main kept, which the node takes as any other spawn and the
agent resumes from that file. Nothing here needs a node to know more than how
to spawn and to stop, so a node deployed by an earlier release does both.

What makes a stop last is that main remembers it (`SpawnRegistry.is_stopped`):
the agent leaves its node's desired state, so the node does not start it again
when it reboots, and main neither restores it at startup nor takes its absence
from the node's heartbeats for a crash.

Main sends and does not wait: a node answers a spawn by running the agent, not
with a message. So what a caller is told is that the command was sent, or why
it was not; a node that then fails to start the agent says so in the feed, as
it does for any spawn.
"""

import logging
from dataclasses import dataclass
from typing import Any

from ...core.topics import node_topic
from .hosts import StopStartHost

logger: logging.Logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """What came of asking: whether the command was sent, and what to tell whoever asked."""

    sent: bool
    message: str


class StopStartService:
    """Stops and starts the agents main keeps on nodes."""

    def __init__(self, host: StopStartHost) -> None:
        self.host = host

    def _entry(self, name: str, node: str, actor_id: str | None) -> dict[str, Any] | None:
        """The entry of the agent meant: by its id when given, and always the one on ``node``."""
        reg = self.host._get_spawn_registry()
        entry = reg.by_id(actor_id) if actor_id else None
        if entry is not None and entry["name"] == name:
            return entry
        return reg.at(name, node)

    async def stop(self, name: str, node: str, actor_id: str | None = None) -> Outcome:
        """Stop ``name`` on ``node``, keeping its state there and its config here.

        The stop is remembered before anything is sent: the node's answer to
        either message may be the agent withdrawing its manifest, and by then
        main has to know the entry is one to keep. If the desired state cannot
        be published the stop is forgotten again, since nothing was asked of
        the node. Once it is, the node stops the agent on that alone, so a stop
        message that then fails to leave is logged and the stop stands.
        """
        entry = self._entry(name, node, actor_id)
        if entry is None:
            # One main did not spawn: a node's own, or one started by hand.
            await self.host._mqtt_publish(node_topic(node, "stop"), {"name": name}, qos=1)
            return Outcome(
                True,
                f"Stop sent to '{name}' on node '{node}'. Main keeps no config for it, so it "
                "cannot be started again from here.",
            )
        registry = self.host._get_spawn_registry()
        if registry.is_stopped(entry["actor_id"]):
            return Outcome(True, f"'{name}' on node '{node}' is already stopped.")
        self.host._keep_stopped(entry["actor_id"], True)
        try:
            await self.host._update_node_desired_state(node)
        except Exception as exc:
            self.host._keep_stopped(entry["actor_id"], False)
            logger.warning("[%s] Could not stop %r on %r: %s", self.host.name, name, node, exc)
            return Outcome(False, f"'{name}' on node '{node}' was not stopped: {exc}")
        try:
            await self.host._mqtt_publish(node_topic(node, "stop"), {"name": name}, qos=1)
        except Exception as exc:
            logger.warning(
                "[%s] The stop for %r did not leave for %r (%s); its desired state stops it.",
                self.host.name,
                name,
                node,
                exc,
            )
        return Outcome(
            True,
            f"Stop sent to '{name}' on node '{node}'. Its state is kept there: "
            f"/start {name}@{node} starts it again.",
        )

    async def start(self, name: str, node: str, actor_id: str | None = None) -> Outcome:
        """Start ``name`` again on ``node``, where it was stopped, from the config main kept.

        It stays stopped unless the spawn is sent, and the spawn is what ends
        the stop (`SpawnService._spawn_remote`): a node that is away, or that
        cannot take the agent, leaves everything as it was. A spawn that left
        is a start, whatever fails after it, and is reported as one.
        """
        entry = self._entry(name, node, actor_id)
        if entry is None:
            return Outcome(
                False,
                f"'{name}' on node '{node}' cannot be started: main keeps no config for it.",
            )
        if not self.host._get_spawn_registry().is_stopped(entry["actor_id"]):
            return Outcome(False, f"'{name}' on node '{node}' is not stopped.")
        if node not in self.host._online_node_names():
            return Outcome(
                False,
                f"'{name}' was not started: node '{node}' is not online. It stays stopped.",
            )
        try:
            sent = await self.host._spawn_remote(dict(entry), node, False, kept=True)
        except Exception as exc:
            logger.exception("[%s] Starting %r on %r failed", self.host.name, name, node)
            if not self.host._get_spawn_registry().is_stopped(entry["actor_id"]):
                # The spawn left and something after it did not: the node runs
                # the agent, so saying it was not started would be wrong.
                return Outcome(
                    True,
                    f"Start sent to '{name}' on node '{node}', but what follows a spawn did not "
                    f"all go through ({exc}). The list the node starts from after a reboot "
                    "may not name it until that node's agents next change.",
                )
            return Outcome(False, f"'{name}' was not started on node '{node}': {exc}")
        if not sent:
            return Outcome(
                False,
                f"'{name}' was not started: node '{node}' cannot take it now, and the feed says "
                "why. It stays stopped.",
            )
        return Outcome(
            True,
            f"Start sent to '{name}' on node '{node}'. It resumes from the state it kept there.",
        )

    async def restart(self, name: str, node: str, actor_id: str | None = None) -> Outcome:
        """Restart ``name`` on ``node``: a stopped one is started, a running one replaced in place.

        A running agent is restarted by its node, which stops it and spawns it
        again from the config it holds, so nothing is remembered as stopped on
        the way and a restart that fails leaves no stop behind.
        """
        entry = self._entry(name, node, actor_id)
        if entry is not None and self.host._get_spawn_registry().is_stopped(entry["actor_id"]):
            return await self.start(name, node, actor_id)
        await self.host._mqtt_publish(node_topic(node, "restart_agent"), {"name": name}, qos=1)
        return Outcome(
            True,
            f"Restart sent to '{name}' on node '{node}'. It resumes from the state it kept there.",
        )
