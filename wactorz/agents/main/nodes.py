"""What main knows about remote nodes, and the questions it answers about them.

A node reports itself by heartbeat: who it is, which agents it is running, and
some system metrics. Everything here reads that one dict — whether a node counts
as up, which ones do, what to show, and which node is running a given agent.

Freshness is one constant rather than a number written at each call site. The
four readers have to agree: a node that the listing calls online while the
migration check calls it gone is a node nothing can be done with, and the
disagreement would only appear at the boundary.

Deliberately knows nothing about MQTT, the registry or the actor system. It is a
dict of heartbeats with questions attached, so it can be tested as one.
"""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..._version import __version__
from ...config import NODE_SIGNING
from ...core.actor import derive_actor_id
from ...core.compatibility import compatible
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    reconnect_wait,
    session_kwargs,
    user_properties,
)
from ...core.node_publications import Stamp, admit, signed_stamp
from .hosts import NodeHost
from .manifests import ManifestRegistry
from .voices import speaks_for

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: What a node is running when its heartbeat does not say. The single-file
#: runner predates the `runtime` field, so silence means that runtime.
DEFAULT_NODE_RUNTIME = "runner"

#: Consecutive heartbeats an agent must be missing from before it counts as
#: gone rather than late.
#:
#: One miss is a gap — a slow node, a dropped packet, a heartbeat that crossed
#: a restart. Acting on the first one deletes agents that are alive, and a
#: deletion here is not reversible: the spawn registry entry goes with it.
VANISH_MISS_THRESHOLD = 3

#: How recently a node must have reported to count as online.
#:
#: Short on purpose: this drives the indicator a person is looking at, so a node
#: that has gone away should say so quickly. It is not the window for acting on
#: a node's absence — deciding its agents are gone waits considerably longer, so
#: a brief network gap costs a grey dot rather than a deletion.
ONLINE_WINDOW_S = 30.0

#: Told of each heartbeat main takes, with the node and what main took from it.
HeartbeatWatcher = Callable[[str, dict[str, Any]], Awaitable[None]]

#: How many runs of one node's runner to remember the newest heartbeat of. A
#: heartbeat from a run forgotten is taken as a new run's; a node restarts far
#: less often than this many times while main runs.
RUNS_REMEMBERED = 16

#: The least time between two republishes of one node's desired state prompted by
#: what its heartbeats say about signing. A node reporting failures on every
#: heartbeat would otherwise have it republished on every one.
SIGNING_REPUBLISH_INTERVAL_S = 60.0


def _as_count(value: object) -> int:
    """A count a heartbeat reported, or 0 for anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _as_names(value: object) -> list[str]:
    """A list of agent names from a heartbeat, or none if it is not one."""
    if not isinstance(value, list):
        return []
    return [name for name in value if isinstance(name, str)]


def _as_number(value: object) -> float | int | None:
    """A reading a heartbeat reported, or None for anything that is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _as_flags(value: object) -> list[str] | None:
    """The throttle flags a heartbeat reported; None when it could not tell."""
    if not isinstance(value, list):
        return None
    return [flag for flag in value if isinstance(flag, str)]


#: What a node says about how close it is to running out, under the names it
#: says it. Copied as they arrive into what main knows and what it lists; a
#: heartbeat from a runner older than a reading leaves it None, which is "not
#: known" -- never zero, which would read as nothing left.
RESOURCE_READINGS = (
    "cpu_pct",
    "mem_used_mb",
    "mem_free_mb",
    "swap_used_mb",
    "load_1m",
    "load_5m",
    "disk_free_mb",
    "temp_c",
)


#: How long a node may stay silent before its agents are treated as lost.
#:
#: Longer than the window above on purpose. That one drives the indicator in the
#: dashboard, where being quick to say "offline" costs nothing; this one deletes
#: agents, where being quick would turn a network blip into a resurrection loop.
OFFLINE_GRACE_S = 90.0

#: How often the watcher looks for nodes that have gone quiet.
OFFLINE_CHECK_INTERVAL_S = 15.0


def _runner_restarted(record: dict[str, Any], before: dict[str, Any]) -> bool:
    """Whether two heartbeats of one node came from different runs of its runner."""
    if record.get("pid") != before.get("pid"):
        return True
    uptime, was_uptime = record.get("uptime_s"), before.get("uptime_s")
    if isinstance(uptime, (int, float)) and isinstance(was_uptime, (int, float)):
        return uptime < was_uptime
    return False


