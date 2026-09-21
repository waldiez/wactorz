"""A Wactorz agent, running on a node.

:class:`NodeAgent` *is* a :class:`~wactorz.agents.DynamicAgent` — the same class
main runs, compiling the same generated code against the same ``agent`` API. A
node used to carry its own lightweight retelling of that contract, and the two
drifted: a program that worked on main would reach for ``agent.run_in_background``
or ``agent.send_to_many`` on a node and find nothing there.

What this subclass adds is the four things that are true only on a node:

- **Where its messages go.** There is no ``MQTTPublisher`` here, so the actor
  publishes through the runner's bounded queue.
- **Where its memory lives.** JSON under the node's state directory, so it
  survives a reboot and can be shipped to another machine by a migration.
- **What answers its LLM calls.** Main does, over the broker — see :mod:`.llm`.
- **How it is asked to do something.** A task arrives on an MQTT topic rather
  than in a mailbox, and the reply goes back the same way.
"""

import asyncio
import logging
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agents.dynamic.agent import DynamicAgent, _ProgramHalted
from ..core.actor import forbidden
from ..core.topic_bus import TopicContract
from .llm import BridgeProvider, request_over_mqtt
from .state import JsonState, state_path

if TYPE_CHECKING:
    from .runner import NodeRunner

logger = logging.getLogger(__name__)


