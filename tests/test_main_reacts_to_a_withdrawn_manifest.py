"""A withdrawn manifest removes the agent from main's spawn registry.

The withdrawal is the one removal signal every ending publishes — an agent that
ends itself, a delete, a prune, a node going quiet. Main already listened for it
to keep its capability tables current; now it finishes the job, so the same
mechanism serves an agent running here and one running on a node, where an
in-process call to main could never reach.

Without it, an agent that ended itself stayed in the spawn registry and came
back on the next restart, and a node kept the instruction to start it in its
retained desired state.
"""

import uuid
from typing import Any

import pytest

from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.core.actor import derive_actor_id


class Host:
    """The parts of main a removal touches, and nothing else."""

    def __init__(self, registry: dict[str, dict[str, Any]] | None = None) -> None:
        self.name = "main"
        self._registry = None
        self._spawn_registry = registry if registry is not None else {}
        self._conversation_history: list[dict[str, Any]] = []
        self.desired_state_calls: list[tuple[str, str | None]] = []
        self.persisted: list[tuple[str, Any]] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self._spawn_registry

    def _remove_from_spawn_registry(self, name: str) -> None:
        self._spawn_registry.pop(name, None)

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        self.desired_state_calls.append((node, remove_name))

    def persist(self, key: str, value: Any) -> None:
        self.persisted.append((key, value))


def service(registry: dict[str, dict[str, Any]] | None = None) -> tuple[LifecycleService, Host]:
    host = Host(registry)
    return LifecycleService(host), host  # pyright: ignore[reportArgumentType]


def deletion_notes(host: Host) -> list[str]:
    return [
        entry["content"]
        for entry in host._conversation_history
        if isinstance(entry.get("content"), str) and entry["content"].startswith("[SYSTEM] Agent")
    ]


class TestALocalAgent:
    async def test_it_leaves_the_spawn_registry(self) -> None:
        svc, host = service({"worker": {"code": "..."}})

        await svc.agent_withdrew(derive_actor_id("worker"), "worker")

        assert "worker" not in host._spawn_registry

    async def test_no_node_is_rewritten_for_it(self) -> None:
        """A local agent has no desired state to correct."""
        svc, host = service({"worker": {"code": "..."}})

        await svc.agent_withdrew(derive_actor_id("worker"), "worker")

        assert not host.desired_state_calls

    async def test_the_deletion_is_recorded(self) -> None:
        svc, host = service({"worker": {"code": "..."}})

        await svc.agent_withdrew(derive_actor_id("worker"), "worker")

        assert len(deletion_notes(host)) == 1


class TestAnAgentOnANode:
    async def test_the_nodes_desired_state_loses_it(self) -> None:
        """Otherwise the node starts it again on its next reconcile, into a main
        that has just forgotten it."""
        svc, host = service({"collector": {"code": "...", "node": "rpi"}})

        await svc.agent_withdrew(derive_actor_id("collector"), "collector")

        assert host.desired_state_calls == [("rpi", "collector")]
        assert "collector" not in host._spawn_registry


class TestTheNameIsRecoverable:
    async def test_it_is_derived_when_no_manifest_was_cached(self) -> None:
        """A withdrawal can arrive before this process ever saw the manifest it
        withdraws — after a restart the retained message is gone, so the
        tombstone is all there is and the id has to be enough."""
        svc, host = service({"worker": {"code": "..."}})

        await svc.agent_withdrew(derive_actor_id("worker"))  # no name given

        assert "worker" not in host._spawn_registry

    def test_the_formula_itself_is_pinned(self) -> None:
        """The lookup matches a published id against names, so it holds only
        while every copy of this derivation agrees — and `remote_runner` keeps
        its own, deliberately. Asserting the value rather than the function's
        reflexivity is what makes both copies drifting the same way impossible.
        """
        assert derive_actor_id("worker") == str(
            uuid.uuid5(uuid.NAMESPACE_DNS, "wactorz.actor.worker")
        )


class TestAWithdrawalThatMeansNothing:
    async def test_an_unknown_agent_is_a_silent_no_op(self) -> None:
        svc, host = service({"worker": {"code": "..."}})

        await svc.agent_withdrew(derive_actor_id("someone-else"))

        assert host._spawn_registry == {"worker": {"code": "..."}}
        assert not deletion_notes(host)

    async def test_an_agent_already_removed_is_a_silent_no_op(self) -> None:
        """The delete path publishes a withdrawal of its own, so this runs
        after it and must settle on the same result rather than a second note."""
        svc, host = service({"worker": {"code": "..."}})
        await svc.agent_withdrew(derive_actor_id("worker"), "worker")

        await svc.agent_withdrew(derive_actor_id("worker"), "worker")

        assert len(deletion_notes(host)) == 1


class TestAResetIsNotAMassSelfDeletion:
    async def test_withdrawals_are_ignored_inside_the_window(self) -> None:
        """A reset withdraws the manifest of every agent it wipes."""
        svc, host = service({"a": {"code": "..."}, "b": {"code": "..."}})

        with svc.withdrawals_suppressed():
            await svc.agent_withdrew(derive_actor_id("a"), "a")
            await svc.agent_withdrew(derive_actor_id("b"), "b")

        assert set(host._spawn_registry) == {"a", "b"}
        assert not deletion_notes(host)

    async def test_the_window_closes(self) -> None:
        svc, host = service({"a": {"code": "..."}})
        with svc.withdrawals_suppressed():
            pass

        await svc.agent_withdrew(derive_actor_id("a"), "a")

        assert "a" not in host._spawn_registry

    async def test_it_closes_even_if_the_body_raises(self) -> None:
        svc, _host = service({"a": {"code": "..."}})

        with pytest.raises(RuntimeError), svc.withdrawals_suppressed():
            raise RuntimeError("reset failed")

        assert not svc._withdrawals_suppressed

    async def test_a_tombstone_arriving_after_the_reset_finds_nothing(self) -> None:
        """The window is not the only guard: once the reset has emptied the
        registry, a late withdrawal has nothing left to remove."""
        svc, host = service({})

        await svc.agent_withdrew(derive_actor_id("a"), "a")

        assert not deletion_notes(host)
