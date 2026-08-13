"""Re-registering a name must actually stop the instance it replaces.

⚠ The old instance's message loop, heartbeat and any MQTT subscriptions are
still running when its id is re-registered. If it is not stopped, every
published event is delivered twice — to the new listener and to the old one
nothing points at any more.

The stop used to be `asyncio.create_task(existing.stop())`: nothing held the
task, so it could be collected mid-stop, and an exception inside it reached
nobody — failing at exactly the job it was there to do, silently.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.core.actor import Actor, Message
from wactorz.core.registry import ActorRegistry, ProtectedActorCollision


class _Recording(Actor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        await super().stop()

    async def handle_message(self, msg: Message) -> None:
        return None


class _StopExplodes(_Recording):
    async def stop(self) -> None:
        self.stopped = True
        raise RuntimeError("its listeners are wedged")


class TestSuperseding:
    async def test_the_replaced_instance_is_stopped(self, tmp_path: Path) -> None:
        registry = ActorRegistry()
        first = _Recording(name="worker", persistence_dir=str(tmp_path))
        second = _Recording(name="worker", persistence_dir=str(tmp_path))

        await registry.register(first)
        await registry.register(second)

        # Awaited, so this holds the moment register() returns — not eventually.
        assert first.stopped
        assert registry.get(second.actor_id) is second

    async def test_a_failure_to_stop_is_reported_not_swallowed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The old instance may still be subscribed, which is the duplicate
        # delivery this guards against — silence would leave nobody looking.
        registry = ActorRegistry()
        first = _StopExplodes(name="worker", persistence_dir=str(tmp_path))
        second = _Recording(name="worker", persistence_dir=str(tmp_path))
        await registry.register(first)

        with caplog.at_level("ERROR"):
            await registry.register(second)

        assert "listeners may still be live" in caplog.text
        # The new actor is registered regardless: refusing would leave the name
        # owned by an instance that is already being torn down.
        assert registry.get(second.actor_id) is second

    async def test_registering_the_same_object_twice_stops_nothing(self, tmp_path: Path) -> None:
        registry = ActorRegistry()
        actor = _Recording(name="worker", persistence_dir=str(tmp_path))

        await registry.register(actor)
        await registry.register(actor)

        assert not actor.stopped

    async def test_a_protected_actor_is_never_superseded(self, tmp_path: Path) -> None:
        registry = ActorRegistry()
        protected = _Recording(name="main", persistence_dir=str(tmp_path))
        protected.protected = True
        await registry.register(protected)

        with pytest.raises(ProtectedActorCollision):
            await registry.register(_Recording(name="main", persistence_dir=str(tmp_path)))

        assert not protected.stopped
