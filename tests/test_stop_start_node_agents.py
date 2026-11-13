# SPDX-License-Identifier: Apache-2.0
"""An agent on a node can be stopped, stays stopped, and starts again where it was.

A node stops an agent by forgetting it and keeping its state file, so what makes
a stop a state rather than a slow delete is main remembering it: the agent keeps
its entry in the spawn registry, marked stopped beside it, and every path that
starts agents or tidies away those that went quiet leaves it alone. A start is
the agent's spawn sent again from that entry.

Main is real here and never started: what it would publish is recorded, and its
node is one it was told of by hand.
"""

import time
from pathlib import Path
from typing import Any, cast

import pytest

from wactorz.agents.main import alongside
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.nodes import OFFLINE_GRACE_S, VANISH_MISS_THRESHOLD
from wactorz.agents.main.spawn_registry import STOPPED_AGENTS_KEY, SpawnRegistry
from wactorz.core.addresses import Address
from wactorz.core.manifests import NodeManifest
from wactorz.core.topics import node_topic

NODE = "rpi"
OTHER = "pc"
CODE = "async def setup(agent):\n    pass\n"

#: An id as a store keeps one.
KEPT_ID = "3f2b8a52-7c1d-4e9a-9d55-0c6a1b2e4f70"

Sent = list[tuple[str, Any]]


def _known(main: MainActor, node: str, agents: list[str] | None = None) -> None:
    """Have ``main`` hear from ``node`` just now, running ``agents``."""
    main._known_nodes[node] = {"last_seen": time.time(), "agents": agents or [], "status": "online"}
    # A node of this release: it runs an agent under the id its spawn names.
    main.nodes.node_manifests[node] = cast(NodeManifest, {"manifest_v": 1, "actor_ids": True})


def _recorder(sent: Sent) -> Any:
    """A publish that keeps what it was given in ``sent``, in place of the broker."""

    async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        sent.append((topic, payload))

    return _publish


@pytest.fixture(name="sent")
def sent_fixture() -> Sent:
    return []


@pytest.fixture(name="main")
def main_fixture(tmp_path: Path, sent: Sent, monkeypatch: pytest.MonkeyPatch) -> MainActor:
    """Main with both nodes online and `cam` running on the first."""
    main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))

    monkeypatch.setattr(main, "_mqtt_publish", _recorder(sent))
    _known(main, NODE, ["cam"])
    _known(main, OTHER)
    return main


async def _placed(main: MainActor, sent: Sent, name: str = "cam", node: str = NODE) -> str:
    """Spawn ``name`` on ``node``, as a chat spawn does; its id. Nothing sent is kept."""
    config: dict[str, Any] = {"name": name, "type": "dynamic", "code": CODE}
    if main._get_spawn_registry().named(name):
        # A second agent of the name, as `/agents copy` makes one: with an id of its own.
        config["alongside"] = True
    assert await main._spawn_remote(config, node, save=True)
    sent.clear()
    entry = main._get_spawn_registry().at(name, node)
    assert entry is not None
    return str(entry["actor_id"])


def _topics(sent: Sent) -> list[str]:
    return [topic for topic, _ in sent]


def _desired(sent: Sent, node: str = NODE) -> list[str]:
    """The agents the last desired state sent to ``node`` lists."""
    states = [payload for topic, payload in sent if topic == node_topic(node, "desired_state")]
    assert states, "no desired state was sent"
    return [str(agent["name"]) for agent in states[-1]["agents"]]


def _is_stopped(main: MainActor, actor_id: str) -> bool:
    return main._get_spawn_registry().is_stopped(actor_id)


