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

A refusal, or a question nobody answered, is remembered for a while
(:mod:`wactorz.core.refusals`), so a model trying the same spawn again is
refused at once rather than asking again.

Only a ``<spawn>`` the model wrote is placed here. Catalogue and native agents,
library agents and agents restored from the spawn registry keep the placement
they have, and so does a move the operator asked for with ``/migrate``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from ...config import CONFIG, MODEL_CODE_NODE, MODEL_CODE_ON_MAIN
from ...core.outbound import Answer
from ...core.packages import APPROVAL_TIMEOUT_S
from ...core.refusals import REFUSED, UNANSWERED, RecentRefusals
from ..mixins import KeptHere, NotSpawned, PlacedOnNode

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = ["KeptHere", "NotSpawned", "PlacedOnNode", "Placement", "runs_model_code"]

#: What a spawn names to run in main's own process.
MAIN = "main"

#: The operator's choices, as the dashboard shows them.
RUN_ON_MAIN = "Run on main"
REFUSE = "Refuse"
#: How a question nobody answered in time is closed on every dashboard.
NO_ANSWER = "No answer in time"


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

    async def notify_user(self, text: str, *, options: Sequence[str] = ...) -> str: ...

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None: ...


class Placement:
    """Decides where each model-written spawn runs, asking the operator when it must."""

    def __init__(self, host: PlacementHost) -> None:
        self.host = host
        #: Questions put to the operator and not yet answered, by message id.
        self._asking: dict[str, asyncio.Future[bool]] = {}
        self._refusals = RecentRefusals()

    async def place(self, config: dict[str, Any]) -> dict[str, Any] | NotSpawned:
        """``config`` as it should be spawned, naming the node it goes to; or why not at all."""
        if not MODEL_CODE_NODE or not runs_model_code(config):
            return config
        name = str(config.get("name") or "")
        named = str(config.get("node") or "").strip()
        if named == MAIN:
            return await self._on_main(config, name)
        if named:
            return config
        known = {target.name for target in CONFIG.deploy_targets} | set(self.host._known_nodes)
        if MODEL_CODE_NODE not in known:
            return NotSpawned(
                name,
                f"code the model writes runs on node '{MODEL_CODE_NODE}' "
                "(WACTORZ_MODEL_CODE_NODE), which is neither a deploy target here nor a node "
                "main has heard from. Set it up with `sudo wactorz-node setup-local`, then "
                f"`/deploy {MODEL_CODE_NODE}`; in the compose stack, start it with "
                "`--profile sandbox`",
            )
        return {**config, "node": MODEL_CODE_NODE}

    async def _on_main(self, config: dict[str, Any], name: str) -> dict[str, Any] | NotSpawned:
        """``config`` placed on main, if the setting or the operator allows it."""
        on_main = {key: value for key, value in config.items() if key != "node"}
        if MODEL_CODE_ON_MAIN == "allow":
            return on_main
        if MODEL_CODE_ON_MAIN == "never":
            return NotSpawned(
                name,
                "code the model writes does not run on main (WACTORZ_MODEL_CODE_ON_MAIN=never); "
                f"it runs on node '{MODEL_CODE_NODE}' when the spawn names no node",
            )
        standing = self._refusals.standing(name)
        if standing is not None:
            return NotSpawned(name, f"running it on main was {standing.said()}")
        if await self._operator_allows(name):
            return on_main
        return NotSpawned(name, "the operator did not let it run on main")

    async def _operator_allows(self, name: str) -> bool:
        """Ask on the dashboard, and wait for the press; no answer in time is a no."""
        question_id = await self.host.notify_user(
            f"The model wrote an agent, '{name}', and asks to run it on main, where it would "
            "hold every credential this server has. Run it on main, or refuse? Refused, it "
            f"can run on node '{MODEL_CODE_NODE}' instead: ask for it without naming main.",
            options=(RUN_ON_MAIN, REFUSE),
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
                await self._close(question_id, NO_ANSWER)
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
        logger.warning(
            "[%s] The operator answered %r about running model-written code on main",
            self.host.name,
            answer.option,
        )

    async def _close(self, question_id: str, option: str) -> None:
        """Mark ``question_id`` answered with ``option`` on every dashboard."""
        closing = Answer(
            id=question_id,
            agent_id=self.host.actor_id,
            option=option,
            index=-1,
            channel=self.host.name,
            timestamp=time.time(),
        )
        await self.host._mqtt_publish(closing.topic, closing.to_payload(), qos=1)
