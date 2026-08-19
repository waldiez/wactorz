"""Vanish-detection tolerates a transient node-heartbeat gap.

A registry-claimed remote agent that is missing from a single node heartbeat is a
transient discovery gap, not a crash — pruning it on the spot drops a live agent
from the spawn registry (breaking delete and node-reboot recovery). It is now
pruned only after several consecutive heartbeats omit it; a reappearance resets
the counter, and an agent that never appeared (just spawned) or migrated away is
never pruned.
"""

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.migration import Migration
from wactorz.agents.nodes import VANISH_MISS_THRESHOLD, NodeManager
from wactorz.agents.spawns import SpawnService


def _bare_main(registry: dict) -> MainActor:
    """A MainActor with only what _agents_to_prune touches, stubbed."""
    m = MainActor.__new__(MainActor)
    m.manifests = ManifestRegistry(m)
    m.nodes = NodeManager(m, m.manifests)
    m.spawns = SpawnService(m)
    m.migration = Migration(m, m.nodes)
    m.name = "main"
    m._node_agent_misses = {}
    m.recall = lambda *a, **k: registry  # the spawn registry
    return m


def test_present_agent_is_never_pruned():
    m = _bare_main({"system-monitor": {"node": "rpi-test"}})
    assert m._agents_to_prune("rpi-test", {"system-monitor"}, {"system-monitor"}) == []
    assert m._node_agent_misses == {}


def test_single_missed_heartbeat_does_not_prune():
    m = _bare_main({"system-monitor": {"node": "rpi-test"}})
    # present last heartbeat, absent now → one miss, kept
    assert m._agents_to_prune("rpi-test", set(), {"system-monitor"}) == []
    assert m._node_agent_misses  # counting


def test_consecutive_misses_prune():
    m = _bare_main({"system-monitor": {"node": "rpi-test"}})
    prev = {"system-monitor"}
    result: list[str] = []
    for _ in range(VANISH_MISS_THRESHOLD):
        result = m._agents_to_prune("rpi-test", set(), prev)
        prev = set()  # after the first miss the agent is no longer in prev
    assert result == ["system-monitor"]
    assert m._node_agent_misses == {}  # counter cleared on prune


def test_reappearance_resets_the_counter():
    m = _bare_main({"system-monitor": {"node": "rpi-test"}})
    prev = {"system-monitor"}
    for _ in range(VANISH_MISS_THRESHOLD - 1):  # miss short of the threshold
        m._agents_to_prune("rpi-test", set(), prev)
        prev = set()
    m._agents_to_prune("rpi-test", {"system-monitor"}, prev)  # seen again → reset
    prev = {"system-monitor"}
    result: list[str] = []
    for _ in range(VANISH_MISS_THRESHOLD - 1):  # miss short again → still alive
        result = m._agents_to_prune("rpi-test", set(), prev)
        prev = set()
    assert result == []


def test_never_appeared_agent_is_not_pruned():
    # just spawned into the registry; the node hasn't reported it yet
    m = _bare_main({"just-spawned": {"node": "rpi-test"}})
    assert m._agents_to_prune("rpi-test", set(), set()) == []
    assert m._node_agent_misses == {}


def test_migrated_away_agent_is_skipped():
    # registry now names a different node → not this node's concern
    m = _bare_main({"system-monitor": {"node": "other-node"}})
    assert m._agents_to_prune("rpi-test", set(), {"system-monitor"}) == []
    assert m._node_agent_misses == {}
