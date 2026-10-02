"""A real main and a real node, in this process, joined only by a real broker.

Every other test refuses broker connections, so the contract between main and a
node is checked piece by piece against fakes. Here both sides run as they do in
production and talk through mosquitto, which is the one thing that proves the
pieces agree: topics, signatures, retained messages and reply channels included.

Skipped unless ``WACTORZ_TEST_BROKER=host:port`` names a broker that takes
anonymous clients. ``make test-broker`` starts one and runs these.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from wactorz.agents.lookup import find_main_actor
from wactorz.agents.main.actor import MainActor
from wactorz.core import node_signing, persistence
from wactorz.core.cancellation import cancel_until_done
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.registry import ActorSystem
from wactorz.node.runner import NodeRunner

BROKER_ENV = "WACTORZ_TEST_BROKER"

#: Generous: these wait on real connections, and on a node's next heartbeat.
WAIT_S = 30.0


class FastNode(NodeRunner):
    """A node that reports several times a second, so a test is not paced by its heartbeat."""

    async def _node_heartbeat_loop(self, interval: float = 0.3) -> None:
        await super()._node_heartbeat_loop(interval)


async def until(condition: Callable[[], object], what: str, timeout: float = WAIT_S) -> None:
    """Wait for ``condition`` to hold, and say what never happened if it does not."""

    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(poll(), timeout)
    except asyncio.TimeoutError:
        pytest.fail(f"within {timeout:g}s, never: {what}")


@pytest.fixture(name="broker")
def broker_fixture() -> tuple[str, int]:
    address = os.environ.get(BROKER_ENV, "").strip()
    if not address:
        pytest.skip(f"set {BROKER_ENV}=host:port, or run `make test-broker`")
    host, _, port = address.rpartition(":")
    return host, int(port)


@pytest.fixture(name="main")
async def main_fixture(
    broker: tuple[str, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MainActor]:
    """A started main, on its own state directory, publishing through the real publisher.

    With the database and the store the server gives it, which the agents it
    spawns inherit: what an agent keeps is read from there when it is moved.
    """
    host, port = broker
    state = tmp_path / "main"
    state.mkdir()
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
    system = ActorSystem(mqtt_broker=host, mqtt_port=port, state_dir=str(state))
    system._mqtt_client = await MQTTPublisher.create(host, port, db_path=state / "mqtt_outbox.db")
    db, pickles = persistence.init_persistence(
        db_path=state / "wactorz.db", state_dir=str(state), run_migration=False
    )

    def _main() -> MainActor:
        actor = MainActor(llm_provider=None, name="main", persistence_dir=str(state))
        actor._persistence_api = persistence.PersistenceAPI(db, pickles, actor.name)
        return actor

    system.supervisor.supervise("main", _main)
    await system.supervisor.start()
    main = find_main_actor(system.registry)
    assert main is not None
    try:
        yield main
    finally:
        await system.stop_all()
        persistence.close_persistence()


@pytest.fixture(name="node")
async def node_fixture(
    broker: tuple[str, int], main: MainActor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastNode]:
    """A running node that main knows about, holding the key main signs its commands with.

    A name of its own per test: the broker outlives the test, and so would anything
    retained on a name used before.
    """
    host, port = broker
    name = f"it-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("WACTORZ_NODE_KEY", node_signing.node_key(name))
    monkeypatch.setenv("WACTORZ_NODE_SIGNING", "enforce")
    runner = FastNode(host, port, name, state_dir=str(tmp_path / "node"))
    running = asyncio.create_task(runner.run())
    try:
        await until(lambda: name in main._known_nodes, f"main hearing from node {name}")
        yield runner
    finally:
        await runner.shutdown()
        await cancel_until_done(running, timeout=WAIT_S)
