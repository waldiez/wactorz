"""Where an agent the model wrote runs: on main, or on a node made for it.

Main holds every credential the deployment has, and the model writes code from
whatever it read: a message, a web page, a device's name. With
``WACTORZ_MODEL_CODE_NODE`` set -- usually to ``sandbox``, the node
``wactorz-node setup-local`` makes -- a ``<spawn>`` whose code the model wrote
goes there when it names no node, rather than into main's process.

A spawn that names main itself is the model asking for those credentials, and
the model's asking is not enough: ``WACTORZ_MODEL_CODE_ON_MAIN`` says whether to
ask the operator (the default), allow it, or refuse it. The operator's answer is
taken only from the dashboard in this process, as the package question's is,
never from the broker, where any agent could publish a yes.

Unset, the setting means ``sandbox`` whenever a node by that name exists --
a deploy target, or a node main has heard from -- and main otherwise, so an
install gains the sandbox by setting one up. ``main`` keeps the code on main.

A refusal, or a question nobody answered, is remembered for a while
(:mod:`wactorz.core.refusals`), so a model trying the same spawn again is
refused at once rather than asking again.

Agents the model wrote before there was a node for them are moved there the
first time it reports after a start (:meth:`Placement.rehome`). One that reads
the history without ``await`` stays, since only main answers that call that way.

Only a ``<spawn>`` the model wrote is placed here. Catalogue and native agents,
library agents and agents restored from the spawn registry keep the placement
they have, and so does a move the operator asked for with ``/migrate``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from ...config import CONFIG, MODEL_CODE_NODE, MODEL_CODE_ON_MAIN
from ...core.outbound import Answer
from ...core.packages import APPROVAL_TIMEOUT_S
from ...core.refusals import REFUSED, UNANSWERED, RecentRefusals
from ..dynamic.safety import parse_program
from ..mixins import KeptHere, NotSpawned, PlacedOnNode
from .spawn_registry import address_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .migration import Migration
    from .spawn_registry import SpawnRegistry

logger = logging.getLogger(__name__)

__all__ = ["KeptHere", "NotSpawned", "PlacedOnNode", "Placement", "runs_model_code"]

#: What a spawn names to run in main's own process.
MAIN = "main"

#: The node model-written code goes to when ``WACTORZ_MODEL_CODE_NODE`` is
#: unset, if a node by this name exists.
DEFAULT_NODE = "sandbox"

#: The history reads that on a node must be awaited (see :mod:`wactorz.node.history`).
HISTORY_CALLS = frozenset({"query_ts", "query_detections", "query_ha_states", "ts_stats"})

#: The operator's choices, as the dashboard shows them.
RUN_ON_MAIN = "Run on main"
REFUSE = "Refuse"
#: How a question nobody answered in time is closed on every dashboard.
NO_ANSWER = "No answer in time"


def stays_on_main(code: str) -> str | None:
    """Why the program ``code`` must stay on main, or None if it can run on a node.

    A history read used without ``await`` answers only on main; on a node it
    fails the moment its result is used. A program that cannot be parsed
    cannot be checked, and stays where it runs.
    """
    try:
        tree = parse_program(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return "its code could not be read to check it"
    awaited = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Await)}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in HISTORY_CALLS
            and id(node) not in awaited
        ):
            return f"it calls {node.func.attr}() without await, which only main answers"
    return None


def runs_model_code(config: dict[str, Any]) -> bool:
    """Whether ``config`` is an agent whose program is code the spawn carries.

    A dynamic agent with code. An LLM agent runs main's own class around a
    prompt; rule, scheduled and actuator agents carry settings, not a program;
    catalogue, native and library agents run code this install shipped.
    """
    agent_type = str(config.get("type") or "dynamic").strip().lower()
    return agent_type == "dynamic" and bool(str(config.get("code") or "").strip())


class PlacementHost(Protocol):
    """What placement needs from main: to ask, and to close the question."""

    name: str
    actor_id: str

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        """The nodes main has heard from: a node with no deploy target, in a container, is one."""
        ...

    async def ask_permission(self, text: str, options: Sequence[str]) -> str: ...

    async def permission_answered(self, answer: Answer) -> None: ...

    async def close_permission(self, question_id: str, option: str) -> None: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...

    def _get_spawn_registry(self) -> SpawnRegistry: ...

    def _node_refuses_tasks(self, node_name: str) -> str | None: ...

    def _queue_notification(self, notice: dict[str, Any]) -> None: ...

    @property
    def _registry(self) -> Any:
        """The actors in main's process: a model-written agent is moved only if it runs here."""
        ...

    @property
    def migration(self) -> Migration:
        """What moves an agent, as `/migrate` does."""
        ...


