"""A recorder on a node files a reading, and the file reaches main's data directory, over a real broker.

Every hop is a real one: main spawns the recorder on the node as a library
agent, the reading goes over the broker to the node, the recorder files it,
the node announces and sends the closed file on the blob path, main's listener
takes it from its inbox into its own data layout and answers, and the node
marks the file answered for.
"""

import asyncio
from pathlib import Path

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.core.paths import resolve_state_dir
from wactorz.data import data_root
from wactorz.data.parquet import frame

from .conftest import FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

T0 = 1_760_000_000.0


async def test_a_nodes_recorded_file_is_filed_on_main(main: MainActor, node: FastNode) -> None:
    config = {
        "type": "module",
        "target": "wactorz.agents.recorder_agent:RecorderAgent",
        "name": "recorder",
        "options": {"topics": ["sensors/ship/#"], "source_field": "device", "batch_rows": 1},
    }
    # The node says which library agents it builds in its manifest, which
    # arrives after its first heartbeat; a spawn before it is refused.
    await until(
        lambda: config["target"] in main.nodes.targets_of(node.node_name),
        "the node's manifest naming the recorder",
    )
    await main._spawn_remote(config, node.node_name, save=True)
    await until(
        lambda: "recorder" in main._known_nodes.get(node.node_name, {}).get("agents", []),
        "main seeing the recorder on the node",
    )
    hosted = node.hosted("recorder")
    assert hosted is not None
    await until(lambda: hosted.state.value == "running", "the recorder running")
    await asyncio.sleep(0.5)  # its subscription settling on the broker

    await main.publish("sensors/ship/left", {"device": "ship-left", "ts": T0, "ax": 1.5})
    node_root = data_root(node.state_dir)
    await until(
        lambda: frame(node_root, "ship-left", T0, T0 + 1).num_rows == 1,
        "the node filing the reading",
    )

    sent = await node.shipper.look()
    assert sent == ["ship-left/2025/10/09/08.parquet"]
    main_root = data_root(resolve_state_dir())
    await until(
        lambda: frame(main_root, "ship-left", T0, T0 + 1).num_rows == 1,
        "main filing the node's file",
    )
    await until(
        lambda: node.shipper._load()["ship-left/2025/10/09/08.parquet"].get("acked") is True,
        "the node hearing main's yes",
    )
    assert (main_root / "ship-left/2025/10/09/08.parquet").is_file()
    assert Path(node_root / "ship-left/2025/10/09/08.parquet").is_file()
