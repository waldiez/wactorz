"""A redeploy keeps the node's agents.

A deploy kills the node's old process and starts the new one. From main's side
that looks like a crash twice over: the last heartbeats before the kill may
already have gone quiet on an agent, and the new process's first heartbeats
list no agents at all, because it reconciles against the desired state only
after it has connected. The prune counted those misses and deleted the agent
from the registry and the desired state a quarter of a second after `pkill` --
so the new node came up, asked what it should be running, and was told nothing.

The deploy now tells main the node is being redeployed. Until the node's first
heartbeat after the restart, its missing agents are not pruned and its silence
is not treated as the node going away; when the flag lifts, the miss counters
are reset so the new node's empty first heartbeats do not finish the job.
"""

import time
from typing import Any, cast

from wactorz.agents.installer_agent import InstallerAgent
from wactorz.agents.main.hosts import NodeHost
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import (
    OFFLINE_GRACE_S,
    VANISH_MISS_THRESHOLD,
    NodeManager,
)


class _Host:
    """Records what pruning and forgetting remove."""

    name = "main"
    #: No live registry: the heartbeat path also nudges the monitor, and skips
    #: that when there is nobody to nudge.
    _registry = None

    def __init__(self, registry: dict[str, dict[str, Any]]) -> None:
        self.registry = dict(registry)
        self.events: list[tuple[Any, ...]] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.registry

    def _remove_from_spawn_registry(self, name: str) -> None:
        self.registry.pop(name, None)
        self.events.append(("unregistered", name))

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        self.events.append(("desired_state", node, remove_name))

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.events.append(("publish", topic, payload, qos))

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        self.events.append(("manifest", name))

    def _record_agent_deletion(self, name: str, reason: str = "") -> None:
        self.events.append(("deleted", name))

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.events.append(("notice", notice["message"]))


def _nodes(host: _Host) -> NodeManager:
    return NodeManager(cast("NodeHost", host), ManifestRegistry(cast("Any", host)))


async def _heartbeat(nodes: NodeManager, node: str, agents: list[str]) -> None:
    await nodes.receive_heartbeat(node, {"agents": agents})


class TestWhileTheNodeIsBeingRedeployed:
    async def test_empty_heartbeats_do_not_prune_its_agents(self) -> None:
        host = _Host({"collector": {"node": "rpi"}})
        nodes = _nodes(host)
        await _heartbeat(nodes, "rpi", ["collector"])

        nodes.begin_redeploy("rpi")
        for _ in range(VANISH_MISS_THRESHOLD + 1):
            await _heartbeat(nodes, "rpi", [])

        assert "collector" in host.registry
        assert host.events == []

    async def test_its_silence_does_not_forget_it(self) -> None:
        host = _Host({"collector": {"node": "rpi"}})
        nodes = _nodes(host)
        now = time.time()
        nodes.known = {"rpi": {"last_seen": now - OFFLINE_GRACE_S - 60, "agents": ["collector"]}}

        nodes.begin_redeploy("rpi")
        await nodes.forget_offline_nodes(now)

        assert "collector" in host.registry
        assert "rpi" in nodes.known
        assert host.events == []

    async def test_other_nodes_are_judged_as_before(self) -> None:
        host = _Host({"collector": {"node": "rpi"}, "fan": {"node": "nuc"}})
        nodes = _nodes(host)
        now = time.time()
        nodes.known = {
            "rpi": {"last_seen": now - OFFLINE_GRACE_S - 60, "agents": ["collector"]},
            "nuc": {"last_seen": now - OFFLINE_GRACE_S - 60, "agents": ["fan"]},
        }

        nodes.begin_redeploy("rpi")
        await nodes.forget_offline_nodes(now)

        assert "collector" in host.registry
        assert "fan" not in host.registry


class TestWhenTheRedeployEnds:
    async def test_the_misses_counted_during_it_are_forgotten(self) -> None:
        # The new node's first heartbeats list nothing until it has reconciled.
        # Misses counted before the kill must not add up with those.
        host = _Host({"collector": {"node": "rpi"}})
        nodes = _nodes(host)
        await _heartbeat(nodes, "rpi", ["collector"])
        for _ in range(VANISH_MISS_THRESHOLD - 1):
            await _heartbeat(nodes, "rpi", [])
        assert nodes.agent_misses[("rpi", "collector")] == VANISH_MISS_THRESHOLD - 1

        nodes.begin_redeploy("rpi")
        await _heartbeat(nodes, "rpi", [])
        nodes.end_redeploy("rpi")

        assert ("rpi", "collector") not in nodes.agent_misses
        assert "collector" in host.registry

    async def test_after_it_a_real_disappearance_is_still_caught(self) -> None:
        host = _Host({"collector": {"node": "rpi"}})
        nodes = _nodes(host)
        await _heartbeat(nodes, "rpi", ["collector"])
        nodes.begin_redeploy("rpi")
        nodes.end_redeploy("rpi")

        # Back, then gone for good.
        await _heartbeat(nodes, "rpi", ["collector"])
        for _ in range(VANISH_MISS_THRESHOLD):
            await _heartbeat(nodes, "rpi", [])

        assert "collector" not in host.registry

    def test_ending_a_redeploy_that_never_began_is_harmless(self) -> None:
        nodes = _nodes(_Host({}))

        nodes.end_redeploy("rpi")

        assert nodes.redeploying == set()


class TestTheDeployBracketsTheRestart:
    def test_it_marks_before_killing_and_unmarks_after_the_wait(self) -> None:
        import inspect

        source = inspect.getsource(InstallerAgent._node_deploy)
        mark = source.index("_mark_redeploying")
        kill = source.index("pkill -f")
        wait = source.index("_await_first_heartbeat")
        unmark = source.index("_unmark_redeploying")
        assert mark < kill < wait < unmark

    def test_a_deploy_that_fails_part_way_unmarks_too(self) -> None:
        import inspect

        source = inspect.getsource(InstallerAgent._node_deploy)
        handler = source.index("except Exception as e:")
        assert "_unmark_redeploying" in source[handler:]

    def test_marking_reaches_mains_node_table(self) -> None:
        installer = InstallerAgent.__new__(InstallerAgent)
        installer._registry = None  # no main: nothing to tell, and no error
        installer._mark_redeploying("rpi")
        installer._unmark_redeploying("rpi")
