"""Where an agent runs follows its heartbeat, back home included.

Every heartbeat says where its agent runs: a node's name, or "" for this
server. The dashboard places a card from what the server's state says, so a
node kept there after the agent moved home lists it under a node that no longer
runs it, and under no row at all once that node stops reporting it.
"""

from typing import Any

import pytest

from wactorz.web import events, runtime


@pytest.fixture(name="clean_state", autouse=True)
def clean_state_fixture() -> Any:
    """Each case starts with no agents recorded."""
    before = runtime.state.get("agents")
    runtime.state["agents"] = {}
    yield
    runtime.state["agents"] = before if before is not None else {}


def _beat(**fields: Any) -> dict[str, Any]:
    events.record_heartbeat("a1", {"name": "counter", "state": "running", **fields})
    return runtime.state["agents"]["a1"]


def test_a_node_is_taken_from_the_heartbeat() -> None:
    assert _beat(node="edge")["node"] == "edge"


def test_moved_home_it_is_local_again() -> None:
    _beat(node="edge")

    entry = _beat(node="")

    assert "node" not in entry


def test_the_snapshot_the_page_reads_says_so_too() -> None:
    _beat(node="edge")
    _beat(node="")

    [agent] = [a for a in events.snapshot()["agents"] if a.get("name") == "counter"]

    assert "node" not in agent


def test_a_heartbeat_that_does_not_say_leaves_it_alone() -> None:
    _beat(node="edge")

    assert _beat()["node"] == "edge"


def test_moved_out_again_it_follows() -> None:
    _beat(node="edge")
    _beat(node="")

    assert _beat(node="pi")["node"] == "pi"
