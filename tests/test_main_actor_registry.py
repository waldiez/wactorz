"""What main remembers about the agents it spawned.

The spawn registry is what survives a restart: it is how an agent comes back,
how `/agents restart` finds its config, and how main knows a remote node has
gone quiet. The deletion note beside it exists for a different reason — the LLM
reads its own earlier "Spawned 'x'" turn and will otherwise insist the agent
still exists.

Everything here runs against a real `MainActor` with no LLM provider and
persistence redirected to a temp directory, so nothing touches a broker, a
model, or the developer's state directory.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main_actor import SPAWN_REGISTRY_KEY, MainActor
from wactorz.agents.nodes import VANISH_MISS_THRESHOLD
from wactorz.core.actor import ActorState


@pytest.fixture(name="main")
def main_fixture(tmp_path: Path) -> MainActor:
    """A main actor whose persistence is an in-memory dict.

    `persist`/`recall` are the seam every method below goes through, so
    replacing them keeps the tests to the logic rather than the storage layer,
    which has its own tests.
    """
    actor = MainActor(llm_provider=None, persistence_dir=str(tmp_path))
    store: dict[str, Any] = {}
    actor.persist = lambda key, value: store.__setitem__(key, value)  # type: ignore[method-assign]
    actor.recall = lambda key, default=None: store.get(key, default)  # type: ignore[method-assign]
    return actor


def _cfg(name: str, node: str = "") -> dict[str, Any]:
    return {"name": name, "code": "", "node": node}


class TestSpawnRegistry:
    def test_it_starts_empty(self, main: MainActor) -> None:
        assert main._get_spawn_registry() == {}

    def test_a_saved_agent_is_recalled_by_name(self, main: MainActor) -> None:
        main._save_to_spawn_registry(_cfg("weather"))

        assert list(main._get_spawn_registry()) == ["weather"]

    def test_saving_the_same_name_replaces_rather_than_duplicates(self, main: MainActor) -> None:
        main._save_to_spawn_registry(_cfg("weather", node="node-a"))
        main._save_to_spawn_registry(_cfg("weather", node="node-b"))

        reg = main._get_spawn_registry()
        # Re-spawning under an existing name is a replacement; two entries would
        # leave a stale node recorded and a restart aimed at the wrong machine.
        assert len(reg) == 1
        assert reg["weather"]["node"] == "node-b"

    def test_removing_an_agent_leaves_the_others(self, main: MainActor) -> None:
        main._save_to_spawn_registry(_cfg("weather"))
        main._save_to_spawn_registry(_cfg("news"))

        main._remove_from_spawn_registry("weather")

        assert list(main._get_spawn_registry()) == ["news"]

    def test_removing_something_absent_is_harmless(self, main: MainActor) -> None:
        main._save_to_spawn_registry(_cfg("news"))

        main._remove_from_spawn_registry("never-existed")

        assert list(main._get_spawn_registry()) == ["news"]

    def test_the_registry_is_persisted_not_just_held(self, main: MainActor) -> None:
        main._save_to_spawn_registry(_cfg("weather"))

        # Read back through recall, which is what a restart does.
        assert "weather" in (main.recall(SPAWN_REGISTRY_KEY) or {})


class TestAgentsToPrune:
    """A remote agent is dropped only after it has really gone.

    Node heartbeats list the agents running there. An agent in the registry but
    absent from the heartbeat may be gone — or the heartbeat may have raced a
    spawn. Pruning on the first miss drops live agents; never pruning leaves
    ghosts in the dashboard forever.
    """

    @staticmethod
    def _with_remote(main: MainActor, name: str = "sensor", node: str = "node-a") -> None:
        main._save_to_spawn_registry(_cfg(name, node=node))

    def test_a_present_agent_is_never_pruned(self, main: MainActor) -> None:
        self._with_remote(main)

        assert main._agents_to_prune("node-a", {"sensor"}, {"sensor"}) == []

    def test_one_missed_heartbeat_is_not_enough(self, main: MainActor) -> None:
        self._with_remote(main)

        # A single gap is a transient, not a loss.
        assert main._agents_to_prune("node-a", set(), {"sensor"}) == []

    def test_it_prunes_once_the_threshold_is_reached(self, main: MainActor) -> None:
        self._with_remote(main)

        pruned: list[str] = []
        for _ in range(VANISH_MISS_THRESHOLD):
            pruned = main._agents_to_prune("node-a", set(), {"sensor"})

        assert pruned == ["sensor"]

    def test_reappearing_resets_the_count(self, main: MainActor) -> None:
        self._with_remote(main)
        for _ in range(VANISH_MISS_THRESHOLD - 1):
            main._agents_to_prune("node-a", set(), {"sensor"})

        main._agents_to_prune("node-a", {"sensor"}, set())  # back again

        # The count restarts, so an agent that flickers is never pruned.
        assert main._agents_to_prune("node-a", set(), {"sensor"}) == []

    def test_an_agent_that_never_appeared_is_not_counted(self, main: MainActor) -> None:
        self._with_remote(main)

        for _ in range(VANISH_MISS_THRESHOLD + 2):
            assert main._agents_to_prune("node-a", set(), set()) == []

        # Absent from both this heartbeat and the last means it has not shown up
        # yet — a just-spawned agent whose first heartbeat is still in flight.
        assert main._node_agent_misses == {}

    def test_an_agent_on_another_node_is_left_alone(self, main: MainActor) -> None:
        self._with_remote(main, node="node-b")

        for _ in range(VANISH_MISS_THRESHOLD + 1):
            assert main._agents_to_prune("node-a", set(), {"sensor"}) == []

    def test_a_local_agent_is_left_alone(self, main: MainActor) -> None:
        main._save_to_spawn_registry(_cfg("local-one"))  # no node

        for _ in range(VANISH_MISS_THRESHOLD + 1):
            assert main._agents_to_prune("node-a", set(), {"local-one"}) == []

    def test_counts_are_kept_per_node(self, main: MainActor) -> None:
        self._with_remote(main, name="sensor", node="node-a")
        main._save_to_spawn_registry(_cfg("sensor-b", node="node-b"))

        for _ in range(VANISH_MISS_THRESHOLD - 1):
            main._agents_to_prune("node-a", set(), {"sensor"})

        # node-b's own heartbeat must not advance node-a's counter.
        assert main._agents_to_prune("node-b", set(), {"sensor-b"}) == []
        assert main._agents_to_prune("node-a", set(), {"sensor"}) == ["sensor"]


class TestDeletionNote:
    """The LLM reads its own history; a deleted agent has to be contradicted."""

    def test_it_names_the_agent_and_the_reason(self, main: MainActor) -> None:
        main._record_agent_deletion("weather", reason="user request")

        text = " ".join(m["content"] for m in main._conversation_history)
        assert "weather" in text
        assert "user request" in text

    def test_it_writes_both_sides_of_the_exchange(self, main: MainActor) -> None:
        main._record_agent_deletion("weather")

        roles = [m["role"] for m in main._conversation_history]
        # A user turn alone can be summarised away; the acknowledgement makes it
        # a completed exchange the model is less willing to contradict.
        assert roles == ["user", "assistant"]

    def test_it_tells_the_model_a_respawn_is_fresh(self, main: MainActor) -> None:
        main._record_agent_deletion("weather")

        # The whole point: the model has said "Spawned 'weather'" earlier in this
        # same history and will otherwise claim it still exists.
        assert "fresh spawn" in main._conversation_history[0]["content"]

    def test_it_is_persisted_with_the_rest_of_the_history(self, main: MainActor) -> None:
        main._record_agent_deletion("weather")

        assert len(main.recall("conversation_history") or []) == 2

    def test_a_persistence_failure_does_not_propagate(self, main: MainActor) -> None:
        def _boom(_key: str, _value: Any) -> None:
            raise RuntimeError("disk full")

        main.persist = _boom  # type: ignore[method-assign]

        # Called from the delete path; raising here would fail a delete that has
        # already happened.
        main._record_agent_deletion("weather")


class _Target:
    """A stand-in agent that records the lifecycle commands it is given."""

    def __init__(
        self,
        name: str = "weather",
        protected: bool = False,
        refuse: bool = False,
        state: ActorState = ActorState.RUNNING,
    ) -> None:
        self.name = name
        self.state = state
        self.actor_id = f"{name}-0001"
        self.protected = protected
        self._refuse = refuse
        self.commands: list[str] = []

    async def apply_command(self, command: str) -> bool:
        self.commands.append(command)
        return not self._refuse


class _Registry:
    def __init__(self, actor: _Target | None = None) -> None:
        self._actor = actor
        self.unregistered: list[str] = []

    def find_by_name(self, name: str) -> _Target | None:
        return self._actor if self._actor and self._actor.name == name else None

    def all_actors(self) -> list[_Target]:
        return [self._actor] if self._actor else []

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


class TestSlashCommands:
    """`process_user_input` answers these without reaching an LLM.

    That is the point of the direct intercepts: they work on an install with no
    API key, and they are the only route to these actions from chat.
    """

    async def test_help_lists_the_commands(self, main: MainActor) -> None:
        out = await main.process_user_input("/help")

        assert "/start <agent>" in out
        assert "/stop <agent>" in out

    async def test_help_describes_stop_as_reversible(self, main: MainActor) -> None:
        out = await main.process_user_input("/help")

        # It used to say "alias of /delete", which was never what the code did.
        assert "reversible" in out
        assert "alias of /delete" not in out

    async def test_agents_pause_pauses(self, main: MainActor) -> None:
        target = _Target()
        main._registry = _Registry(target)  # type: ignore[assignment]
        main._save_to_spawn_registry(_cfg("weather"))

        out = await main.process_user_input("/agents pause weather")

        # It ran stop() for this verb and reported the agent paused.
        assert target.commands == ["pause"]
        assert "paused" in out

    async def test_agents_stop_stops(self, main: MainActor) -> None:
        target = _Target()
        main._registry = _Registry(target)  # type: ignore[assignment]
        main._save_to_spawn_registry(_cfg("weather"))

        out = await main.process_user_input("/agents stop weather")

        assert target.commands == ["stop"]
        assert "stopped" in out

    async def test_a_refused_command_is_reported(self, main: MainActor) -> None:
        target = _Target(refuse=True)
        main._registry = _Registry(target)  # type: ignore[assignment]
        main._save_to_spawn_registry(_cfg("weather"))

        out = await main.process_user_input("/agents stop weather")

        # A protected agent refuses; claiming success would be the old bug in a
        # new place.
        assert "refused" in out.lower()

    async def test_an_unknown_agent_says_so(self, main: MainActor) -> None:
        main._registry = _Registry(None)  # type: ignore[assignment]

        out = await main.process_user_input("/agents stop nonexistent")

        assert "not found" in out.lower()

    async def test_a_bare_lifecycle_command_shows_its_usage(self, main: MainActor) -> None:
        # "/pause" alone used to fall through to the LLM: the input is stripped
        # before matching, so the "/pause " prefix never matched and the usage
        # hint written for this case was unreachable.
        for command in ("/pause", "/resume", "/start"):
            out = await main.process_user_input(command)
            assert "usage" in out.lower(), command
            assert "<agent-name>" in out

    async def test_the_pause_shortcut_pauses_too(self, main: MainActor) -> None:
        target = _Target()
        main._registry = _Registry(target)  # type: ignore[assignment]

        out = await main.process_user_input("/pause weather")

        # The two spellings must not disagree — they did.
        assert target.commands == ["pause"]
        assert "paused" in out

    async def test_start_is_refused_for_a_running_agent(self, main: MainActor) -> None:
        main._registry = _Registry(_Target())  # type: ignore[assignment]

        out = await main.process_user_input("/start weather")

        # Starting twice would add a second message loop and command listener.
        assert "not stopped" in out.lower()
