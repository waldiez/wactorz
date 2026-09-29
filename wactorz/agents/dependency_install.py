"""Installing an agent's packages before it spawns, and saying what came of it.

The catalog and main both spawn agents that declare packages. An agent started
before its install has finished crashes on import, and one started after pip
replaced a package this process had already loaded cannot work until a restart.
So a spawn waits for the installer's own answer, for as long as the installer
may take, and only starts the agent when that answer says it can run. The
waiting and the reading of the answer live here so both spawn paths agree.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.actor import Message, MessageType
from ..core.pip import install_wait_s

logger = logging.getLogger(__name__)


@dataclass
class InstallOutcome:
    """What the installer reported for one agent's packages."""

    failed: dict[str, str] = field(default_factory=dict)
    """Package → the tail of pip's error, for each package that did not install."""
    restart_required: list[str] = field(default_factory=list)
    """``name old -> new`` for each already-imported package pip replaced."""
    timed_out: bool = False
    unavailable: bool = False
    """No installer to ask: the packages were not installed."""

    @property
    def ok(self) -> bool:
        """Whether the agent can be spawned in this process now."""
        return not (self.failed or self.restart_required or self.timed_out or self.unavailable)

    def problem(self, agent_name: str) -> str:
        """The user-facing reason `agent_name` was not started, or ``""``."""
        if self.unavailable:
            return (
                f"Could not install the packages {agent_name} needs: the installer "
                "agent is not running."
            )
        if self.timed_out:
            return (
                f"Installing the packages for {agent_name} did not finish in time. "
                f"Try spawning it again; anything already installed is kept."
            )
        if self.failed:
            errors = {error.strip()[-300:] or "pip failed" for error in self.failed.values()}
            if len(errors) == 1 and len(self.failed) > 1:
                # One cause for every package, so it is said once.
                return (
                    f"{agent_name} was not started because its packages could not be "
                    f"installed: {errors.pop()}"
                )
            lines = "\n".join(
                f"- {pkg}: {error.strip()[-300:] or 'pip failed'}"
                for pkg, error in self.failed.items()
            )
            return (
                f"{agent_name} was not started because some of its packages failed "
                f"to install:\n{lines}"
            )
        if self.restart_required:
            replaced = ", ".join(self.restart_required)
            return (
                f"{agent_name}'s packages are installed, but installing them replaced "
                f"packages Wactorz had already loaded ({replaced}). Python cannot "
                f"swap those in while it runs, so restart Wactorz once to finish; "
                f"{agent_name} will start by itself after the restart."
            )
        return ""


async def install_for_agent(
    requester: Any, main: Any, packages: list[str], agent_name: str, *, notify: bool = True
) -> InstallOutcome:
    """Ask the installer for `packages` on behalf of `agent_name` and wait for it.

    `requester` is the actor asking. The installer's reply is routed through
    `main`'s result futures, where main resolves every RESULT addressed to it.
    With `notify`, the installer announces each package in chat as it starts.
    """
    registry = getattr(requester, "_registry", None)
    installer = registry.find_by_name("installer") if registry else None
    if installer is None or main is None:
        logger.warning(
            "[%s] installer unavailable — cannot install %s for '%s'",
            getattr(requester, "name", "?"),
            packages,
            agent_name,
        )
        return InstallOutcome(unavailable=True)

    task_id = f"install_{uuid.uuid4().hex[:8]}"
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    main._result_futures[task_id] = future
    try:
        await installer.receive(
            Message(
                type=MessageType.TASK,
                sender_id=requester.actor_id,
                reply_to=main.actor_id,
                payload={
                    "action": "install",
                    "packages": list(packages),
                    "for_agent": agent_name,
                    "notify": notify,
                    "task": task_id,
                    "_task_id": task_id,
                },
            )
        )
        try:
            result = await asyncio.wait_for(future, timeout=install_wait_s(len(packages)))
        except asyncio.TimeoutError:
            logger.warning("Install for '%s' timed out: %s", agent_name, packages)
            return InstallOutcome(timed_out=True)
    finally:
        main._result_futures.pop(task_id, None)

    outcome = outcome_from_result(result)
    logger.info(
        "Install for '%s': %s%s",
        agent_name,
        result.get("message", result) if isinstance(result, dict) else result,
        f" — {outcome.problem(agent_name)}" if not outcome.ok else "",
    )
    return outcome


def outcome_from_result(result: Any) -> InstallOutcome:
    """Read the installer's RESULT payload into an `InstallOutcome`."""
    result = result if isinstance(result, dict) else {}
    results = result.get("results") or {}
    failed = {
        pkg: str(results.get(pkg, "")).removeprefix("failed: ")
        for pkg in result.get("failed") or []
    }
    return InstallOutcome(
        failed=failed, restart_required=list(result.get("restart_required") or [])
    )