class Placement:
    """Decides where each model-written spawn runs, asking the operator when it must."""

    def __init__(self, host: PlacementHost) -> None:
        self.host = host
        #: Questions put to the operator and not yet answered, by message id.
        self._asking: dict[str, asyncio.Future[bool]] = {}
        self._refusals = RecentRefusals()
        #: The nodes model-written agents have been moved to since this start.
        self._rehomed: set[str] = set()

    def model_code_node(self) -> str:
        """The node model-written code goes to now, or "" for main."""
        if MODEL_CODE_NODE == MAIN:
            return ""
        if MODEL_CODE_NODE:
            return MODEL_CODE_NODE
        return DEFAULT_NODE if DEFAULT_NODE in self._nodes_here() else ""

    def _nodes_here(self) -> set[str]:
        """The nodes this install has: its deploy targets, and those main has heard from."""
        return {target.name for target in CONFIG.deploy_targets} | set(self.host._known_nodes)

    async def place(self, config: dict[str, Any]) -> dict[str, Any] | NotSpawned:
        """``config`` as it should be spawned, naming the node it goes to; or why not at all.

        A node main would not hear from is refused here, whoever chose it, so
        the caller can say why at once rather than wait for an agent that never
        counts as started.
        """
        placed = await self._placed(config)
        if isinstance(placed, NotSpawned):
            return placed
        node = str(placed.get("node") or "").strip()
        unheard = self.host._node_refuses_tasks(node) if node else None
        if unheard:
            return NotSpawned(str(config.get("name") or ""), unheard)
        return placed

    async def _placed(self, config: dict[str, Any]) -> dict[str, Any] | NotSpawned:
        """Where ``config`` goes by the rules for code the model writes, or why not at all."""
        node = self.model_code_node()
        if not node or not runs_model_code(config):
            return config
        name = str(config.get("name") or "")
        named = str(config.get("node") or "").strip()
        if named == MAIN:
            return await self._on_main(config, name, node)
        if named:
            return config
        if node not in self._nodes_here():
            return NotSpawned(
                name,
                f"code the model writes runs on node '{node}' "
                "(WACTORZ_MODEL_CODE_NODE), which is neither a deploy target here nor a node "
                "main has heard from. Set it up with `sudo wactorz-node setup-local`, then "
                f"`/deploy {node}`; in the compose stack, start it with `--profile sandbox`",
            )
        return {**config, "node": node}

    def claim_rehoming(self, node: str) -> bool:
        """Whether a heartbeat from ``node`` should move the model's agents there now.

        True once per start, for the node model-written code goes to, and
        claimed in the asking, so two heartbeats cannot start two moves.
        """
        if node in self._rehomed or node != self.model_code_node():
            return False
        self._rehomed.add(node)
        return True

    async def rehome(self, node: str) -> None:
        """Move the model-written agents still running on main to ``node``, and say so.

        As ``/migrate`` moves them, state and all. One that cannot run there,
        or whose move is refused, stays, and the chat says why.
        """
        moved: list[str] = []
        kept: list[tuple[str, str]] = []
        registry = self.host._registry
        here = self.host._get_spawn_registry().on_node("")
        for config in sorted(here, key=lambda entry: entry["name"]):
            name = config["name"]
            if (
                not runs_model_code(config)
                or config.get("trusted")
                or registry is None
                or registry.find_by_name(name) is None
            ):
                continue
            why = stays_on_main(str(config.get("code") or ""))
            if why is None:
                result = await self.host.migration.migrate_agent(str(address_of(config)), node)
                if result.get("success"):
                    moved.append(name)
                    continue
                why = str(result.get("message") or "the move was refused")
            kept.append((name, why))
        if moved or kept:
            self.host._queue_notification(
                {
                    "severity": "info" if not kept else "warning",
                    "message": _rehomed(node, moved, kept),
                }
            )

    async def _on_main(
        self, config: dict[str, Any], name: str, node: str
    ) -> dict[str, Any] | NotSpawned:
        """``config`` placed on main, if the setting or the operator allows it."""
        on_main = {key: value for key, value in config.items() if key != "node"}
        if MODEL_CODE_ON_MAIN == "allow":
            return on_main
        if MODEL_CODE_ON_MAIN == "never":
            return NotSpawned(
                name,
                "code the model writes does not run on main (WACTORZ_MODEL_CODE_ON_MAIN=never); "
                f"it runs on node '{node}' when the spawn names no node",
            )
        standing = self._refusals.standing(name)
        if standing is not None:
            return NotSpawned(name, f"running it on main was {standing.said()}")
        if await self._operator_allows(name, node):
            return on_main
        return NotSpawned(name, "the operator did not let it run on main")

    async def _operator_allows(self, name: str, node: str) -> bool:
        """Ask on the dashboard, and wait for the press; no answer in time is a no."""
        question_id = await self.host.ask_permission(
            f"The model wrote an agent, '{name}', and asks to run it on main, where it would "
            "hold every credential this server has. Run it on main, or refuse? Refused, it "
            f"can run on node '{node}' instead: ask for it without naming main.",
            (RUN_ON_MAIN, REFUSE),
        )
        answered: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._asking[question_id] = answered
        try:
            allowed = await asyncio.wait_for(asyncio.shield(answered), timeout=APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] No answer within %gs about running %r on main",
                self.host.name,
                APPROVAL_TIMEOUT_S,
                name,
            )
            # Closed, so a press that comes later cannot let a spawn through
            # that has already been refused.
            if self._asking.pop(question_id, None) is not None:
                await self.host.close_permission(question_id, NO_ANSWER)
            self._refusals.remember(name, UNANSWERED)
            return False
        if allowed:
            self._refusals.forget(name)
        else:
            self._refusals.remember(name, REFUSED)
        return allowed

    async def dashboard_answer(self, answer: Answer) -> None:
        """The operator's press on the dashboard, handed over by the web server in this process."""
        answered = self._asking.pop(answer.id, None)
        if answered is None or answered.done():
            return
        answered.set_result(answer.option == RUN_ON_MAIN)
        await self.host.permission_answered(answer)
        logger.warning(
            "[%s] The operator answered %r about running model-written code on main",
            self.host.name,
            answer.option,
        )


def _rehomed(node: str, moved: list[str], kept: list[tuple[str, str]]) -> str:
    """What the chat is told after the model's agents were moved to ``node``."""
    lines = []
    if moved:
        named = ", ".join(f"'{name}'" for name in moved)
        lines.append(f"Moved to node '{node}', where code the model writes runs: {named}.")
    for name, why in kept:
        lines.append(f"Kept '{name}' on main: {why}.")
    return "\n".join(lines)