class TestTheRegistry:
    def test_a_stop_is_kept_beside_the_entries_and_never_in_one(self) -> None:
        entry = {"name": "cam", "node": NODE, "actor_id": "id-cam"}
        registry = SpawnRegistry({"id-cam": entry}).with_stopped("id-cam", True)

        assert registry.is_stopped("id-cam")
        assert registry.stopped_form() == ["id-cam"]
        assert registry.stored_form() == {"id-cam": entry}
        assert registry.by_id("id-cam") == entry

    def test_the_agents_that_should_run_on_a_node_leave_the_stopped_out(self) -> None:
        cam = {"name": "cam", "node": NODE, "actor_id": "id-cam"}
        mic = {"name": "mic", "node": NODE, "actor_id": "id-mic"}
        registry = SpawnRegistry({"id-cam": cam, "id-mic": mic}, ["id-cam"])

        assert registry.on_node(NODE) == [cam, mic]
        assert registry.running_on(NODE) == [mic]
        assert registry.with_stopped("id-cam", False).running_on(NODE) == [cam, mic]

    def test_an_entry_that_goes_takes_its_stop_with_it(self) -> None:
        cam = {"name": "cam", "node": NODE, "actor_id": "id-cam"}
        registry = SpawnRegistry({"id-cam": cam}, ["id-cam"]).without("id-cam")

        assert registry.stopped_form() == []
        assert not registry.is_stopped("id-cam")

    @pytest.mark.parametrize("stored", [None, KEPT_ID, {KEPT_ID: True}, ["gone", 7]])
    def test_a_stored_stop_that_names_no_entry_says_nothing(self, stored: Any) -> None:
        by_id = {KEPT_ID: {"name": "cam", "node": NODE}}

        assert SpawnRegistry.stored(by_id, None, stored).stopped_form() == []

    def test_it_is_read_back_as_it_was_stored(self) -> None:
        by_id = {KEPT_ID: {"name": "cam", "node": NODE}}

        assert SpawnRegistry.stored(by_id, None, [KEPT_ID]).is_stopped(KEPT_ID)


