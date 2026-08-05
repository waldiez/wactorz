"""A protected actor must not be replaced or deleted out from under the system.

`actor_id` is a uuid5 of the name and `ActorRegistry.register` replaces whatever
holds the same id, so an agent named after a system actor does not collide with
it — it lands on the same id and the original is stopped. The orchestrator goes
down while the spawn reports success. Spawn configs are frequently LLM-authored,
so "name it main" needs no attacker to happen.

The delete half is the same invariant on the other side: REST refused a
protected actor with 403, the WebSocket path did not, and the two reached the
same code.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.core.actor import Actor, Message
from wactorz.core.registry import ActorRegistry, ProtectedActorCollision
from wactorz.web import lifecycle, runtime


class _Agent(Actor):
    """A minimal concrete actor — `Actor` is abstract."""

    async def handle_message(self, message: Message) -> None: ...


@pytest.fixture(name="registry")
def registry_fixture() -> ActorRegistry:
    return ActorRegistry()


def _agent(name: str, tmp_path: Path, *, protected: bool = False) -> _Agent:
    actor = _Agent(name=name, persistence_dir=str(tmp_path))
    actor.protected = protected
    return actor


class TestRegistryRefusesToEvictProtected:
    async def test_same_name_lands_on_the_same_id(self, tmp_path: Path) -> None:
        # The premise of the whole guard: this is why a spawn replaces rather
        # than coexists. If it ever stops being true, the guard needs rethinking.
        assert _agent("main", tmp_path).actor_id == _agent("main", tmp_path / "b").actor_id

    async def test_registering_over_a_protected_actor_raises(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        await registry.register(_agent("main", tmp_path, protected=True))

        with pytest.raises(ProtectedActorCollision):
            await registry.register(_agent("main", tmp_path / "impostor"))

    async def test_the_original_is_still_registered_afterwards(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        original = _agent("main", tmp_path, protected=True)
        await registry.register(original)

        with pytest.raises(ProtectedActorCollision):
            await registry.register(_agent("main", tmp_path / "impostor"))

        # The failure mode being prevented: the impostor takes the slot and the
        # real one is stopped, so this must still be the object it was.
        assert registry.find_by_name("main") is original

    async def test_an_unprotected_actor_is_still_replaced(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        await registry.register(_agent("worker", tmp_path))
        replacement = _agent("worker", tmp_path / "b")

        await registry.register(replacement)  # no raise

        # Replacement is the documented behaviour for ordinary actors — a
        # restart re-registers under the same deterministic id.
        assert registry.find_by_name("worker") is replacement

    async def test_re_registering_the_same_instance_is_not_a_collision(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        actor = _agent("main", tmp_path, protected=True)
        await registry.register(actor)

        await registry.register(actor)  # idempotent, not an eviction

        assert registry.find_by_name("main") is actor


class TestDeleteRefusesProtected:
    """`delete_agent` is reached by both REST and the WebSocket command path."""

    @pytest.fixture(autouse=True)
    def _restore_runtime(self) -> Any:
        registry, agents = runtime.registry, dict(runtime.state["agents"])
        yield
        runtime.registry, runtime.state["agents"] = registry, agents

    async def test_a_protected_actor_is_refused(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        actor = _agent("main", tmp_path, protected=True)
        await registry.register(actor)
        runtime.registry = registry
        runtime.state["agents"] = {actor.actor_id: {"name": "main"}}

        assert await lifecycle.delete_agent(actor.actor_id) == "refused-protected"

    async def test_the_refused_actor_is_not_marked_deleted(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        actor = _agent("main", tmp_path, protected=True)
        await registry.register(actor)
        runtime.registry = registry
        runtime.state["agents"] = {actor.actor_id: {"name": "main"}}

        await lifecycle.delete_agent(actor.actor_id)

        # A tombstone would suppress the agent's own heartbeats, so the
        # dashboard would lose it even though the delete was refused.
        assert actor.actor_id not in [aid for aid, _ in runtime.deleted_agent_ids]
        assert actor.actor_id in runtime.state["agents"]

    async def test_the_dashboard_record_alone_cannot_protect_an_agent(
        self, registry: ActorRegistry, tmp_path: Path
    ) -> None:
        # The record is built from heartbeats, so a remote agent can claim
        # `protected` for itself. The live actor is the authority.
        actor = _agent("weather", tmp_path)
        await registry.register(actor)
        runtime.registry = registry
        runtime.state["agents"] = {actor.actor_id: {"name": "weather", "protected": True}}

        assert await lifecycle.delete_agent(actor.actor_id) != "refused-protected"
