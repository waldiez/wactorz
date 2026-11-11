# SPDX-License-Identifier: Apache-2.0
"""Main and a node still work together with the access list in force.

`test_access_list.py` asks the broker what each rule allows. This runs the real
protocol through those rules: main connects as the server's account, and the
node is a process of its own with the node's account, key and environment, as
`/deploy` leaves it. If the list ever keeps a node from a topic the protocol
needs, this is where it shows.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from wactorz.agents.lookup import find_main_actor
from wactorz.agents.main import history as main_history
from wactorz.agents.main import node_access
from wactorz.agents.main.actor import MainActor
from wactorz.core import broker_accounts, node_signing
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.persistence import WactorzDB
from wactorz.core.registry import ActorSystem
from wactorz.core.topics import NAMESPACE

from .acl_fixture import PASSWORD, SERVER, node_topics
from .conftest import until
from .test_access_list import locked_fixture  # noqa: F401  # a fixture, used by name

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(240)]

NODE = "node-a"

#: A node reports every ten seconds, so main learns of a change within a few of those.
NODE_WAIT_S = 60.0

ECHO = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    return {"result": "echo:" + str(payload.get("text", ""))}
"""

#: One topic outside what a node may publish to, then one the test's access list adds.
OUTSIDE = """
async def setup(agent):
    await agent.publish("anomalies/imu", {"v": 1})
    await agent.publish("plant/pump", {"v": 1})
"""

READER = """
async def handle_task(agent, payload):
    rows = await agent.query_ts(hours=1, field="temp")
    return {"result": [row["value"] for row in rows]}
"""


@pytest.fixture(name="main")
async def main_fixture(
    locked: tuple[str, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MainActor]:
    """A started main, connected as the server's account."""
    host, port = locked
    for module in list(sys.modules.values()):
        ambient = getattr(module, "CONFIG", None)
        if ambient is not None and type(ambient).__name__ == "AppConfig":
            monkeypatch.setattr(
                module, "CONFIG", replace(ambient, mqtt_username=SERVER, mqtt_password=PASSWORD)
            )
    state = tmp_path / "main"
    state.mkdir()
    system = ActorSystem(mqtt_broker=host, mqtt_port=port, state_dir=str(state))
    system._mqtt_client = await MQTTPublisher.create(host, port, db_path=state / "mqtt_outbox.db")
    system.supervisor.supervise(
        "main", lambda: MainActor(llm_provider=None, name="main", persistence_dir=str(state))
    )
    await system.supervisor.start()
    main = find_main_actor(system.registry)
    assert main is not None
    try:
        yield main
    finally:
        await system.stop_all()


@pytest.fixture(name="node_process")
def node_process_fixture(
    locked: tuple[str, int], tmp_path: Path
) -> Iterator[subprocess.Popen[bytes]]:
    """The node, as its own process with its own account, key and home directory."""
    host, port = locked
    home = tmp_path / "node-home"
    home.mkdir()
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("WACTORZ_", "MQTT_"))
    }
    env.update(
        HOME=str(home),
        MQTT_USERNAME=NODE,
        MQTT_PASSWORD=broker_accounts.password(NODE),
        WACTORZ_NODE_KEY=node_signing.node_key(NODE),
        WACTORZ_NODE_SIGNING="enforce",
    )
    # As `node_env_text` tells a node main's namespace: only when one is set.
    if NAMESPACE:
        env["WACTORZ_NAMESPACE"] = NAMESPACE
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "wactorz.cli",
            "--node",
            NODE,
            "--mqtt-broker",
            host,
            "--mqtt-port",
            str(port),
        ],
        env=env,
        cwd=home,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        yield process
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)


def _agents_main_sees(main: MainActor) -> list[str]:
    return list(main._known_nodes.get(NODE, {}).get("agents", []))


async def _node_heard(main: MainActor, node_process: subprocess.Popen[bytes]) -> None:
    """Wait until main hears from the node process, failing at once if it exits."""

    def _node_is_running() -> bool:
        if node_process.poll() is not None:
            said = (
                node_process.stderr.read().decode(errors="replace") if node_process.stderr else ""
            )
            pytest.fail(f"the node process exited:\n{said[-2000:]}")
        return NODE in main._known_nodes

    await until(_node_is_running, "main hearing from the node process", NODE_WAIT_S)


async def _spawned_on_the_node(main: MainActor, name: str, code: str) -> None:
    await main._spawn_remote({"name": name, "type": "dynamic", "code": code}, NODE, save=False)
    await until(
        lambda: name in _agents_main_sees(main), f"main seeing {name!r} on the node", NODE_WAIT_S
    )


async def test_main_spawns_and_asks_an_agent_on_a_node_with_its_own_account(
    main: MainActor, node_process: subprocess.Popen[bytes]
) -> None:
    await _node_heard(main, node_process)
    await _spawned_on_the_node(main, "echo", ECHO)

    reply = await asyncio.wait_for(
        main.delegation.delegate_task("echo", "ping", timeout=30), timeout=60
    )
    assert reply is not None
    assert reply.get("result") == "echo:ping"


async def test_an_agent_on_the_node_reads_main_s_history(
    main: MainActor,
    node_process: subprocess.Popen[bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = WactorzDB(str(tmp_path / "history.db"))
    db.write_sensor(time.time(), "sensors/t", "sensor.kitchen", "temp", 21.5)
    monkeypatch.setattr(main_history, "get_db", lambda: db)
    await _node_heard(main, node_process)
    await _spawned_on_the_node(main, "reader", READER)

    reply = await asyncio.wait_for(
        main.delegation.delegate_task("reader", "how warm?", timeout=30), timeout=60
    )

    assert reply is not None
    assert reply.get("result") == [21.5]


async def test_main_says_which_topic_of_a_nodes_agent_the_broker_refuses(
    main: MainActor, node_process: subprocess.Popen[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The broker's files are the fixture's, not written from this server's settings.
    monkeypatch.setattr(node_access, "node_is_under_access_list", lambda node: node == NODE)
    monkeypatch.setattr(node_access, "node_data_topics", node_topics)
    await _node_heard(main, node_process)

    await _spawned_on_the_node(main, "outside", OUTSIDE)

    def _said() -> list[str]:
        return [str(n.get("message", "")) for n in main._pending_notifications]

    # The second topic is in the manifest by then too: it is the one sent after it.
    await until(
        lambda: "plant/pump" in main.manifests.manifests.get("outside", {}).get("publishes", []),
        "the agent's manifest naming both topics",
        NODE_WAIT_S,
    )
    about = [message for message in _said() if "'outside' on node 'node-a'" in message]
    assert len(about) == 1
    assert "publishes to `anomalies/imu`" in about[0]
    assert "add `anomalies/imu` to WACTORZ_NODE_TOPICS" in about[0]
