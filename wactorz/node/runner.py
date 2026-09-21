"""The long-running process on an edge node.

It holds one connection for control messages from main, one for publishing, a
registry of the agents running here and a supervisor over them. Everything it
does is in answer to something on ``nodes/<name>/…`` — spawn an agent, stop one,
reconcile against a desired state after a reboot, hand an agent's state back so
main can move it somewhere else.

The agents themselves are ordinary :class:`~wactorz.agents.DynamicAgent`
instances (see :mod:`.agent`), registered in an ordinary
:class:`~wactorz.core.registry.ActorRegistry` and watched by the ordinary
:class:`~wactorz.core.registry.Supervisor`. So a node gets the same OTP
restart semantics as main, agents on one node reach each other in process
through the registry, and there is no second supervisor to keep in step.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from .. import __version__
from ..config import CONFIG
from ..core.actor import Actor, SupervisorStrategy
from ..core.cancellation import cancel_all_until_done
from ..core.mqtt import SERVER_SESSION_EXPIRY_SECONDS, client_id, mqtt_client, session_kwargs
from ..core.mqtt_tls import tls_enabled
from ..core.node_signing import CONTROL_LEAVES
from ..core.pip import install_command, install_destination, is_installable_name
from ..core.registry import ActorRegistry, Supervisor
from .agent import NodeAgent
from .publishing import NodePublisher
from .signing import CLEARABLE_LEAVES, ControlGuard, message_bytes
from .state import json_safe

logger = logging.getLogger(__name__)

#: How long a node waits for a pip install an agent asked for. Long enough to
#: build a wheel from source on a slow board over a slow link.
INSTALL_TIMEOUT_S = 900.0

#: How long a shutdown waits for the node's own loops to unwind before it
#: reports them and exits regardless. Generous next to the work they do on the
#: way out, short next to a service manager's own patience.
SHUTDOWN_TIMEOUT_S = 5.0

#: What kind of process is speaking on the node topics. A heartbeat names it so
#: main can tell a node running the package from one still running the old
#: single-file runner, which said ``runner``.
NODE_RUNTIME = "node"

#: The retained topics an agent leaves behind, cleared when it is deleted.
AGENT_RETAINED_TOPICS = (
    "status",
    "heartbeat",
    "metrics",
    "logs",
    "spawned",
    "manifest",
    "errors",
    "detections",
    "results",
    "completed",
)


class NodeRunner:
    """Connects to the broker, listens for control messages, manages agents."""

    def __init__(
        self, broker: str, port: int, node_name: str, state_dir: str | None = None
    ) -> None:
        self.broker = broker
        self.port = port
        self.node_name = node_name
        self.publisher = NodePublisher(broker, port, node_name)
        self._running = False
        self._start_time: float = time.time()
        #: Control commands running off the subscriber loop, held so none is
        #: collected mid-flight.
        self._commands: set[asyncio.Task] = set()
        #: The long-running loops `run` owns, so a shutdown can end them.
        self._loops: list[asyncio.Task] = []
        self.registry = ActorRegistry()
        self.supervisor = Supervisor(self.registry, self._inject)
        # The back-reference `ActorSystem` gives the registry on main. Without
        # it an agent here cannot reach its own supervisor, and two things go
        # quiet rather than wrong: a repaired program never reaches the factory,
        # so a restart rebuilds the broken one and repairs it again; and a
        # deliberate stop never releases supervision.
        self.registry._supervisor_ref = self.supervisor
        #: name → the config it was spawned from, so a restart can use it again.
        self._configs: dict[str, dict] = {}
        state_path = self._resolve_state_dir(state_dir)
        self.state_dir = str(state_path)
        # Read from the environment rather than a flag, like the broker
        # credentials, so the key appears in no process listing. Left in the
        # environment: a restart re-executes this process and needs it again.
        self._control = ControlGuard(
            os.environ.get("WACTORZ_NODE_KEY", ""),
            os.environ.get("WACTORZ_CONTROL_SINCE", ""),
            os.environ.get("WACTORZ_NODE_SIGNING", ""),
            self.state_dir,
        )

    @staticmethod
    def _resolve_state_dir(state_dir: str | None) -> Path:
        """Where this node keeps what must survive a reboot.

        Under the node's own directory rather than the working one, because a
        runner started by systemd and one started by hand must find the same
        agents' memory.

        Named, not created: a runner is built in a test without wanting a
        directory anywhere. Whoever writes there makes it -- see `JsonState`
        and the control guard.
        """
        return Path(state_dir) if state_dir else Path.home() / "wactorz" / "state"

    def _inject(self, actor: Actor) -> None:
        """Give a supervised actor this node's broker connection and identity."""
        actor._mqtt_client = self.publisher
        actor._mqtt_broker = self.broker
        actor._mqtt_port = self.port
        actor._node = self.node_name

    # ── Publishing ────────────────────────────────────────────────────────────

    async def publish(self, topic: str, data: Any, retain: bool = False) -> None:
        """Queue a message for the publisher loop. Never waits for room."""
        await self.publisher.publish(topic, data, retain=retain)

    # ── The agents running here ───────────────────────────────────────────────

    @property
    def agents(self) -> dict[str, NodeAgent]:
        """The node's agents by name, as the control plane thinks of them."""
        return {
            actor.name: actor
            for actor in self.registry.all_actors()
            if isinstance(actor, NodeAgent)
        }

    def get(self, name: str) -> NodeAgent | None:
        actor = self.registry.find_by_name(name)
        return actor if isinstance(actor, NodeAgent) else None

    def remember_code(self, name: str, code: str) -> None:
        """Record that an agent's program has changed under it.

        An agent can repair its own code, and the node keeps the config it
        would rebuild that agent from — which a restart reads, and which a
        migration hands to main. Both would otherwise pass on the program that
        failed, so the repair would be undone by the next thing that touched
        the agent, and paid for again.

        The stored config is the one the supervisor's factory closed over, so
        updating it here is also what a supervisor-driven restart sees.
        """
        config = self._configs.get(name)
        if config is None or not code or config.get("code") == code:
            return
        config["code"] = code
        # Main is told that something changed, and nothing more. The program
        # itself travels only when main asks for it, because code a node
        # volunteered would end up in the spawn registry and from there on
        # another machine. A forged notice costs one question and one answer.
        self._background(
            self.publish(
                f"nodes/{self.node_name}/code_changed",
                {"agent": name, "node": self.node_name, "timestamp": time.time()},
            ),
            "code_changed",
        )

    def forget(self, name: str) -> None:
        """Drop an agent that has ended itself, without stopping it again."""
        self._configs.pop(name, None)
        self.supervisor.drop_supervised(name)

    async def spawn_agent(self, config: Any) -> None:
        if not isinstance(config, dict):
            logger.warning("[runner] spawn_agent: invalid config type %s, ignoring.", type(config))
            return
        name = config.get("name", f"agent-{uuid.uuid4().hex[:6]}")
        logger.info("[runner] Spawning agent '%s'...", name)
        if self.get(name) is not None:
            if config.get("replace", False):
                logger.info("[runner] Replacing agent '%s'", name)
                await self.stop_agent(name)
            else:
                logger.info("[runner] Agent '%s' already running (use replace=true)", name)
                return

        packages = config.get("install", [])
        if packages:
            refused = await self._install_packages(packages)
            if refused:
                # Abort, unlike the pip-failure path below it, which warns and
                # carries on. A pip failure can be transient and may still leave
                # a usable environment; a refusal means we read the request and
                # rejected it, so starting the agent only moves the failure to an
                # import somewhere else — with the reason left in this node's log
                # and nothing on the dashboard saying why.
                logger.error("[runner] Not spawning '%s' — install list refused.", name)
                await self._log_to_dashboard(
                    "error",
                    f"Refused to spawn '{name}': these are not package names: {refused}",
                )
                return

        try:
            self._configs[name] = config
            await self.supervisor.start_supervised(
                name,
                lambda: NodeAgent(config, self, state_dir=self.state_dir),
                strategy=SupervisorStrategy.ONE_FOR_ONE,
                max_restarts=int(config.get("max_restarts", 5)),
                restart_delay=float(config.get("restart_delay", 3.0)),
            )
            logger.info("[runner] Agent '%s' started.", name)
        except Exception as e:
            logger.exception("[runner] Failed to start agent '%s'", name)
            self.forget(name)
            await self._log_to_dashboard("error", f"Failed to start '{name}': {e}")
            return

        await self.publish(
            f"agents/{self.node_name}/logs",
            {
                "type": "spawned",
                "message": f"Remote agent '{name}' started on {self.node_name}",
                "child_name": name,
                "node": self.node_name,
                "timestamp": time.time(),
            },
        )

        # A migration is waiting on this. The ack says the *process started*,
        # nothing more: a heartbeat or a manifest would conflate arrival with
        # health, and roll back a migration that worked because the agent's own
        # code crashed a moment later -- which is a supervision matter.
        token = config.get("_migration_token")
        if token:
            await self.publish(
                f"nodes/{self.node_name}/spawn_ack",
                {
                    "agent": name,
                    "migration_token": token,
                    "node": self.node_name,
                    "timestamp": time.time(),
                },
                retain=False,
            )

    async def stop_agent(self, name: str, delete: bool = False) -> None:
        """Stop an agent, and with ``delete`` erase everything it leaves behind.

        A plain stop flushes the agent's state to disk and leaves it there, so
        the next spawn or runner restart picks it back up. A delete also removes
        the state file and clears the agent's retained MQTT topics, without
        which the broker re-delivers its last heartbeat and manifest to every
        subscriber that connects afterwards -- which is what made a deleted
        agent come back on the next restart.
        """
        agent = self.get(name)
        if agent is None:
            return
        # Remembered before the stop, in case the agent clears attributes.
        actor_id = agent.actor_id

        await self.supervisor.stop_supervised(name)
        self._configs.pop(name, None)

        if delete:
            # After the agent is fully stopped: its own shutdown writes the
            # state file, and a supervisor restart could recreate it.
            agent.delete_state()
            await self._purge_agent_retained(actor_id)
            logger.info("[runner] Agent '%s' permanently deleted from this node.", name)

    async def _purge_agent_retained(self, actor_id: str) -> None:
        """Clear the retained topics of an agent that has just been deleted.

        Empty-payload-with-retain is the MQTT idiom for removing a retained
        topic. The runner does it because it has the connection open and knows
        the actor id, so the purge works even when main is unreachable.
        """
        for leaf in AGENT_RETAINED_TOPICS:
            try:
                await self.publish(f"agents/{actor_id}/{leaf}", b"", retain=True)
            except Exception as e:
                logger.debug(
                    "[runner] Failed to clear retained agents/%s/%s: %s", actor_id, leaf, e
                )

    async def stop_all(self) -> None:
        for name in list(self.agents):
            await self.stop_agent(name)

    async def _install_packages(self, packages: list[str]) -> list[str]:
        """Install pip packages on this node. Returns the names it refused.

        The names arrive in a spawn payload off the broker, so they are treated
        as input: refused unless they look like package names, and passed as
        argv rather than through a shell. A non-empty return means nothing was
        installed and the caller should not start the agent.
        """
        refused = [p for p in packages if not is_installable_name(p)]
        if refused:
            # Refused as a whole, not filtered down to the acceptable ones:
            # installing a subset would report success for a request that was
            # not carried out, and the agent would then fail on a missing import
            # somewhere far from here.
            logger.error(
                "[runner] Refusing install — not package names: %s. Names may contain "
                "letters, digits, '.', '-', '_', extras and version specifiers; anything "
                "else (options, URLs, paths) is rejected.",
                refused,
            )
            return refused

        cmd, extra_env = install_command(packages)
        logger.info("[runner] Installing %s into %s.", " ".join(packages), install_destination())
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env={**os.environ, **extra_env} if extra_env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            # A pip that never returns would otherwise hold this spawn for ever,
            # and with it the control command that asked for it. Generous rather
            # than tight: these run on a Raspberry Pi building a wheel from
            # source over a slow link, and cutting off a slow-but-healthy
            # install only trades one failure for another.
            proc.kill()
            await proc.wait()
            logger.warning(
                "[runner] pip install of %s gave up after %gs.",
                " ".join(packages),
                INSTALL_TIMEOUT_S,
            )
            return []
        if proc.returncode != 0:
            logger.warning("[runner] pip install warning: %s", stderr.decode()[:200])
        return []

    async def _log_to_dashboard(self, kind: str, message: str) -> None:
        await self.publish(
            f"agents/{self.node_name}/logs",
            {
                "type": kind,
                "message": message,
                "node": self.node_name,
                "timestamp": time.time(),
            },
        )

    # ── The node's own heartbeat ──────────────────────────────────────────────

    def _node_identity(self) -> dict[str, Any]:
        """The fields every node heartbeat carries, whatever else it says.

        `version` and `runtime` are how main tells what is running here. A
        heartbeat without them comes from a runner too old to send them, and
        main reads it as the single-file runtime at an unknown version.
        """
        return {"node": self.node_name, "version": __version__, "runtime": NODE_RUNTIME}

    async def _node_heartbeat_loop(self, interval: float = 10.0) -> None:
        """Publish a heartbeat for the runner process itself, so the node appears."""
        node_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.node.{self.node_name}"))
        while self._running:
            try:
                agent_names = list(self.agents)
                try:
                    cpu_pct = psutil.cpu_percent(interval=None)
                    vm = psutil.virtual_memory()
                    mem_used = vm.used // (1024 * 1024)
                    mem_free = vm.available // (1024 * 1024)
                except Exception:
                    cpu_pct = mem_used = mem_free = 0
                await self.publish(
                    f"nodes/{self.node_name}/heartbeat",
                    {
                        **self._node_identity(),
                        "node_id": node_id,
                        "timestamp": time.time(),
                        "agents": agent_names,
                        "agent_count": len(agent_names),
                        "broker": self.broker,
                        "pid": os.getpid(),
                        "uptime_s": round(time.time() - self._start_time, 1),
                        "cpu_pct": cpu_pct,
                        "mem_used_mb": mem_used,
                        "mem_free_mb": mem_free,
                        # Whether this node checks what main sends it, and how
                        # often something arrived that was not signed for it.
                        "signing": self._control.mode,
                        "signing_failures": self._control.failures,
                        # Whether this node reaches the broker over TLS.
                        "tls": tls_enabled(CONFIG.mqtt_tls),
                    },
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(interval)

    # ── Control plane ─────────────────────────────────────────────────────────

    def _admit_control(self, topic_str: str, msg: Any) -> bool:
        """Whether a message may be acted on.

        Checked once here, for every command alike: a spawn runs code, a desired
        state starts agents and a stop can delete an agent's state, so none is
        cheaper to forge than another. The signature travels in the message's MQTT
        v5 user properties, over the payload bytes as they arrived, so a handler
        sees the payload untouched either way.
        """
        parts = topic_str.split("/")
        if len(parts) != 3 or parts[:2] != ["nodes", self.node_name]:
            return True
        leaf = parts[2]
        if leaf not in CONTROL_LEAVES:
            return True
        payload = message_bytes(msg.payload)
        if not payload and leaf in CLEARABLE_LEAVES:
            return True
        pairs = getattr(getattr(msg, "properties", None), "UserProperty", None) or []
        properties = {str(name): str(value) for name, value in pairs}
        return self._control.admit(leaf, topic_str, payload, properties)

    async def _dispatch_control(self, topic_str: str, data: Any, msg: Any) -> None:
        """Route one control message to the command it names.

        Exact topics are looked up; the two families that carry a variable
        segment — a reply's correlation key and an agent's name — are matched
        after, because a table cannot express them.
        """
        exact = {
            f"nodes/{self.node_name}/desired_state": self._on_desired_state,
            f"nodes/{self.node_name}/spawn": self._on_spawn,
            f"nodes/{self.node_name}/stop": self._on_stop,
            f"nodes/{self.node_name}/migrate": self._on_migrate,
            f"nodes/{self.node_name}/stop_all": self._on_stop_all,
            f"nodes/{self.node_name}/restart": self._on_restart,
            f"nodes/{self.node_name}/restart_agent": self._on_restart_agent,
            f"nodes/{self.node_name}/list": self._on_list,
            f"nodes/{self.node_name}/code_request": self._on_code_request,
        }
        handler = exact.get(topic_str)
        if handler is not None:
            await handler(topic_str, data, msg)
        elif topic_str.startswith(f"nodes/{self.node_name}/reply/"):
            await self._on_reply(topic_str, data, msg)
        elif "/task" in topic_str:
            await self._on_task(topic_str, data, msg)

    async def _on_desired_state(self, topic_str: str, data: Any, msg: Any) -> None:
        """Start any agent named in the desired state that is not running."""
        if not msg.payload or not isinstance(data, dict):
            return
        desired = data.get("agents", [])
        if not desired:
            return
        logger.info("[runner] Reconciling desired state: %s", [a.get("name") for a in desired])

        for agent_config in desired:
            aname = agent_config.get("name")
            if not aname:
                continue
            if self.get(aname) is not None:
                logger.info("[runner] '%s' already running, skipping.", aname)
            else:
                logger.info("[runner] Reconcile: starting missing agent '%s'", aname)
                self._background(self.spawn_agent(agent_config), "reconcile")

    async def _on_spawn(self, topic_str: str, data: Any, msg: Any) -> None:
        if not msg.payload:  # empty = retain-clear message, ignore
            return
        self._background(self.spawn_agent(data), "spawn_agent")

    async def _on_stop(self, topic_str: str, data: Any, msg: Any) -> None:
        """Stop a named agent.

        Payload formats accepted:
          {"name": "foo"}                 plain stop, state preserved
          {"name": "foo", "delete": true} permanent delete: wipes the state file
                                          and the retained MQTT topics
          "foo"                           legacy bare name, plain stop
        """
        if isinstance(data, dict):
            name = data.get("name")
            do_delete = bool(data.get("delete", False))
        else:
            name = str(data)
            do_delete = False
        if name:
            self._background(self.stop_agent(name, delete=do_delete), "stop_agent")

    async def _on_migrate(self, topic_str: str, data: Any, msg: Any) -> None:
        """Move a running agent to another node.

        Payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
        """
        if isinstance(data, dict):
            self._background(self._migrate_agent(data), "migrate")

    async def _on_stop_all(self, topic_str: str, data: Any, msg: Any) -> None:
        logger.info("[runner] stop_all received — shutting down.")
        self._background(self.shutdown(), "shutdown")

    async def _on_restart(self, topic_str: str, data: Any, msg: Any) -> None:
        """Restart the runner in place: stop the agents, re-exec, same PID."""
        logger.info("[runner] Restart command received.")
        self._background(self._restart(), "restart")

    async def _on_restart_agent(self, topic_str: str, data: Any, msg: Any) -> None:
        """Restart one agent without losing its config — stop plus spawn."""
        name = data.get("name") if isinstance(data, dict) else str(data)
        self._background(self._restart_agent(str(name)), "restart_agent")

    async def _on_list(self, topic_str: str, data: Any, msg: Any) -> None:
        await self.publish(
            f"nodes/{self.node_name}/agents",
            {
                "node": self.node_name,
                "agents": [{"name": a.name, "actor_id": a.actor_id} for a in self.agents.values()],
                "timestamp": time.time(),
            },
        )

    async def _on_code_request(self, topic_str: str, data: Any, msg: Any) -> None:
        """Answer with the program an agent here is actually running.

        Main asks when it has been told the program changed, quoting a token it
        minted; the answer quotes it back. That exchange is what lets main act
        on code a node sent it -- the same rule a migration follows. Nothing is
        volunteered: a node that published code main had not asked for could
        write into the spawn registry, and from there onto another machine.

        A node holding a signing key answers only a request main signed, which
        the control guard has already checked by the time this runs. One with no
        key answers anyone -- as it acts on every other command from anyone --
        and that costs it nothing here: the program is in the spawn that placed
        the agent and in every heartbeat it sends, both of which the same
        listener can already read. The answer is not the disclosure; being able
        to make main *file* it would be, and that needs main's token.
        """
        if not isinstance(data, dict):
            return
        name = str(data.get("agent") or "")
        agent = self.get(name)
        if agent is None:
            logger.info("[runner] code_request for '%s', which is not running here", name)
            return
        await self.publish(
            f"nodes/{self.node_name}/code_return",
            {
                "agent": name,
                "token": data.get("token", ""),
                "code": agent._code,
                "node": self.node_name,
                "timestamp": time.time(),
            },
        )

    async def _on_reply(self, topic_str: str, data: Any, msg: Any) -> None:
        """Hand a reply to whichever agent is waiting on its topic."""
        agents = list(self.agents.values())
        for agent in agents:
            if agent.deliver_reply(topic_str, data):
                return
        # Every key actually waiting, so the mismatch is visible now rather
        # than as a timeout a minute later.
        waiting: list[str] = []
        for agent in agents:
            waiting.extend(agent._result_futures)
        logger.warning(
            "[runner] Reply arrived on %s but no agent had a matching pending future. "
            "Waiting keys: %r",
            topic_str,
            waiting,
        )

    async def _on_task(self, topic_str: str, data: Any, msg: Any) -> None:
        """Run a task addressed to a named agent, off the consuming loop.

        The subscriber is a sequential consumer, so awaiting the task here would
        stop every other message being dispatched. An agent whose task makes a
        round trip — publishing to main and awaiting the reply on this same
        client — could then never receive it: the loop holding the only consumer
        is the loop waiting for the call to finish. It deadlocks and times out a
        minute later although main answered in milliseconds.
        """
        parts = topic_str.split("/")  # agents/by-name/{agent_name}/task
        if len(parts) < 4:
            return
        agent_name = parts[2]
        agent = self.get(agent_name)
        if agent is None or not isinstance(data, dict):
            return

        # handle_task receives the full envelope, as an agent on main does.
        # Unwrapping data['payload'] here instead would hand agent code a bare
        # string where it expects a dict, so the same agent would work on main
        # and break on a node. Transport metadata is stripped so it does not
        # leak into what the agent sees.
        reply_topic = data.get("_reply_topic")
        payload: Any = {k: v for k, v in data.items() if k not in ("_reply_topic", "_remote_task")}
        # A scalar wrapped in 'payload' and nothing else passes through as the
        # scalar, which is what callers sending {'payload': 42} expect.
        if set(payload) == {"payload"} and not isinstance(payload["payload"], dict):
            payload = payload["payload"]

        task = asyncio.create_task(self._run_task(agent, payload, reply_topic))
        # Held so the task is not collected mid-flight, and so a shutdown can
        # cancel it with the rest of the agent's work.
        agent._tasks.append(task)
        task.add_done_callback(lambda t, _ts=agent._tasks: _ts.remove(t) if t in _ts else None)

    async def _run_task(self, agent: NodeAgent, payload: Any, reply_topic: str | None) -> None:
        """Run one task and publish the answer where the caller asked for it."""
        try:
            result = await agent.run_task(payload)
        except Exception as e:
            logger.exception("[runner] handle_task error for '%s'", agent.name)
            result = {"error": str(e), "agent": agent.name}
        if not reply_topic:
            return
        if not isinstance(result, dict):
            result = {"result": str(result) if result is not None else ""}
        try:
            await self.publish(reply_topic, result)
        except Exception as e:
            logger.warning(
                "[runner] Reply publish failed for '%s' → %s: %s", agent.name, reply_topic, e
            )

    def _background(self, coro: Any, what: str) -> None:
        """Run a command off the subscriber loop, and say so if it fails.

        Every control handler goes through here rather than calling
        `create_task` itself: a bare task drops its exception on the floor, so a
        spawn that raised looked from the outside exactly like one that was
        never sent.
        """
        task = asyncio.create_task(coro)
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)
        task.add_done_callback(
            lambda t: (
                None
                if t.cancelled() or t.exception() is None
                else logger.error("[runner] %s failed: %s", what, t.exception())
            )
        )

    async def _subscriber_loop(self) -> None:
        """Hold the control connection open, for as long as this node is up.

        Subscribes to the node's own control topics and to tasks addressed to an
        agent by name.
        """
        topics = [
            f"nodes/{self.node_name}/spawn",
            f"nodes/{self.node_name}/desired_state",  # reconciliation on reboot
            f"nodes/{self.node_name}/stop",
            f"nodes/{self.node_name}/stop_all",
            f"nodes/{self.node_name}/restart",  # restart the runner process in-place
            f"nodes/{self.node_name}/restart_agent",  # restart a single named agent
            f"nodes/{self.node_name}/migrate",
            f"nodes/{self.node_name}/list",
            f"nodes/{self.node_name}/code_request",
            f"nodes/{self.node_name}/reply/#",
            "agents/by-name/+/task",
        ]

        while self._running:
            try:
                async with mqtt_client(
                    self.broker,
                    self.port,
                    identifier=client_id("node", self.node_name),
                    # Durable: the broker holds control messages sent while this
                    # node was away, instead of dropping them on the floor.
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    for topic in topics:
                        await client.subscribe(topic, qos=1)
                    logger.info(
                        "[runner] Subscribed to control topics on node '%s'", self.node_name
                    )

                    async for msg in client.messages:
                        topic_str = str(msg.topic)
                        try:
                            data = json.loads(msg.payload.decode())
                        except Exception:
                            data = msg.payload.decode()
                        if self._admit_control(topic_str, msg):
                            await self._dispatch_control(topic_str, data, msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning("[runner] Subscriber disconnected: %s. Reconnecting in 3s...", e)
                    await asyncio.sleep(3)

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            "[runner] Starting node '%s' → broker %s:%s", self.node_name, self.broker, self.port
        )
        publisher_ready = asyncio.Event()
        tasks = self._loops = [
            asyncio.create_task(self.publisher.run(publisher_ready)),
            asyncio.create_task(self._node_heartbeat_loop()),
        ]
        # The queue has to exist before anything publishes into it, and it is
        # created inside the publisher's own task so it belongs to this loop.
        await publisher_ready.wait()
        await self.supervisor.start()
        tasks.append(asyncio.create_task(self._subscriber_loop()))
        logger.info("[runner] Node '%s' online.", self.node_name)

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # Deliberately swallowed, unlike the other places that cancel a task
            # they own. This is the top of the node — nothing awaits run(), it is
            # driven by run_until_complete — and consuming the cancellation is
            # what lets the cleanup below finish. Left pending, the first await
            # in stop_all() would re-raise and the node would exit without
            # stopping its agents.
            pass
        finally:
            # Through the supervisor, not `stop_all`: it stops its watch loop
            # first and waits for it, so a restart already in flight cannot
            # register a fresh agent into a node that is shutting down -- and
            # the loop is not left pending at exit.
            await self.supervisor.stop()
            self._configs.clear()
            self.publisher.stop()
            for t in tasks:
                t.cancel()
            self._loops = []

    async def _restart_agent(self, name: str) -> None:
        """Restart one agent without losing its config or its memory.

        The state file is left on disk, so the fresh instance reads it back on
        the way up.
        """
        config = self._configs.get(name)
        if config is None:
            logger.warning("[runner] restart_agent: '%s' not running here", name)
            await self._log_to_dashboard("error", f"restart_agent: '{name}' not found")
            return
        config = {**config, "replace": True}
        logger.info("[runner] Restarting agent '%s'", name)
        await self.spawn_agent(config)

    async def _restart(self) -> None:
        """Restart this process in place with ``os.execv`` — same PID, clean loop.

        Under systemd this is a graceful reload; without a process manager the
        process simply comes back by itself.
        """
        logger.info("[runner] Restarting runner process via os.execv …")
        await self.stop_all()
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {**self._node_identity(), "status": "restarting", "timestamp": time.time()},
        )
        # Let the queue drain before the process image is replaced.
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def shutdown(self) -> None:
        """Stop the agents, say the node is going, and let :meth:`run` return.

        No ``sys.exit`` here, although that is what this did: shutdown is driven
        from a signal handler and from a `stop_all` off the broker, and both run
        it as a task. ``SystemExit`` raised in a task does not end the process —
        it is stored on the task, reported at exit as "Task exception was never
        retrieved", and the loops carry on until something else stops them. The
        loops are cancelled instead, which unwinds `run` and returns from `main`
        with status 0. That status matters: `Restart=on-failure` is what lets
        `/nodes shutdown` actually stop a node rather than restart it.
        """
        self._running = False
        await self.stop_all()
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {**self._node_identity(), "status": "offline", "timestamp": time.time()},
        )
        # Let the queue drain, so the heartbeat reaches the broker before the
        # publisher is taken down with everything else.
        await asyncio.sleep(0.3)
        # Asked again while they keep running, rather than cancelled once: on
        # Python 3.10 and 3.11 a cancellation that lands inside a `wait_for` is
        # discarded, and the broker client waits that way. A lost request here
        # would leave `run` waiting on a gather that never completes -- a node
        # that was told to stop and simply does not.
        still_running = await cancel_all_until_done(self._loops, timeout=SHUTDOWN_TIMEOUT_S)
        if still_running:
            logger.warning(
                "[runner] %d loop(s) did not stop within %gs; exiting anyway.",
                len(still_running),
                SHUTDOWN_TIMEOUT_S,
            )

    # ── Migration ─────────────────────────────────────────────────────────────

    async def _migrate_agent(self, payload: dict[str, Any]) -> None:
        """Hand an agent's state back to main so it can be placed elsewhere.

        Only values that survive JSON travel — counters, calibration values,
        thresholds, timestamps, everything a typical agent stores. A numpy array
        or a cv2 capture is dropped with a warning; neither would survive a
        process restart either.

        payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
        """
        name = payload.get("name")
        target_node = payload.get("target_node")
        if not name or not target_node:
            logger.warning("[runner] migrate: missing 'name' or 'target_node' in payload")
            return

        agent = self.get(str(name))
        if agent is None:
            logger.warning("[runner] migrate: agent '%s' not running here", name)
            await self.publish(
                f"nodes/{self.node_name}/migrate_result",
                {
                    "success": False,
                    "error": f"Agent '{name}' not found on {self.node_name}",
                    "agent": name,
                    "timestamp": time.time(),
                },
            )
            return

        safe_state, dropped = json_safe(dict(agent._persistent_state))
        if dropped:
            logger.warning(
                "[runner] migrate '%s': dropping non-JSON state keys %s — they cannot "
                "travel over MQTT",
                name,
                dropped,
            )

        # `@main` is the sentinel from MainActor: do not spawn anywhere, stop
        # the agent and return its state, and main will place it itself.
        if target_node == "@main":
            await self._return_to_main(agent, payload, safe_state, dropped)
            return

        # Node-to-node migration used to happen here: this runner published
        # `nodes/{target}/spawn` directly. That is a lateral-RCE path -- generated
        # code on one node could spawn code on another. A node holds only its own
        # signing key, so a node holding one would refuse such a spawn anyway.
        # Migration is routed through main instead, the one party that can sign
        # for every node.
        logger.warning(
            "[runner] migrate '%s' to '%s': node-to-node migration is not "
            "supported; main routes migrations. Ignoring.",
            name,
            target_node,
        )
        await self.publish(
            f"nodes/{self.node_name}/migrate_result",
            {
                "success": False,
                "error": "node-to-node migration is routed through main",
                "agent": name,
                "from_node": self.node_name,
                "to_node": target_node,
                "timestamp": time.time(),
            },
        )

    async def _return_to_main(
        self,
        agent: NodeAgent,
        payload: dict[str, Any],
        safe_state: dict[str, Any],
        dropped: list[str],
    ) -> None:
        """Stop an agent and publish its config and state for main to re-place."""
        name = agent.name
        logger.info(
            "[runner] Migrating '%s' from %s → local (main); returning %s state key(s)",
            name,
            self.node_name,
            len(safe_state),
        )
        # The config is taken before the stop, and says where it came from so
        # main can see the origin and strip it.
        return_config = dict(self._configs.get(name, agent._config))
        return_config["node"] = self.node_name
        return_config.pop("_initial_state", None)
        return_config.pop("replace", None)
        # Stopped, but the state file is kept. Main deletes it with an explicit
        # `stop {"delete": true}` once the agent is confirmed running somewhere
        # else. Deleting here would mean a migration that fails after this point
        # has nothing to roll back to -- the snapshot in flight would be the only
        # copy, and a dropped message would lose the agent outright.
        await self.stop_agent(name)
        await asyncio.sleep(0.3)
        await self.publish(
            f"nodes/{self.node_name}/state_return",
            {
                "agent": name,
                "return_token": payload.get("return_token", ""),
                "config": return_config,
                "state": safe_state,
                "state_keys_dropped": dropped,
                "from_node": self.node_name,
                "timestamp": time.time(),
            },
        )
        await self.publish(
            f"nodes/{self.node_name}/migrate_result",
            {
                "success": True,
                "agent": name,
                "from_node": self.node_name,
                "to_node": "local",
                "state_keys_transferred": list(safe_state),
                "state_keys_dropped": dropped,
                "timestamp": time.time(),
            },
        )
        logger.info("[runner] Migration of '%s' to local (main) dispatched.", name)