class TestStopping:
    async def test_it_leaves_the_desired_state_and_is_told_to_stop(
        self, main: MainActor, sent: Sent
    ) -> None:
        actor_id = await _placed(main, sent)

        outcome = await main.stop_agent_on_node("cam", NODE)

        assert outcome.sent
        assert "/start cam@rpi" in outcome.message
        assert sent == [
            (node_topic(NODE, "desired_state"), sent[0][1]),
            (node_topic(NODE, "stop"), {"name": "cam"}),
        ]
        assert _desired(sent) == []
        assert _is_stopped(main, actor_id)
        assert main._get_spawn_registry().at("cam", NODE) is not None

    async def test_the_stop_is_remembered_before_anything_is_sent(
        self, main: MainActor, sent: Sent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The node may answer either message with the agent's manifest withdrawn.
        actor_id = await _placed(main, sent)
        remembered: list[bool] = []

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            remembered.append(_is_stopped(main, actor_id))

        monkeypatch.setattr(main, "_mqtt_publish", _publish)

        await main.stop_agent_on_node("cam", NODE)

        assert remembered == [True, True]

    async def test_a_desired_state_that_cannot_be_sent_leaves_it_running(
        self, main: MainActor, sent: Sent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor_id = await _placed(main, sent)

        async def _down(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            raise ConnectionError("no broker")

        monkeypatch.setattr(main, "_mqtt_publish", _down)

        outcome = await main.stop_agent_on_node("cam", NODE)

        assert not outcome.sent
        assert outcome.message == "'cam' on node 'rpi' was not stopped: no broker"
        assert not _is_stopped(main, actor_id)

    async def test_a_stop_message_that_cannot_be_sent_still_stops_it(
        self, main: MainActor, sent: Sent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The desired state left without the agent, and the node stops what it leaves out.
        actor_id = await _placed(main, sent)

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            if topic == node_topic(NODE, "stop"):
                raise ConnectionError("no broker")
            sent.append((topic, payload))

        monkeypatch.setattr(main, "_mqtt_publish", _publish)

        outcome = await main.stop_agent_on_node("cam", NODE)

        assert outcome.sent
        assert _desired(sent) == []
        assert _is_stopped(main, actor_id)

    async def test_stopping_it_again_sends_nothing(self, main: MainActor, sent: Sent) -> None:
        await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        outcome = await main.stop_agent_on_node("cam", NODE)

        assert outcome.sent
        assert outcome.message == "'cam' on node 'rpi' is already stopped."
        assert sent == []

    async def test_an_agent_main_did_not_spawn_is_told_to_stop_and_no_more(
        self, main: MainActor, sent: Sent
    ) -> None:
        outcome = await main.stop_agent_on_node("its-own", NODE)

        assert outcome.sent
        assert "cannot be started again from here" in outcome.message
        assert sent == [(node_topic(NODE, "stop"), {"name": "its-own"})]
        assert main.recall(STOPPED_AGENTS_KEY) is None


class TestStayingStopped:
    async def test_another_agents_spawn_does_not_list_it_again(
        self, main: MainActor, sent: Sent
    ) -> None:
        await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        await _placed(main, sent, "mic")
        await main._update_node_desired_state(NODE)

        assert _desired(sent) == ["mic"]

    async def test_a_restart_of_main_does_not_start_it(self, main: MainActor, sent: Sent) -> None:
        await _placed(main, sent)
        await _placed(main, sent, "mic")
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        await main.spawns._restore_spawned_agents()

        spawned = [payload["name"] for topic, payload in sent if topic == node_topic(NODE, "spawn")]
        assert spawned == ["mic"]
        assert _desired(sent) == ["mic"]

    async def test_it_is_not_taken_for_one_that_crashed(self, main: MainActor, sent: Sent) -> None:
        actor_id = await _placed(main, sent)
        await _placed(main, sent, "mic")
        await main.stop_agent_on_node("cam", NODE)

        # Heartbeat after heartbeat without either: `mic` is gone, `cam` was stopped.
        pruned: list[str] = []
        for _ in range(VANISH_MISS_THRESHOLD + 1):
            pruned += main.nodes.agents_to_prune(NODE, set(), {"cam", "mic"})

        assert pruned == ["mic"]
        assert _is_stopped(main, actor_id)

    async def test_its_node_going_offline_keeps_it(self, main: MainActor, sent: Sent) -> None:
        actor_id = await _placed(main, sent)
        await _placed(main, sent, "mic")
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        await main.nodes.forget_offline_nodes(time.time() + OFFLINE_GRACE_S + 60)

        registry = main._get_spawn_registry()
        assert [entry["name"] for entry in registry.on_node(NODE)] == ["cam"]
        assert registry.is_stopped(actor_id)
        assert (node_topic(NODE, "stop"), {"name": "cam"}) not in sent

    @pytest.mark.parametrize("still_listed", [["cam"], []])
    async def test_its_manifest_withdrawn_keeps_it_and_tells_nobody_it_went(
        self, main: MainActor, sent: Sent, still_listed: list[str]
    ) -> None:
        # An agent withdraws its manifest as it stops. Whoever shows agents is
        # told of none going, or a stopped agent would leave the dashboard and
        # take the way to start it with it.
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        main._known_nodes[NODE]["agents"] = still_listed
        told: list[str] = []

        async def _watcher(gone: str) -> None:
            told.append(gone)

        main.lifecycle.watchers.append(_watcher)
        sent.clear()

        forgotten = await main.lifecycle.agent_withdrew(actor_id, "cam")

        # Its manifest is forgotten, as that of any agent not running.
        assert forgotten is True
        assert told == []
        assert main._get_spawn_registry().at("cam", NODE) is not None
        assert _is_stopped(main, actor_id)
        assert node_topic(NODE, "desired_state") not in _topics(sent)

    async def test_one_that_runs_and_withdraws_is_removed_as_before(
        self, main: MainActor, sent: Sent
    ) -> None:
        actor_id = await _placed(main, sent)
        main._known_nodes[NODE]["agents"] = []

        await main.lifecycle.agent_withdrew(actor_id, "cam")

        assert main._get_spawn_registry().at("cam", NODE) is None

    async def test_a_move_that_did_not_happen_puts_it_back_stopped(
        self, main: MainActor, sent: Sent
    ) -> None:
        await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        await alongside.put_back(main, Address("cam", NODE))

        assert sent == []

    async def test_it_is_not_moved_while_it_is_stopped(self, main: MainActor, sent: Sent) -> None:
        await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        result = await main.migrate_agent("cam", OTHER)

        assert result["success"] is False
        assert "is stopped on 'rpi'. Start it there first (/start cam@rpi)" in result["message"]
        assert sent == []


class TestStarting:
    async def test_it_is_spawned_again_from_the_entry_main_kept(
        self, main: MainActor, sent: Sent
    ) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        outcome = await main.start_agent_on_node("cam", NODE)

        assert outcome.sent
        assert outcome.message.startswith("Start sent to 'cam' on node 'rpi'.")
        (spawn,) = [payload for topic, payload in sent if topic == node_topic(NODE, "spawn")]
        assert (spawn["name"], spawn["actor_id"], spawn["code"]) == ("cam", actor_id, CODE)
        assert _desired(sent) == ["cam"]
        assert not _is_stopped(main, actor_id)

    async def test_nothing_sent_to_a_node_says_it_was_stopped(
        self, main: MainActor, sent: Sent
    ) -> None:
        await _placed(main, sent)
        await _placed(main, sent, "mic")
        await main.stop_agent_on_node("cam", NODE)
        await main.start_agent_on_node("cam", NODE)

        configs = [payload for topic, payload in sent if topic == node_topic(NODE, "spawn")]
        for topic, payload in sent:
            if topic == node_topic(NODE, "desired_state"):
                configs += payload["agents"]
        assert configs
        assert all("stopped" not in str(sorted(config)) for config in configs)

    async def test_a_node_that_is_away_leaves_it_stopped(self, main: MainActor, sent: Sent) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        del main._known_nodes[NODE]
        sent.clear()

        outcome = await main.start_agent_on_node("cam", NODE)

        assert not outcome.sent
        assert outcome.message == (
            "'cam' was not started: node 'rpi' is not online. It stays stopped."
        )
        assert sent == []
        assert _is_stopped(main, actor_id)

    async def test_a_node_that_cannot_take_it_leaves_it_stopped(
        self, main: MainActor, sent: Sent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        async def _refused(*_args: Any, **_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(main.spawns, "_spawn_remote", _refused)

        outcome = await main.start_agent_on_node("cam", NODE)

        assert not outcome.sent
        assert "cannot take it now" in outcome.message
        assert _is_stopped(main, actor_id)

    async def test_a_spawn_that_fails_leaves_it_stopped_and_says_why(
        self, main: MainActor, sent: Sent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        async def _broken(*_args: Any, **_kwargs: Any) -> bool:
            raise ConnectionError("no broker")

        monkeypatch.setattr(main.spawns, "_spawn_remote", _broken)

        outcome = await main.start_agent_on_node("cam", NODE)

        assert not outcome.sent
        assert outcome.message == "'cam' was not started on node 'rpi': no broker"
        assert _is_stopped(main, actor_id)

    async def test_a_spawn_that_left_is_a_start_whatever_fails_after_it(
        self, main: MainActor, sent: Sent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The node has the spawn and runs the agent: main says so, and no
        # longer keeps it as stopped.
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            if topic == node_topic(NODE, "desired_state"):
                raise ConnectionError("no broker")
            sent.append((topic, payload))

        monkeypatch.setattr(main, "_mqtt_publish", _publish)

        outcome = await main.start_agent_on_node("cam", NODE)

        assert node_topic(NODE, "spawn") in _topics(sent)
        assert outcome.sent
        assert outcome.message.startswith("Start sent to 'cam' on node 'rpi', but")
        assert "no broker" in outcome.message
        assert not _is_stopped(main, actor_id)
        # The next change on that node sends the list whole, the agent in it.
        monkeypatch.setattr(main, "_mqtt_publish", _recorder(sent))
        await main._update_node_desired_state(NODE)
        assert _desired(sent) == ["cam"]

    async def test_one_that_runs_is_not_started_again(self, main: MainActor, sent: Sent) -> None:
        await _placed(main, sent)

        outcome = await main.start_agent_on_node("cam", NODE)

        assert not outcome.sent
        assert outcome.message == "'cam' on node 'rpi' is not stopped."
        assert sent == []

    async def test_one_main_keeps_no_config_for_cannot_be(
        self, main: MainActor, sent: Sent
    ) -> None:
        outcome = await main.start_agent_on_node("its-own", NODE)

        assert not outcome.sent
        assert "main keeps no config for it" in outcome.message
        assert sent == []


class TestRestarting:
    async def test_a_running_one_is_replaced_in_place_by_its_node(
        self, main: MainActor, sent: Sent
    ) -> None:
        actor_id = await _placed(main, sent)

        outcome = await main.restart_agent_on_node("cam", NODE)

        assert outcome.sent
        assert sent == [(node_topic(NODE, "restart_agent"), {"name": "cam"})]
        # Nothing is remembered as stopped on the way, so nothing is left if it fails.
        assert not _is_stopped(main, actor_id)

    async def test_a_stopped_one_is_started(self, main: MainActor, sent: Sent) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)
        sent.clear()

        outcome = await main.restart_agent_on_node("cam", NODE)

        assert outcome.sent
        assert node_topic(NODE, "spawn") in _topics(sent)
        assert node_topic(NODE, "restart_agent") not in _topics(sent)
        assert not _is_stopped(main, actor_id)


class TestWhichAgentOfAName:
    async def test_the_one_on_the_node_named_is_stopped_and_the_other_runs_on(
        self, main: MainActor, sent: Sent
    ) -> None:
        on_rpi = await _placed(main, sent)
        on_pc = await _placed(main, sent, "cam", OTHER)
        assert on_rpi != on_pc

        await main.stop_agent_on_node("cam", OTHER, on_pc)

        assert _is_stopped(main, on_pc)
        assert not _is_stopped(main, on_rpi)
        assert node_topic(NODE, "stop") not in _topics(sent)
        assert _desired(sent, OTHER) == []

    async def test_an_id_that_is_another_agents_does_not_pick_it(
        self, main: MainActor, sent: Sent
    ) -> None:
        cam = await _placed(main, sent)
        mic = await _placed(main, sent, "mic")

        # Asked for `cam`, with the id of `mic`: the name on that node decides.
        await main.stop_agent_on_node("cam", NODE, mic)

        assert _is_stopped(main, cam)
        assert not _is_stopped(main, mic)


class TestWhatEndsAStop:
    async def test_deleting_it_forgets_the_stop_with_the_entry(
        self, main: MainActor, sent: Sent
    ) -> None:
        await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        await main.delete_spawned_agent("cam", NODE)

        assert main._get_spawn_registry().at("cam", NODE) is None
        assert main.recall(STOPPED_AGENTS_KEY) == []

    async def test_a_spawn_over_it_runs_it(self, main: MainActor, sent: Sent) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        config = {"name": "cam", "type": "dynamic", "code": CODE, "replace": True}
        assert await main._spawn_remote(config, NODE, save=True)

        assert not _is_stopped(main, actor_id)
        assert _desired(sent) == ["cam"]

    async def test_saved_as_mains_own_it_is_no_longer_one_stopped_on_a_node(
        self, main: MainActor, sent: Sent
    ) -> None:
        actor_id = await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        main._save_to_spawn_registry({"name": "cam", "type": "dynamic", "code": CODE})

        registry = main._get_spawn_registry()
        assert registry.at("cam", "") is not None
        assert not registry.is_stopped(actor_id)

    async def test_clearing_the_registry_clears_what_was_stopped(
        self, main: MainActor, sent: Sent
    ) -> None:
        await _placed(main, sent)
        await main.stop_agent_on_node("cam", NODE)

        main._clear_spawn_registry()

        assert main.recall(STOPPED_AGENTS_KEY) == []
