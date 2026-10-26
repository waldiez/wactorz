"""A Wactorz agent, running on a node.

:class:`NodeAgent` *is* a :class:`~wactorz.agents.DynamicAgent` — the same class
main runs, compiling the same generated code against the same ``agent`` API. A
node used to carry its own lightweight retelling of that contract, and the two
drifted: a program that worked on main would reach for ``agent.run_in_background``
or ``agent.send_to_many`` on a node and find nothing there.

What this subclass adds is what is true only on a node:

- **Where its messages go.** There is no ``MQTTPublisher`` here, so the actor
  publishes through the runner's bounded queue.
- **Where its memory lives.** JSON under the node's state directory, so it
  survives a reboot and can be shipped to another machine by a migration.
- **What answers its LLM calls.** Main does, over the broker — see :mod:`.llm`.
- **Where its history comes from.** Main's database, read over the broker a page
  at a time — see :mod:`.history`.
- **How it is asked to do something.** A task arrives on an MQTT topic rather
  than in a mailbox, and the reply goes back the same way.

All but the LLM calls and the history hold for a library agent a node builds too, and live in
`NodeHosted` (see :mod:`.hosted`), which both put in front of their own class.
"""

import asyncio
import logging
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agents.dynamic.agent import DynamicAgent, _ProgramHalted
from ..core.topic_bus import TopicContract
from ..monitoring.agent_metrics import RecentDurations
from .history import read_history, read_stats
from .hosted import NodeHosted
from .llm import BridgeProvider, request_over_mqtt

if TYPE_CHECKING:
    from .runner import NodeRunner

logger = logging.getLogger(__name__)


class NodeAgent(NodeHosted, DynamicAgent):
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
            task_timeout_s=config.get("task_timeout_s"),
            artifacts=runner.readable_artifacts(config),
            # Passed in rather than assigned after, because `AgentAPI` is built
            # inside this call and decides there whether `agent.llm` exists. It
            # only holds the reference, so handing it a half-built agent is safe.
            llm_provider=BridgeProvider(self),
            name=str(name),
            persistence_dir=str(directory),
        )
        #: What the spawn said this agent does, so the planner sees it before
        #: the agent's own code has declared or published anything.
        self.capabilities = list(config.get("capabilities") or [])
        self._spawn_contract = TopicContract.from_spawn_config({**config, "node": runner.node_name})
        #: What this agent's model requests took, there through main and back,
        #: and how many main never answered in time. Reported in the metrics
        #: frame, since a node serves no `/metrics`.
        self._llm_round_trips = RecentDurations()
        self._llm_timeouts = 0
        self._llm_unsent = 0
        self.host_on(runner, config, directory)

    def llm_round_trip(self, seconds: float, *, answered: bool) -> None:
        """Record one model request sent to main: what it took, and whether it was answered."""
        self._llm_round_trips.add(seconds)
        if not answered:
            self._llm_timeouts += 1

    def llm_unsent(self) -> None:
        """Record one model request that could not be sent to main at all."""
        self._llm_unsent += 1

    def _build_metrics(self) -> dict:
        """The generated agent's frame, plus its model requests' round trips through main.

        The model call itself is timed on main, where it is made; this is the
        whole wait this agent sees, the broker both ways included, which is
        what a slow answer on a node is made of.
        """
        metrics = super()._build_metrics()
        summary = self._llm_round_trips.summary("llm_round_trip")
        if summary or self._llm_unsent:
            metrics.update(summary)
            metrics["llm_timeouts"] = self._llm_timeouts
            metrics["llm_unsent"] = self._llm_unsent
        return metrics

    # ── Talking to main ───────────────────────────────────────────────────────

    def sign_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """``request`` signed with this node's key, so main can tell who is asking."""
        return self._runner.sign_request(request)

    async def ask_main(self, topic: str, payload: dict[str, Any], timeout: float) -> Any:
        """Publish a request to main and wait for the reply it sends back."""
        return await request_over_mqtt(self, topic, payload, timeout)

    #: Main keeps the history; see `DynamicAgent.reads_history_from_main`.
    reads_history_from_main = True

    async def read_history(
        self, query: str, filters: dict[str, Any], *, hours: float, limit: int
    ) -> list[dict[str, Any]] | None:
        """Rows of main's history, asked of main a page at a time."""
        return await read_history(self, query, filters, hours=hours, limit=limit)

    async def read_history_stats(self) -> dict[str, Any] | None:
        """Row counts of main's history tables, asked of main."""
        return await read_stats(self)

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
