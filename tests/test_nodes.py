"""What MainActor reports about remote nodes, pinned as it is.

Four readers share one dict of heartbeats: the node listing the dashboard and
`/nodes` render, the online predicate, the sorted online-name list, and the
lookup that decides which node an agent is running on.

These tests describe current behaviour so the shared state can be moved to a
collaborator as a move: the same answers, the same boundary, the same keys. The
key names matter beyond this module — the dashboard reads them off the wire.
"""

import time
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.nodes import OFFLINE_GRACE_S, NodeManager

#: The window every "is it online" reader uses. Written as a literal `30` at
#: three call sites rather than shared, which is the thing worth watching.
ONLINE_WINDOW_S = 30


def node(*, seen_ago: float = 0.0, agents: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    """One entry of `_known_nodes`, last heard from `seen_ago` seconds back."""
    return {"last_seen": time.time() - seen_ago, "agents": list(agents), **extra}


def make_main(**nodes: dict[str, Any]) -> MainActor:
    """A MainActor holding `nodes` and nothing else the node readers touch.

    `__new__` skips `__init__`, so the collaborator that owns the heartbeat
    table is built here. Assignment still goes through `_known_nodes`, which is
    the name the planner, the CLI and the chat router reach for.
    """
    m = MainActor.__new__(MainActor)
    m.name = "main"
    m.nodes = NodeManager()
    m._known_nodes = dict(nodes)
    return m


class TestWhatTheNodeListingReports:
    """`list_nodes` feeds `/nodes` and the dashboard, so its keys are a contract."""

    def test_it_reports_one_entry_per_known_node(self) -> None:
        main = make_main(alpha=node(), beta=node())

        assert len(main.list_nodes()) == 2

    def test_it_reports_nothing_when_no_node_has_been_seen(self) -> None:
        assert not make_main().list_nodes()

    def test_every_entry_carries_the_keys_its_readers_expect(self) -> None:
        main = make_main(alpha=node(agents=("collector",)))

        entry = main.list_nodes()[0]

        assert set(entry) == {
            "node",
            "agents",
            "last_seen",
            "online",
            "pid",
            "uptime_s",
            "cpu_pct",
            "mem_used_mb",
            "mem_free_mb",
        }

    def test_the_name_comes_from_the_key_not_the_value(self) -> None:
        main = make_main(alpha=node())

        assert main.list_nodes()[0]["node"] == "alpha"

    def test_system_metrics_are_reported_when_the_node_sent_them(self) -> None:
        main = make_main(alpha=node(pid=42, cpu_pct=17.5, mem_free_mb=120))

        entry = main.list_nodes()[0]

        assert (entry["pid"], entry["cpu_pct"], entry["mem_free_mb"]) == (42, 17.5, 120)

    def test_a_metric_the_node_never_sent_is_reported_as_absent(self) -> None:
        # Absent rather than zero: a node that reports no CPU figure has not
        # reported an idle one.
        entry = make_main(alpha=node()).list_nodes()[0]

        assert entry["cpu_pct"] is None
        assert entry["pid"] is None

    def test_a_node_with_no_agent_list_reports_an_empty_one(self) -> None:
        main = make_main(alpha={"last_seen": time.time()})

        assert not main.list_nodes()[0]["agents"]


class TestTheOnlineWindow:
    """Three readers decide "online" from the same number, written three times.

    The boundary cases are here because that is where a repeated literal drifts
    first, and because all three have to answer the same way for the dashboard
    and the migration check to agree about one node.
    """

    @pytest.mark.parametrize("seen_ago", [0.0, 1.0, ONLINE_WINDOW_S - 1])
    def test_a_node_heard_from_inside_the_window_is_online(self, seen_ago: float) -> None:
        main = make_main(alpha=node(seen_ago=seen_ago))

        assert main._node_is_online("alpha")
        assert main.list_nodes()[0]["online"]

    @pytest.mark.parametrize("seen_ago", [ONLINE_WINDOW_S, ONLINE_WINDOW_S + 1, 3600])
    def test_a_node_silent_for_the_window_is_offline(self, seen_ago: float) -> None:
        main = make_main(alpha=node(seen_ago=seen_ago))

        assert not main._node_is_online("alpha")
        assert not main.list_nodes()[0]["online"]

    def test_the_listing_and_the_predicate_agree_at_the_boundary(self) -> None:
        # Stated on its own because they are separate copies of the same
        # comparison: one reads `<`, and a later edit to either alone is
        # exactly the drift this pins.
        main = make_main(alpha=node(seen_ago=ONLINE_WINDOW_S))

        assert main.list_nodes()[0]["online"] == main._node_is_online("alpha")

    def test_an_unknown_node_is_offline_rather_than_an_error(self) -> None:
        assert not make_main(alpha=node())._node_is_online("ghost")

    def test_a_node_that_never_reported_a_heartbeat_is_offline(self) -> None:
        # `last_seen` missing defaults to 0, which is 1970 and always stale.
        main = make_main(alpha={"agents": []})

        assert not main._node_is_online("alpha")


class TestTheOnlineNames:
    def test_only_online_nodes_are_named(self) -> None:
        main = make_main(fresh=node(), stale=node(seen_ago=ONLINE_WINDOW_S + 1))

        assert main._online_node_names() == ["fresh"]

    def test_the_names_are_sorted_rather_than_in_arrival_order(self) -> None:
        # Insertion order here is deliberately reversed: this list reaches the
        # user in an error message, so it is ordered for reading.
        main = make_main(zulu=node(), alpha=node(), mike=node())

        assert main._online_node_names() == ["alpha", "mike", "zulu"]

    def test_nothing_is_named_when_every_node_is_silent(self) -> None:
        main = make_main(alpha=node(seen_ago=3600), beta=node(seen_ago=3600))

        assert not main._online_node_names()


class TestWhichNodeRunsAnAgent:
    """The lookup `migrate_agent` and `delete_spawned_agent` use."""

    def test_it_names_the_node_running_the_agent(self) -> None:
        main = make_main(alpha=node(agents=("collector",)), beta=node(agents=("optimizer",)))

        assert main._node_running_agent("optimizer") == "beta"

    def test_an_agent_on_no_node_is_reported_as_empty(self) -> None:
        # Empty string rather than None: callers test it as a string.
        main = make_main(alpha=node(agents=("collector",)))

        assert main._node_running_agent("ghost") == ""

    def test_a_stale_node_does_not_claim_its_agents(self) -> None:
        # The same freshness window again, third copy. An agent on a node that
        # stopped reporting is treated as not running there, which is what lets
        # a migration proceed rather than refusing on a node that is gone.
        main = make_main(alpha=node(seen_ago=ONLINE_WINDOW_S + 1, agents=("collector",)))

        assert main._node_running_agent("collector") == ""

    def test_a_fresh_node_claims_its_agents_at_the_same_boundary(self) -> None:
        main = make_main(alpha=node(seen_ago=ONLINE_WINDOW_S - 1, agents=("collector",)))

        assert main._node_running_agent("collector") == "alpha"

    def test_a_local_only_agent_belongs_to_no_node(self) -> None:
        assert make_main()._node_running_agent("main") == ""


class TestWhichAgentsAreRunningRemotely:
    """The union across online nodes, which decides what counts as remote."""

    def test_it_names_agents_on_online_nodes(self) -> None:
        main = make_main(
            alpha=node(agents=("collector",)),
            beta=node(agents=("optimizer", "watcher")),
        )

        assert main.nodes.running_agents() == {"collector", "optimizer", "watcher"}

    def test_a_stale_node_contributes_nothing(self) -> None:
        main = make_main(
            fresh=node(agents=("collector",)),
            stale=node(seen_ago=ONLINE_WINDOW_S + 1, agents=("ghost",)),
        )

        assert main.nodes.running_agents() == {"collector"}

    def test_an_agent_on_two_nodes_is_named_once(self) -> None:
        main = make_main(alpha=node(agents=("shared",)), beta=node(agents=("shared",)))

        assert main.nodes.running_agents() == {"shared"}

    def test_nothing_runs_remotely_when_no_node_is_known(self) -> None:
        assert not make_main().nodes.running_agents()


class TestTheTwoWindowsAreDifferentOnPurpose:
    """The indicator and the pruning grace are not the same number.

    Written down because they look like a mistake: the visual indicator is
    short so the dashboard feels responsive, while treating a node's agents as
    gone waits long enough that a brief network blip does not delete anything.
    Unifying them would either make the dashboard sluggish or make a blip
    destructive.
    """

    def test_the_prune_grace_is_longer_than_the_online_window(self) -> None:
        assert OFFLINE_GRACE_S > ONLINE_WINDOW_S

    def test_a_node_can_read_offline_without_being_prunable(self) -> None:
        # The gap between the two windows is where a blip lives: the node shows
        # as offline while its agents are left alone. If the two ever became one
        # number this gap would close and there would be no such state.
        seen_ago = (ONLINE_WINDOW_S + OFFLINE_GRACE_S) / 2
        main = make_main(alpha=node(seen_ago=seen_ago, agents=("collector",)))

        assert not main._node_is_online("alpha")
        assert seen_ago < OFFLINE_GRACE_S