def _unsigned_notice(node_name: str) -> str:
    """What the chat notice saying ``node_name`` does not sign is about, to withdraw it by."""
    return f"unsigned-node:{node_name}"


def remote_actor_id(agent_name: str) -> str:
    """The actor id a remote agent has, derived from its name.

    Deterministic so main and the node arrive at the same id without either
    telling the other — the node builds it the same way for `_RemoteAgent`.
    """
    return derive_actor_id(agent_name)


class NodeManager:
    """The known remote nodes, keyed by name.

    Each entry is what the node last sent: ``last_seen``, ``agents``, and
    whatever system metrics it included. Entries are written by the heartbeat
    listener and read from here.
    """

    def __init__(
        self, host: NodeHost | None = None, manifests: ManifestRegistry | None = None
    ) -> None:
        self.host = host
        self.known: dict[str, dict[str, Any]] = {}
        #: Consulted before filling a gap: a manifest, when one has arrived, is
        #: better than anything a spawn config can say.
        self.manifest_registry = manifests if manifests is not None else ManifestRegistry(host)
        #: (node, agent) -> consecutive heartbeats that agent has been missing.
        self.agent_misses: dict[tuple[str, str], int] = {}
        #: Nodes a deploy is restarting right now. Their silence and their empty
        #: heartbeats are main's own doing, not a crash, and are not acted on.
        self.redeploying: set[str] = set()
        #: node -> monotonic time its desired state was last republished because of
        #: what its heartbeat said about signing.
        self.signing_republished_at: dict[str, float] = {}
        #: node -> what its machine is, from its retained manifest. Kept apart
        #: from `known` and not dropped with it: a node forgotten for going
        #: quiet does not send its manifest again when it comes back -- the
        #: broker delivers a retained message on subscribing, not on reconnects.
        self.node_manifests: dict[str, dict[str, Any]] = {}
        #: node -> run id -> the count of the newest signed heartbeat from that run,
        #: so each heartbeat is taken once. See `wactorz.core.node_publications`.
        self.heartbeat_counts: dict[str, OrderedDict[str, int]] = {}
        #: Nodes already said in chat to be sending unsigned heartbeats.
        self.unsigned_reported: set[str] = set()
        #: Told of every heartbeat taken, as main took it: the dashboard shows a
        #: node from this, never from the broker, where anyone may publish one.
        self.watchers: list[HeartbeatWatcher] = []

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every known node, with its age resolved to an `online` flag.

        The key names are a contract beyond this module: the dashboard reads
        them off the wire, so a renamed or dropped key is invisible from here
        and visible there.
        """
        now = time.time()
        return [
            {
                "node": name,
                "agents": info.get("agents", []),
                "last_seen": info.get("last_seen", 0),
                "online": self._is_fresh(info, now),
                "pid": info.get("pid"),
                "version": info.get("version"),
                "runtime": info.get("runtime", DEFAULT_NODE_RUNTIME),
                "uptime_s": info.get("uptime_s"),
                **{reading: info.get(reading) for reading in RESOURCE_READINGS},
                "throttled": info.get("throttled"),
                # Agents whose restarts the node's supervisor has slowed down.
                "slow_retry": info.get("slow_retry", []),
                # What the machine is; None from a node that has not said.
                "manifest": self.node_manifests.get(name),
            }
            for name, info in self.known.items()
        ]

    def is_online(self, node_name: str) -> bool:
        """Whether `node_name` has reported inside the window.

        A node nobody has heard of is offline rather than an error: callers ask
        about names that came from a user or a stale registry entry.
        """
        info = self.known.get(node_name)
        return bool(info) and self._is_fresh(info, time.time())

    def online_names(self) -> list[str]:
        """The online nodes, sorted — this reaches a person in an error message."""
        return sorted(name for name in self.known if self.is_online(name))

    def begin_redeploy(self, node_name: str) -> None:
        """Say that `node_name` is about to be stopped and started by a deploy.

        Until :meth:`end_redeploy`, the node's agents are not pruned when its
        heartbeats stop listing them, and the node is not forgotten when the
        heartbeats stop altogether. Both are what a redeploy looks like from
        here: the old process is killed, the new one comes up with no agents and
        only then reconciles against the desired state. Reading either as a
        crash is what deleted the agents a redeploy was meant to keep.
        """
        self.redeploying.add(node_name)

    def end_redeploy(self, node_name: str) -> None:
        """The deploy is over, one way or the other: judge the node normally again.

        The miss counters are reset rather than kept: whatever was counted during
        the deploy was the deploy, and the new node's first heartbeats -- sent
        before it has reconciled -- must not carry that count over the threshold.
        """
        self.redeploying.discard(node_name)
        for key in [k for k in self.agent_misses if k[0] == node_name]:
            self.agent_misses.pop(key, None)

    def version_mismatch(self, node_name: str) -> str | None:
        """Why an agent must not be sent to `node_name`, or None when it may be.

        A node runs the same package as main, from the same release series: its
        agents are built from the same code and speak the same contract, and a
        spawn config main writes today may name something an older node has
        never heard of. A node on another series is refused, with the command
        that brings it level; one that differs in the patch number alone is
        not, so a fix to the server does not mean deploying every node again.

        A node that reports no version at all is not judged here. That is a
        runtime from before the field existed, and what to do about it is the
        signing and runtime handling's call, made on the same heartbeat. Only
        ever asked about a node main has heard from: whether the node is online
        at all is a separate question with its own answer.
        """
        info = self.known.get(node_name)
        if not info:
            return None
        reported = info.get("version")
        if not reported or compatible(__version__, str(reported)):
            return None
        return (
            f"node '{node_name}' is running version {reported}, which does not work with "
            f"this server's {__version__}. Redeploy it with `/deploy {node_name}` so both "
            "run the same release."
        )

    def running_agent(self, name: str) -> str:
        """The online node running `name`, or "" if none currently claims it.

        Empty string rather than None because callers test it as a string. A
        node outside the window does not claim its agents, which is what lets a
        migration proceed rather than refusing on behalf of a node that is gone.
        """
        now = time.time()
        for node_name, info in self.known.items():
            if self._is_fresh(info, now) and name in info.get("agents", []):
                return node_name
        return ""

    def running_agents(self) -> set[str]:
        """Every agent name an online node claims to be running.

        A node outside the window contributes nothing, so an agent counts as
        remote only while something is still reporting it.
        """
        now = time.time()
        return {
            agent
            for info in self.known.values()
            if self._is_fresh(info, now)
            for agent in info.get("agents", [])
        }

    def last_listed_on(self, name: str) -> str:
        """The node whose latest heartbeat lists ``name``, recent or not, or "".

        Unlike `running_agent`, a quiet node still answers until main forgets
        it: what it last said stands until it says something else.
        """
        return next(
            (node for node, info in self.known.items() if name in info.get("agents", [])), ""
        )

    @staticmethod
    def _is_fresh(info: dict[str, Any], now: float) -> bool:
        """Whether a heartbeat is recent enough to count.

        Missing `last_seen` reads as 0, which is 1970 and always stale — the
        honest answer for a node that has never reported.
        """
        return (now - info.get("last_seen", 0)) < ONLINE_WINDOW_S

    # ── Heartbeats ─────────────────────────────────────────────────────────

    async def heartbeat_listener(self) -> None:
        """Follow node heartbeats and migration results until the actor stops.

        Reconnects on failure, logging the first occurrence of a fault at
        warning and repeats at debug.
        """
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "nodes"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    # Heartbeats are liveness evidence, and a stale one is
                    # worse than none: it is recorded as "seen just now", so a
                    # replayed batch would mark a dead node online. Nodes send
                    # them at QoS 0 for the same reason; asking for more here
                    # would only be misleading, since delivery is the lower of
                    # the two.
                    await client.subscribe("nodes/+/heartbeat", qos=0)
                    await client.subscribe("nodes/+/migrate_result", qos=1)
                    await client.subscribe("nodes/+/manifest", qos=1)
                    logger.info("[main] Subscribed to node heartbeats.")
                    last_error = None
                    async for message in client.messages:
                        await self.receive_node_message(
                            str(message.topic), message.payload, user_properties(message)
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] Node heartbeat listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] Node heartbeat listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(reconnect_wait(RECONNECT_DELAY_S))

    async def receive_node_message(
        self, topic: str, payload: bytes | None, properties: Mapping[str, str] | None = None
    ) -> None:
        """Route one message from a node to whichever half handles it.

        ``properties`` are the user properties it arrived with, which carry the
        node's signature; None for a message that did not come off the broker,
        which has nothing to check.
        """
        if topic.endswith("/manifest") and not payload:
            # The retained manifest cleared: the node was removed.
            parts = topic.split("/")
            if len(parts) == 3:
                self.node_manifests.pop(parts[1], None)
            return
        try:
            data = json.loads(payload.decode()) if payload else None
        except Exception:
            return
        if not isinstance(data, dict):
            return
        parts = topic.split("/")
        if len(parts) < 3:
            return
        await self._route(parts[1], topic, payload or b"", properties, data)

    async def _route(
        self,
        node_name: str,
        topic: str,
        payload: bytes,
        properties: Mapping[str, str] | None,
        data: dict[str, Any],
    ) -> None:
        """Hand a decoded message to its half, once its signature says it is the node's."""
        if topic.endswith("/heartbeat"):
            signed = self._heartbeat_signed(node_name, topic, payload, properties)
            if signed is not None:
                await self.receive_heartbeat(node_name, data, signed=signed)
            return
        if properties is not None and not admit(
            node_name, topic, payload, properties, "a node report"
        ):
            return
        if topic.endswith("/migrate_result"):
            self.report_migration(data)
        elif topic.endswith("/manifest"):
            self.receive_manifest(node_name, data)

    def _heartbeat_signed(
        self, node_name: str, topic: str, payload: bytes, properties: Mapping[str, str] | None
    ) -> bool | None:
        """Whether a heartbeat is signed by its node; None for one already taken, to be dropped."""
        if properties is None:
            return True
        stamp = signed_stamp(node_name, topic, payload, properties)
        if stamp is None:
            return False
        if not self._first_sight(node_name, stamp):
            logger.debug("[main] Ignoring a heartbeat from %r seen before", node_name)
            return None
        return True

    def _first_sight(self, node_name: str, stamp: Stamp) -> bool:
        """Whether a signed heartbeat is newer than any seen from its run, remembering it if so.

        A heartbeat from a run not seen before is taken: a node restarting starts
        a run, and main has no clock of the node's to order runs by.
        """
        runs = self.heartbeat_counts.setdefault(node_name, OrderedDict())
        if stamp.count <= runs.get(stamp.run, 0):
            return False
        runs[stamp.run] = stamp.count
        runs.move_to_end(stamp.run)
        while len(runs) > RUNS_REMEMBERED:
            runs.popitem(last=False)
        return True

    def speaks_for(self, node_name: str, agent_name: str) -> bool:
        """Whether a message from ``node_name`` may speak for ``agent_name``: see `voices`."""
        return self.host is None or speaks_for(self.host, node_name, agent_name)

    def node_for(self, agent_name: str) -> str:
        """The node a task for ``agent_name`` goes to, or "" when none has it.

        One whose latest heartbeat lists the agent and that may speak for it:
        a node listing an agent main places elsewhere is not sent its tasks.
        """
        return next(
            (
                node
                for node, info in self.known.items()
                if agent_name in info.get("agents", []) and self.speaks_for(node, agent_name)
            ),
            "",
        )

    def refuses_tasks(self, node_name: str) -> str | None:
        """Why no task should go to ``node_name``, when its replies would be refused anyway.

        A node whose heartbeats are unsigned is one deployed before nodes signed
        what they publish, and under ``enforce`` main takes no reply from it: a
        task sent there could only time out, so it is not sent, and the caller
        says why at once.
        """
        record = self.known.get(node_name) or {}
        if NODE_SIGNING != "enforce" or record.get("signed", True):
            return None
        return (
            f"node '{node_name}' does not sign what it publishes, so main takes no reply "
            f"from it (WACTORZ_NODE_SIGNING=enforce). Deploy it again: /deploy {node_name}"
        )

    def receive_manifest(self, node_name: str, data: dict[str, Any]) -> None:
        """Take what a node says its machine is.

        Kept as it arrived, for the readers that know the fields. One without a
        manifest version is not a manifest this server can read, and is ignored
        rather than half-understood.
        """
        version = data.get("manifest_v")
        if isinstance(version, bool) or not isinstance(version, int):
            logger.warning("[main] Ignoring a manifest from node %r with no version.", node_name)
            return
        self.node_manifests[node_name] = data

    def targets_of(self, node_name: str) -> list[str]:
        """The library agents `node_name` says it can build, by import path.

        From its manifest; none from a node that has not sent one, or one
        from a release that did not report them. A module agent is placed on
        a node only when this names its target.
        """
        manifest = self.node_manifests.get(node_name) or {}
        agents = manifest.get("agents")
        if not isinstance(agents, list):
            return []
        return [str(target) for target in agents if isinstance(target, str)]

    def takes_bundles(self, node_name: str) -> bool:
        """Whether `node_name` builds a library agent from code sent with its spawn.

        From its manifest, which says so; a node from a release before bundles
        does not, and is sent none.
        """
        manifest = self.node_manifests.get(node_name) or {}
        return manifest.get("code_bundles") is True

    def forget(self, node_name: str) -> None:
        """Forget a node altogether, as removing it does: its heartbeat and its machine."""
        self.known.pop(node_name, None)
        self.node_manifests.pop(node_name, None)
        self.heartbeat_counts.pop(node_name, None)
        self.unsigned_reported.discard(node_name)

    async def receive_heartbeat(
        self, node_name: str, data: dict[str, Any], *, signed: bool = True
    ) -> None:
        """Take one heartbeat: prune what vanished, record the node, fill gaps.

        One that is not ``signed`` with the node's key is, under ``enforce``,
        evidence that the node is up and nothing more: see `_take_unsigned`.
        """
        before = self.known.get(node_name)
        if not signed:
            if self.heartbeat_counts.get(node_name):
                # This node has signed its heartbeats since main started, and a
                # runner does not stop signing: something else wrote this one.
                logger.debug("[main] Ignoring an unsigned heartbeat for %r", node_name)
                return
            self._say_unsigned(node_name)
            if NODE_SIGNING == "enforce":
                self._take_unsigned(node_name, data, before)
                await self._tell_watchers(
                    node_name, {**data, "agents": self.known[node_name]["agents"]}
                )
                return
        if signed:
            self._now_signed(node_name)
        agents = data.get("agents", [])
        previous = set((before or {}).get("agents", []))
        await self._prune_vanished(node_name, set(agents), previous)
        self.known[node_name] = {
            "last_seen": time.time(),
            "agents": agents,
            "node_id": data.get("node_id", ""),
            # What is running there. A heartbeat that names neither comes from
            # a runner older than the fields: the single-file runtime, at a
            # version it could not say.
            "version": data.get("version"),
            "runtime": data.get("runtime") or DEFAULT_NODE_RUNTIME,
            "pid": data.get("pid"),
            "uptime_s": data.get("uptime_s"),
            **{reading: _as_number(data.get(reading)) for reading in RESOURCE_READINGS},
            "throttled": _as_flags(data.get("throttled")),
            # Whether the node checks what main sends it. A heartbeat without it
            # comes from a runner older than signing, which checks nothing.
            "signing": data.get("signing") or "off",
            "signing_failures": _as_count(data.get("signing_failures")),
            # Whether the node reaches the broker over TLS. A heartbeat without it
            # comes from a runner older than TLS, which does not.
            "tls": data.get("tls") is True,
            # The node's agents whose restarts have slowed after repeated
            # crashes. A heartbeat without it comes from a runner older than
            # slow retry, which reports none.
            "slow_retry": _as_names(data.get("slow_retry")),
            # Whether the node signs what it publishes; see `refuses_tasks`.
            "signed": signed,
        }
        if host := self.host:
            # After `known` is updated: a held withdrawal is decided by this
            # heartbeat, not by the one that made it wait.
            await host.settle_withdrawals(node_name, set(agents))
            host.node_reported(node_name)
        self._bootstrap_contracts(node_name, agents)
        self._touch_monitor(agents)
        await self.follow_signing(node_name, self.known[node_name], before)
        self.follow_restarts(node_name, self.known[node_name], before)
        await self._tell_watchers(node_name, data)

    async def _tell_watchers(self, node_name: str, taken: dict[str, Any]) -> None:
        """Hand a heartbeat, as main took it, to whatever watches the node table.

        A watcher that fails is logged and the rest are still told: what shows
        a node must not stop main recording it.
        """
        for watcher in list(self.watchers):
            try:
                await watcher(node_name, taken)
            except Exception:
                logger.exception("[main] A node watcher failed on %r's heartbeat", node_name)

    def _take_unsigned(
        self, node_name: str, data: dict[str, Any], before: dict[str, Any] | None
    ) -> None:
        """Record an unsigned heartbeat as a sign of life, and believe nothing else in it.

        Refusing it outright would read as the node going silent, and main gives
        up on a silent node's agents and deletes them, so a node that needs
        deploying again would lose its agents for want of a signature. Taken
        whole, it would let anything that can publish there say which agents
        the node runs, and have the others pruned. So the node stays online and
        its agents stay as main last knew them, until it is deployed again.
        """
        record = dict(before or {})
        record.update(
            {
                "last_seen": time.time(),
                "agents": list(record.get("agents", [])),
                "version": data.get("version"),
                "runtime": data.get("runtime") or DEFAULT_NODE_RUNTIME,
                "signing": data.get("signing") or "off",
                "signed": False,
            }
        )
        self.known[node_name] = record

    def _say_unsigned(self, node_name: str) -> None:
        """Say in chat, once per node, that its heartbeats are unsigned and what main does about it."""
        host = self.host
        if host is None or node_name in self.unsigned_reported:
            return
        self.unsigned_reported.add(node_name)
        if NODE_SIGNING == "enforce":
            outcome = (
                "so main ignores what it says about its agents and takes no reply, manifest "
                "or report from it (WACTORZ_NODE_SIGNING=enforce)"
            )
        else:
            outcome = "and main takes them anyway because WACTORZ_NODE_SIGNING=warn"
        logger.warning("[main] Node %r sends unsigned heartbeats, %s", node_name, outcome)
        host._queue_notification(
            {
                "severity": "warning",
                "about": _unsigned_notice(node_name),
                "message": (
                    f"Node '{node_name}' does not sign what it publishes, {outcome}. It was "
                    f"deployed before nodes signed their messages. Deploy it again: "
                    f"/deploy {node_name}"
                ),
            }
        )

    def _now_signed(self, node_name: str) -> None:
        """A node said to be unsigned has signed: what was said is no longer so.

        A notice still queued is dropped, since it would reach the chat after
        the deploy that made it untrue. One already shown was true when it was.
        """
        if node_name not in self.unsigned_reported:
            return
        self.unsigned_reported.discard(node_name)
        if self.host is not None:
            self.host._withdraw_notifications(_unsigned_notice(node_name))

    def follow_restarts(
        self, node_name: str, record: dict[str, Any], before: dict[str, Any] | None
    ) -> None:
        """Say in chat when an agent on a node starts or stops crashing repeatedly.

        On main the supervisor tells main itself; a node's supervisor has no main
        to tell, so the node lists these agents in its heartbeat and main tells
        the user what changed. Once each way, not on every heartbeat.
        """
        host = self.host
        if host is None:
            return
        now = set(record.get("slow_retry", []))
        was = set((before or {}).get("slow_retry", []))
        for name in sorted(now - was):
            host._queue_notification(
                {
                    "severity": "critical",
                    "message": (
                        f"🚨 **{name}** on node '{node_name}' keeps crashing. It will keep "
                        "being restarted, but less often, with the wait growing up to an "
                        "hour. Fix its code, or delete it, if it is not going to recover "
                        "on its own."
                    ),
                }
            )
        # Only those still there, on a runner that has not restarted: one
        # deleted is not a recovery, and a runner that restarted starts every
        # agent with a clean slate, recovered or not.
        if before is None or _runner_restarted(record, before):
            return
        for name in sorted((was - now) & set(record.get("agents", []))):
            host._queue_notification(
                {
                    "severity": "info",
                    "message": (
                        f"✅ **{name}** on node '{node_name}' has stayed up since its last "
                        "restart; it is back to normal supervision."
                    ),
                }
            )

    async def follow_signing(
        self, node_name: str, record: dict[str, Any], before: dict[str, Any] | None
    ) -> None:
        """Act on what a heartbeat says about signing.

        Two things need main. A node that received a control message not signed for
        it is being sent something main did not sign, and nobody reads a Pi's log,
        so it is said in chat. And a node that checks signatures needs a signed
        desired state: the one retained at the broker may predate signing, or be
        forged, and a node that refuses it does not bring its agents back. Main
        publishes its own over it -- when the node first says it checks, and again
        after it reports a failure.
        """
        host = self.host
        if host is None:
            return
        mode = record.get("signing", "off")
        failures = record.get("signing_failures", 0)
        was_mode = (before or {}).get("signing", "off")
        was_failures = (before or {}).get("signing_failures", 0)
        # A runner that restarted counts from zero again.
        new_failures = failures - was_failures if failures >= was_failures else failures
        if mode == "invalid":
            if was_mode != "invalid":
                host._queue_notification(
                    {
                        "severity": "critical",
                        "message": (
                            f"Node '{node_name}' cannot read its signing key, so it refuses "
                            f"everything main sends it. Deploy it again: /deploy {node_name}"
                        ),
                    }
                )
            return
        if new_failures:
            outcome = "refused" if mode == "enforce" else "acted on"
            logger.warning(
                "[main] Node %r received %d control message(s) not signed for it, and %s them.",
                node_name,
                new_failures,
                outcome,
            )
            host._queue_notification(
                {
                    "severity": "warning",
                    "message": (
                        f"Node '{node_name}' received {new_failures} command(s) that were not "
                        f"signed for it, and {outcome} them. A desired state published before "
                        "the node was redeployed does this once, and main republishes it "
                        "signed; if it keeps happening, something other than this Wactorz "
                        "is publishing to the node."
                    ),
                }
            )
        checks = mode in ("warn", "enforce")
        if checks and (was_mode != mode or new_failures) and self._signing_republish_due(node_name):
            await host._update_node_desired_state(node_name)

    def _signing_republish_due(self, node_name: str) -> bool:
        now = time.monotonic()
        last = self.signing_republished_at.get(node_name)
        if last is not None and now - last < SIGNING_REPUBLISH_INTERVAL_S:
            return False
        self.signing_republished_at[node_name] = now
        return True

    def agents_to_prune(self, node_name: str, current: set[str], previous: set[str]) -> list[str]:
        """Registry agents on this node that have now missed enough heartbeats.

        Presence resets the count, absence increments it. An agent that has not
        appeared yet is never started on — a freshly spawned one is absent
        because its first heartbeat is still coming, not because it died. An
        agent the registry now places elsewhere has migrated, and is not this
        node's to lose.
        """
        host = self.host
        if host is None or node_name in self.redeploying:
            return []
        to_prune: list[str] = []
        for agent_name, cfg in list(host._get_spawn_registry().items()):
            if (cfg.get("node", "") or "").strip() != node_name:
                continue
            key = (node_name, agent_name)
            if agent_name in current:
                self.agent_misses.pop(key, None)
                continue
            if key not in self.agent_misses and agent_name not in previous:
                continue
            misses = self.agent_misses.get(key, 0) + 1
            if misses >= VANISH_MISS_THRESHOLD:
                self.agent_misses.pop(key, None)
                to_prune.append(agent_name)
            else:
                self.agent_misses[key] = misses
        return to_prune

    async def _prune_vanished(self, node_name: str, current: set[str], previous: set[str]) -> None:
        """Delete agents the node has stopped reporting.

        The same cleanup as a manual delete, minus the stop signal — there is
        nothing left on the node to send it to.
        """
        host = self.host
        if host is None:
            return
        for agent_name in self.agents_to_prune(node_name, current, previous):
            logger.warning(
                "[main] Agent %r silently disappeared from node %r (crash/kill suspected)",
                agent_name,
                node_name,
            )
            host._remove_from_spawn_registry(agent_name)
            # The node's retained desired_state is what it reconciles against on
            # its next reboot. Dropping the spawn entry without rewriting it
            # leaves the instruction to start this agent in place, so the node
            # brings it back to a main that no longer has a registry entry for
            # it: running and heartbeating, but unsupervised and unmanaged.
            await host._update_node_desired_state(node_name, remove_name=agent_name)
            await host._clear_agent_manifest(agent_name)
            host._record_agent_deletion(
                agent_name,
                reason=f"vanished from node '{node_name}' (crash or external kill)",
            )

    def _bootstrap_contracts(self, node_name: str, agents: list[str]) -> None:
        """Give the planner something for agents whose manifest has not arrived.

        The manifest is authoritative: it carries the agent's real topics and
        schemas. A spawn config only records what was asked for, which may never
        have been true, so it is used where there is nothing better and never
        over a manifest-derived contract.
        """
        from ...core.topic_bus import TopicContract, get_topic_bus

        host = self.host
        if host is None:
            return
        try:
            bus = get_topic_bus()
            if not bus:
                return
            spawn_registry = host._get_spawn_registry()
            for name in agents:
                if bus.registry.get(name) is not None:
                    continue
                if name in self.manifest_registry.manifests:
                    continue
                cfg = spawn_registry.get(name)
                if not cfg:
                    continue
                if not (cfg.get("publishes") or cfg.get("subscribes")):
                    continue
                contract = TopicContract.from_spawn_config(
                    {**cfg, "node": node_name, "actor_id": remote_actor_id(name)}
                )
                bus.register_contract(contract)
                logger.debug(
                    "[main] Bootstrapped TopicContract for %r from spawn config "
                    "(manifest pending).",
                    name,
                )
        except Exception as exc:
            logger.debug("[main] TopicContract bootstrap failed: %s", exc)

    def _touch_monitor(self, agents: list[str]) -> None:
        """Keep the monitor's heartbeat tracker current for remote agents.

        Without this the monitor raises a silence alert for every remote agent,
        since nothing else tells it they are alive.
        """
        host = self.host
        if host is None or not host._registry:
            return
        monitor = host._registry.find_by_name("monitor")
        if not monitor or not hasattr(monitor, "_last_seen"):
            return
        now = time.time()
        for name in agents:
            monitor._last_seen[remote_actor_id(name)] = now

    def report_migration(self, data: dict[str, Any]) -> None:
        """Turn a node's failed migration into a notification.

        A success is not announced from here. The node reports one once it has
        handed the agent over, before main has placed it anywhere, and main
        announces the migration itself when the agent is confirmed running --
        so the user hears of each migration once, and only when it is true.
        """
        host = self.host
        if host is None or data.get("success", False):
            return
        host._queue_notification(
            {
                "_monitor_notification": True,
                "message": f"Migration of '{data.get('agent', '?')}' failed: {data.get('error', '?')}",
                "severity": "warning",
                "timestamp": time.time(),
            }
        )

    async def _node_offline_watcher(self) -> None:
        """Periodically check for nodes that have gone silent. If a node has not
        sent a heartbeat in OFFLINE_GRACE_S, treat all its agents as gone
        and drop the node from our tracking.

        Uses a longer threshold (90s) than the visual "offline" indicator (30s)
        so brief network blips don't trigger false deletion notes. The visual
        indicator stays at 30s for snappy UX; this watcher waits long enough
        to be sure the node is genuinely down.
        """
        host = self.host
        if host is None:
            return

        while host.state.value not in ("stopped", "failed"):
            try:
                await asyncio.sleep(OFFLINE_CHECK_INTERVAL_S)
                await self.forget_offline_nodes(time.time())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[%s] Node offline watcher error: %s", host.name, e)

    async def forget_offline_nodes(self, now: float) -> None:
        """Treat every node silent for longer than OFFLINE_GRACE_S as gone.

        Its agents are removed as a delete removes them, and a stop is queued for
        each on the node. The node is away, so the broker holds the stop in its
        session until it returns, behind whatever already waits there -- a spawn
        published while it was gone included. On its return the node stops what
        main has already forgotten, instead of starting a withdrawn spawn or
        carrying on with agents nothing supervises any more.
        """
        host = self.host
        if host is None:
            return

        # Snapshot to avoid mutation-during-iteration
        stale_nodes = [
            (name, info)
            for name, info in list(self.known.items())
            if (now - info.get("last_seen", 0)) > OFFLINE_GRACE_S and name not in self.redeploying
        ]
        if not stale_nodes:
            return

        reg = host._get_spawn_registry()
        for node_name, _info in stale_nodes:
            logger.warning(
                "[main] Node %r has been silent for >%.0fs — treating as offline",
                node_name,
                OFFLINE_GRACE_S,
            )
            # Find all agents that belong to this node according to the
            # spawn registry (the heartbeat's last-known agent list may
            # be stale).
            lost = [n for n, cfg in reg.items() if cfg.get("node", "").strip() == node_name]
            for agent_name in lost:
                host._remove_from_spawn_registry(agent_name)
                # As in _prune_vanished: the retained desired_state has
                # to lose the agent too, or the node resurrects it on
                # its next reconcile into a main that has forgotten it.
                await host._update_node_desired_state(node_name, remove_name=agent_name)
                # Queued in the node's session while it is away, behind
                # anything already waiting there, so on its return it
                # stops this agent rather than starting a withdrawn spawn.
                await host._mqtt_publish(f"nodes/{node_name}/stop", {"name": agent_name}, qos=1)
                await host._clear_agent_manifest(agent_name)
                host._record_agent_deletion(
                    agent_name,
                    reason=f"node '{node_name}' went offline",
                )
            # Drop the node from our tracking. If it comes back, the
            # heartbeat listener will re-add it as a fresh entry.
            self.known.pop(node_name, None)
            # A withdrawal held for the node's word is decided: it will not speak again.
            await host.settle_withdrawals(node_name, set())
            if lost:
                host._queue_notification(
                    {
                        "_monitor_notification": True,
                        "message": (
                            f"Node '{node_name}' is offline. Lost agents: {', '.join(lost)}."
                        ),
                        "severity": "warning",
                        "timestamp": now,
                    }
                )