class NodeAgent(DynamicAgent):
    """A dynamic agent whose host is a node rather than main."""

    def __init__(
        self, config: dict, runner: "NodeRunner", state_dir: str | Path | None = None
    ) -> None:
        name = config.get("name") or f"remote-agent-{uuid.uuid4().hex[:6]}"
        directory = Path(state_dir) if state_dir else Path(runner.state_dir)
        super().__init__(
            code=config.get("code", ""),
            poll_interval=float(config.get("poll_interval", 5.0)),
            description=config.get("description", ""),
            input_schema=config.get("input_schema"),
            output_schema=config.get("output_schema"),
            trusted=bool(config.get("trusted", False)),
            # Passed in rather than assigned after, because `AgentAPI` is built
            # inside this call and decides there whether `agent.llm` exists. It
            # only holds the reference, so handing it a half-built agent is safe.
            llm_provider=BridgeProvider(self),
            name=str(name),
            persistence_dir=str(directory),
        )
        self._runner = runner
        self._config = config
        #: What `agent.node` reports, and what every heartbeat carries.
        self._node = runner.node_name
        #: The broker this node dials, which generated code opening a
        #: connection of its own reads through `agent`.
        self._mqtt_broker = runner.broker
        self._mqtt_port = runner.port
        #: The runner's publish queue, standing in for an aiomqtt client. Every
        #: `Actor._mqtt_publish` on this agent lands there.
        self._mqtt_client = runner.publisher
        #: What the spawn said this agent does, so the planner sees it before
        #: the agent's own code has declared or published anything.
        self.capabilities = list(config.get("capabilities") or [])
        self._spawn_contract = TopicContract.from_spawn_config({**config, "node": self._node})
        self._state_file = JsonState(state_path(directory, str(name)), str(name))
        self._apply_initial_state(config)

    # ── State ─────────────────────────────────────────────────────────────────

    def _apply_initial_state(self, config: dict) -> None:
        """Take the state this agent starts with, from a migration or from disk.

        Two cases have to be told apart. A plain start or restart has no
        ``_initial_state``: the agent ran here before, and whatever is on disk
        is its memory. A migration arrival has one, and it is authoritative --
        the source node was running this agent moments ago, so any file here is
        from an older incarnation that lived here before it moved away. Letting
        that file win is what produced ghost memory and duplicated conversation
        history when an agent was migrated back to a node it once lived on.
        """
        initial = config.pop("_initial_state", None)
        if not (initial and isinstance(initial, dict)):
            self._persistent_state = self._state_file.load()
            return
        if self._state_file.path.exists():
            logger.info(
                "[%s] Migration: overwriting the stale state file at %s with %s key(s) "
                "shipped from the source node",
                self.name,
                self._state_file.path,
                len(initial),
            )
            self._state_file.delete()
        self._persistent_state = dict(initial)
        self._state_file.save(self._persistent_state)
        logger.info(
            "[%s] Restored %s state key(s) from migration: %s",
            self.name,
            len(initial),
            list(initial),
        )

    async def _load_persistent_state(self) -> None:
        """Already read in the constructor, from the node's JSON file."""

    async def _save_persistent_state(self) -> None:
        self._state_file.save(self._persistent_state)

    def persist(self, key: str, value: Any) -> None:
        self._persistent_state[key] = value
        self._state_file.save(self._persistent_state)

    def recall(self, key: str, default: Any = None) -> Any:
        return self._persistent_state.get(key, default)

    def delete_state(self) -> bool:
        """Remove this agent's memory for good. True when there was any.

        In-memory state goes first, so a save still in flight -- from a
        supervisor restart, or the agent's own stop -- cannot write the file
        back after it is gone.
        """
        self._persistent_state = {}
        removed = self._state_file.delete()
        # `Actor.__init__` makes a directory per agent for the pickle store this
        # one does not use -- its memory is the flat JSON file above. Left
        # behind, every agent ever spawned here leaves an empty directory on a
        # machine chosen for being small. `rmdir`, so anything unexpectedly
        # inside it survives to be looked at.
        try:
            self._persistence_dir.rmdir()
        except OSError:
            logger.debug("[%s] Left %s in place", self.name, self._persistence_dir)
        return removed

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def node(self) -> str:
        return self._node

    # ── Talking to main ───────────────────────────────────────────────────────

    async def ask_main(self, topic: str, payload: dict[str, Any], timeout: float) -> Any:
        """Publish a request to main and wait for the reply it sends back."""
        return await request_over_mqtt(self, topic, payload, timeout)

    def deliver_reply(self, reply_topic: str, data: Any) -> bool:
        """Resolve whatever call was waiting on ``reply_topic``. True if one was."""
        future = self._result_futures.get(reply_topic)
        if future is not None and not future.done():
            future.set_result(data)
            return True
        return False

    # ── Tasks, which arrive on a topic rather than in a mailbox ───────────────

    async def run_task(self, payload: Any) -> Any:
        """Run the generated ``handle_task`` and return what it produced.

        The node's half of what ``_invoke_handle_task`` does for a task that
        arrived in the mailbox: the call itself is shared, and only the reply
        differs -- there it goes back as a RESULT message, here the runner
        publishes it to the topic the caller named.
        """
        if not self._fn_handle_task:
            return {"error": f"Agent '{self.name}' has no handle_task function."}
        try:
            result = await self._run_handle_task(payload)
        except asyncio.TimeoutError:
            logger.exception(
                "[%s] handle_task() timed out after %ss", self.name, self._HANDLE_TASK_TIMEOUT
            )
            return {
                "error": f"handle_task() timed out after {self._HANDLE_TASK_TIMEOUT}s",
                "error_phase": "handle_task",
                "agent": self.name,
            }
        except _ProgramHalted as halted:
            if isinstance(halted.original, KeyboardInterrupt):
                raise halted.original from None
            return await self._task_failed(halted.original)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        # BaseException for the same reason as the process loop: a `sys.exit()`
        # in generated code must end the task, not the node.
        except BaseException as e:
            return await self._task_failed(e)
        else:
            return result or {}

    async def _task_failed(self, error: BaseException) -> dict[str, Any]:
        """Report a failed task where an operator will see it, and answer the caller."""
        details = traceback.format_exc()
        # `exc_info` explicitly rather than `logger.exception`: every caller is
        # an except block, but this is a function call away from one, and
        # `exception` outside a handler logs "NoneType: None" for the traceback.
        logger.error("[%s] handle_task() error", self.name, exc_info=error)
        await self._publish_error(phase="handle_task", error=error, traceback_str=details)
        return {"error": str(error), "error_phase": "handle_task", "agent": self.name}

    def _persist_fixed_code(self, fixed_code: str) -> Any:
        """Keep a repair where everything that rebuilds this agent will see it.

        The base class writes it to main's spawn registry and to the
        supervisor's factory. The registry is not reachable from here — it is
        main's, and a node pushing code into it unasked is the lateral path
        this system deliberately closed. What a node can do is remember the
        program on its own behalf, which is what a restart here and a migration
        back to main both read.
        """
        self._runner.remember_code(self.name, fixed_code)
        return super()._persist_fixed_code(fixed_code)

    # ── Commands, which main may send straight to the agent ───────────────────

    async def apply_command(self, command: str) -> bool:
        """Carry out a command, through the node that is running this agent.

        A node's agents hold a command listener of their own, so `stop` and
        `delete` can arrive on `agents/<id>/commands` without passing through
        the node's control plane. The base class does the right thing for an
        agent on main and only half of it here: a stop would leave the agent in
        this node's registry — still in its heartbeat, still refusing a respawn
        without `replace` — and a delete would leave the state file and the
        retained topics behind, which is exactly what makes a deleted agent
        come back. Both are routed to the node instead, which owns that
        bookkeeping.
        """
        if command in ("stop", "delete") and not forbidden(
            command, protected=self.protected, essential=self.essential
        ):
            await self._runner.stop_agent(self.name, delete=command == "delete")
            return True
        return await super().apply_command(command)

    # ── Ending ────────────────────────────────────────────────────────────────

    async def end_self(self) -> None:
        """End this agent, and take it off the node that was running it.

        The runner is told first. ``stop()`` below cancels this agent's tasks,
        and when a program ends itself the call is coming from one of them --
        `Actor._wind_down_tasks` spares the task it runs in, but nothing
        guarantees the same for a caller further down. Dropping the registry
        entry before that means the node's view is right even if this call does
        not return.
        """
        self._runner.forget(self.name)
        await super().end_self()
