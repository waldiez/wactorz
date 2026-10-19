"""Where a spawn the model wrote runs: on main, or on the node made for it.

With ``WACTORZ_MODEL_CODE_NODE`` set, code the model writes goes to that node
when its spawn names none, so that a crafted email or web page cannot turn into
code holding every credential main has. A spawn that names main is the model
asking for those credentials; ``WACTORZ_MODEL_CODE_ON_MAIN`` decides whether the
operator is asked, and the answer is taken only from the dashboard in this
process. A refusal is remembered for a while, so a retry is not asked again.

Unset, nothing changes: every spawn runs where it did.
"""

import asyncio
import dataclasses
import json
from typing import Any, cast

import pytest

from wactorz.agents.main import placement
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.placement import (
    REFUSE,
    RUN_ON_MAIN,
    NotSpawned,
    PlacedOnNode,
    Placement,
    runs_model_code,
)
from wactorz.agents.main.spawns import SpawnService
from wactorz.agents.main.turn_actions import TurnActions
from wactorz.config import DeployTarget
from wactorz.core import refusals
from wactorz.core.outbound import Answer, answer_topic
from wactorz.web import answers, runtime

NODE = "sandbox"

#: How long a test waits for main to ask. Generous: it only bounds a failure.
ASKED_WITHIN_S = 10


def dynamic(name: str = "scraper", **over: Any) -> dict[str, Any]:
    return {"name": name, "type": "dynamic", "code": "async def setup(agent): pass", **over}


class _Host:
    """Main as placement sees it: a way to ask the operator, and to close the question."""

    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.questions: list[dict[str, Any]] = []
        self.published: list[tuple[str, Any]] = []
        self.asked = asyncio.Event()
        #: The nodes main has heard from, by name.
        self.heard: dict[str, dict[str, Any]] = {}

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        return self.heard

    # What the move to a sandbox reads; nothing here is moved (see test_rehoming.py).
    @property
    def _registry(self) -> Any:
        return None

    @property
    def migration(self) -> Migration:
        return cast("Migration", None)  # no move is made from these tests

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return {}

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        return None

    async def notify_user(self, text: str, *, options: Any = ()) -> str:
        self.questions.append({"text": text, "options": list(options)})
        self.asked.set()
        return f"q{len(self.questions)}"

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))


def _press(option: str, question_id: str = "q1") -> Answer:
    return Answer(id=question_id, agent_id="main-id", option=option, index=0, channel="dashboard")


@pytest.fixture(name="sandboxed")
def sandboxed_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model-written code goes to a node named `NODE`, which is a deploy target."""
    monkeypatch.setattr(placement, "MODEL_CODE_NODE", NODE)
    monkeypatch.setattr(placement, "MODEL_CODE_ON_MAIN", "ask")
    monkeypatch.setattr(
        placement,
        "CONFIG",
        dataclasses.replace(placement.CONFIG, deploy_targets=(DeployTarget(name=NODE),)),
    )


@pytest.fixture(name="host")
def host_fixture() -> _Host:
    return _Host()


class TestWhatCountsAsModelCode:
    @pytest.mark.parametrize(
        "config",
        [
            {"name": "a", "code": "x = 1"},
            {"name": "a", "type": "dynamic", "code": "x = 1"},
            {"name": "a", "type": "Dynamic", "code": "x = 1"},
        ],
    )
    def test_a_dynamic_agent_with_code(self, config: dict[str, Any]) -> None:
        assert runs_model_code(config)

    @pytest.mark.parametrize(
        "config",
        [
            {"name": "a", "type": "llm", "system_prompt": "be brief"},
            {"name": "a", "type": "llm", "code": "x = 1"},
            {"name": "a", "type": "rule", "trigger": {}},
            {"name": "a", "type": "module", "target": "pkg.mod:Agent"},
            {"name": "a", "type": "dynamic", "code": "   "},
        ],
    )
    def test_not_what_runs_main_s_own_classes_or_nothing(self, config: dict[str, Any]) -> None:
        assert not runs_model_code(config)


@pytest.fixture(name="unset")
def unset_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """WACTORZ_MODEL_CODE_NODE unset, on an install with no deploy targets."""
    monkeypatch.setattr(placement, "MODEL_CODE_NODE", "")
    monkeypatch.setattr(
        placement, "CONFIG", dataclasses.replace(placement.CONFIG, deploy_targets=())
    )


@pytest.mark.usefixtures("unset")
class TestUnset:
    async def test_without_a_sandbox_every_spawn_runs_where_it_did(self, host: _Host) -> None:
        for config in (dynamic(), dynamic(node="main"), dynamic(node="raspberrypi")):
            assert await Placement(host).place(config) == config
        assert host.questions == []

    async def test_a_sandbox_main_has_heard_from_is_used(self, host: _Host) -> None:
        # The compose stack's sandbox: a node main hears, not a deploy target.
        host.heard[placement.DEFAULT_NODE] = {"agents": []}

        placed = await Placement(host).place(dynamic())

        assert isinstance(placed, dict)
        assert placed["node"] == placement.DEFAULT_NODE

    async def test_a_sandbox_that_is_a_deploy_target_is_used(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # setup-local's, before it has reported since this start.
        monkeypatch.setattr(
            placement,
            "CONFIG",
            dataclasses.replace(
                placement.CONFIG, deploy_targets=(DeployTarget(name=placement.DEFAULT_NODE),)
            ),
        )

        placed = await Placement(host).place(dynamic())

        assert isinstance(placed, dict)
        assert placed["node"] == placement.DEFAULT_NODE

    async def test_main_keeps_the_code_on_main_though_there_is_a_sandbox(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "MODEL_CODE_NODE", "main")
        host.heard[placement.DEFAULT_NODE] = {"agents": []}

        config = dynamic(node="main")

        assert await Placement(host).place(dynamic()) == dynamic()
        assert await Placement(host).place(config) == config
        assert host.questions == []


@pytest.mark.usefixtures("sandboxed")
class TestOffMain:
    async def test_model_code_naming_no_node_goes_to_the_node_for_it(self, host: _Host) -> None:
        placed = await Placement(host).place(dynamic())

        assert isinstance(placed, dict)
        assert placed["node"] == NODE

    async def test_a_node_the_spawn_names_is_kept(self, host: _Host) -> None:
        config = dynamic(node="raspberrypi")

        assert await Placement(host).place(config) == config

    async def test_an_agent_that_runs_no_model_code_stays(self, host: _Host) -> None:
        config = {"name": "helper", "type": "llm", "system_prompt": "be brief"}

        assert await Placement(host).place(config) == config

    async def test_a_node_that_is_not_set_up_refuses_and_says_how(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Never back to main in silence: the setting says the code must not run there.
        monkeypatch.setattr(
            placement, "CONFIG", dataclasses.replace(placement.CONFIG, deploy_targets=())
        )

        placed = await Placement(host).place(dynamic())

        assert isinstance(placed, NotSpawned)
        assert "setup-local" in placed.reason
        assert f"/deploy {NODE}" in placed.reason
        assert "--profile sandbox" in placed.reason

    async def test_a_node_main_has_heard_from_will_do(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The compose stack's sandbox is a container, not a machine /deploy reaches.
        monkeypatch.setattr(
            placement, "CONFIG", dataclasses.replace(placement.CONFIG, deploy_targets=())
        )
        host.heard[NODE] = {"agents": []}

        placed = await Placement(host).place(dynamic())

        assert isinstance(placed, dict)
        assert placed["node"] == NODE


@pytest.mark.usefixtures("sandboxed")
class TestAskingForMain:
    async def test_allow_lets_it_run_on_main(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "MODEL_CODE_ON_MAIN", "allow")

        placed = await Placement(host).place(dynamic(node="main"))

        assert isinstance(placed, dict)
        assert "node" not in placed
        assert host.questions == []

    async def test_never_refuses_without_asking(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "MODEL_CODE_ON_MAIN", "never")

        placed = await Placement(host).place(dynamic(node="main"))

        assert isinstance(placed, NotSpawned)
        assert host.questions == []

    async def test_ask_waits_for_the_operator_s_yes(self, host: _Host) -> None:
        deciding = Placement(host)
        pending = asyncio.ensure_future(deciding.place(dynamic(node="main")))
        await asyncio.wait_for(host.asked.wait(), timeout=ASKED_WITHIN_S)

        assert not pending.done()
        assert host.questions[0]["options"] == [RUN_ON_MAIN, REFUSE]
        assert "'scraper'" in host.questions[0]["text"]

        await deciding.dashboard_answer(_press(RUN_ON_MAIN))
        placed = await pending

        assert isinstance(placed, dict)
        assert "node" not in placed

    async def test_a_refusal_is_not_asked_again_for_a_while(self, host: _Host) -> None:
        deciding = Placement(host)
        pending = asyncio.ensure_future(deciding.place(dynamic(node="main")))
        await asyncio.wait_for(host.asked.wait(), timeout=ASKED_WITHIN_S)
        await deciding.dashboard_answer(_press(REFUSE))

        first = await pending
        again = await deciding.place(dynamic(node="main"))

        assert isinstance(first, NotSpawned)
        assert isinstance(again, NotSpawned)
        assert "refused" in again.reason
        assert len(host.questions) == 1

    async def test_no_answer_in_time_is_a_no_and_closes_the_question(
        self, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "APPROVAL_TIMEOUT_S", 0.01)
        deciding = Placement(host)

        placed = await deciding.place(dynamic(node="main"))

        assert isinstance(placed, NotSpawned)
        assert host.published == [
            (answer_topic("main-id"), host.published[0][1]),
        ]
        assert host.published[0][1]["option"] == placement.NO_ANSWER
        # A press that comes afterwards lets nothing through.
        await deciding.dashboard_answer(_press(RUN_ON_MAIN))
        again = await deciding.place(dynamic(node="main"))
        assert isinstance(again, NotSpawned)
        assert "not answered" in again.reason

    async def test_a_press_for_a_question_it_did_not_ask_changes_nothing(self, host: _Host) -> None:
        deciding = Placement(host)
        pending = asyncio.ensure_future(deciding.place(dynamic(node="main")))
        await asyncio.wait_for(host.asked.wait(), timeout=ASKED_WITHIN_S)

        await deciding.dashboard_answer(_press(RUN_ON_MAIN, question_id="someone-elses"))

        assert not pending.done()
        await deciding.dashboard_answer(_press(REFUSE))
        assert isinstance(await pending, NotSpawned)


def _main() -> MainActor:
    """A MainActor with what placement and the spawn path touch, and no broker."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.actor_id = "main-id"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.spawns = SpawnService(main)
    main.placement = Placement(main)
    return main


@pytest.mark.usefixtures("sandboxed")
class TestOnlyTheDashboardMaySayYes:
    async def test_a_press_on_the_dashboard_reaches_main_in_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _main()
        asked = asyncio.Event()

        async def _notify(text: str, *, options: Any = ()) -> str:
            asked.set()
            return "q1"

        async def _publish(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(main, "notify_user", _notify)
        monkeypatch.setattr(runtime, "registry", type("R", (), {"get": lambda _s, _id: main})())
        monkeypatch.setattr(runtime, "mqtt_client_ref", None)
        monkeypatch.setattr(main, "_mqtt_publish", _publish)

        pending = asyncio.ensure_future(main.placement.place(dynamic(node="main")))
        await asyncio.wait_for(asked.wait(), timeout=ASKED_WITHIN_S)
        await answers.answer_from_dashboard(
            {"agent_id": "main-id", "id": "q1", "option": RUN_ON_MAIN, "index": 0}
        )

        placed = await pending
        assert isinstance(placed, dict)


class _Registry:
    def __init__(self, *running: str) -> None:
        self._by = {
            name: type("A", (), {"name": name, "actor_id": f"{name}-id"})() for name in running
        }

    def find_by_name(self, name: str) -> Any:
        return self._by.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._by.values())


def _spawning_main(
    monkeypatch: pytest.MonkeyPatch, *running: str
) -> tuple[MainActor, list[Any], list[str]]:
    """Main whose spawns are recorded, with the agents in ``running`` already here."""
    main = _main()
    setattr(main, "_registry", _Registry(*running))
    setattr(main, "_match_catalog_recipe", lambda name, capabilities=None: None)
    sent: list[Any] = []
    stopped: list[str] = []

    async def _spawn(config: dict[str, Any], save: bool = True, **_kw: Any) -> Any:
        sent.append(config)
        return PlacedOnNode(config["name"], config["node"], True) if config.get("node") else None

    async def _stop(existing: Any, name: str) -> None:
        stopped.append(name)

    monkeypatch.setattr(main.spawns, "_spawn_from_config", _spawn)
    monkeypatch.setattr(main, "_stop_for_replace", _stop)
    monkeypatch.setattr(main.spawns, "_remove_from_spawn_registry", lambda name: None)
    return main, sent, stopped


def _block(config: dict[str, Any]) -> str:
    return f"<spawn>{json.dumps(config)}</spawn>"


@pytest.mark.usefixtures("sandboxed")
class TestTheSpawnBlocks:
    async def test_a_block_without_a_node_goes_to_the_node_and_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main, sent, _stopped = _spawning_main(monkeypatch)

        _, spawned = await main.spawns._process_spawn_commands(_block(dynamic()))

        assert [c["node"] for c in sent] == [NODE]
        assert TurnActions(tuple(spawned)).summary("") == f"Spawned 'scraper' on node '{NODE}'"

    async def test_a_refused_block_spawns_nothing_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "MODEL_CODE_ON_MAIN", "never")
        main, sent, _stopped = _spawning_main(monkeypatch)

        _, spawned = await main.spawns._process_spawn_commands(_block(dynamic(node="main")))

        assert sent == []
        assert TurnActions(tuple(spawned)).summary("").startswith("Did not spawn 'scraper':")

    async def test_the_same_name_running_here_is_kept_rather_than_doubled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two agents answering to one name, one here and one on the node, is
        # what a spawn of a running name must never make.
        main, sent, _stopped = _spawning_main(monkeypatch, "scraper")

        _, spawned = await main.spawns._process_spawn_commands(_block(dynamic()))

        assert sent == []
        said = TurnActions(tuple(spawned)).summary("")
        assert said.startswith("'scraper' is already running here, so it was not spawned again")
        assert f"node '{NODE}'" in said
        assert "Spawned" not in said

    async def test_replace_moves_it_off_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        main, sent, stopped = _spawning_main(monkeypatch, "scraper")

        await main.spawns._process_spawn_commands(_block(dynamic(replace=True)))

        assert stopped == ["scraper"]
        assert [c["node"] for c in sent] == [NODE]


class TestWhatTheTurnSays:
    def test_a_node_not_heard_from_is_said_to_start_later(self) -> None:
        said = TurnActions((PlacedOnNode("scraper", NODE, running=False),)).summary("")

        assert "not running" in said
        assert f"/deploy {NODE}" in said

    def test_a_replacement_on_a_node_is_called_one(self) -> None:
        said = TurnActions((PlacedOnNode("scraper", NODE, running=True),)).summary(
            '"replace": true'
        )

        assert said.startswith("Replaced 'scraper' on node")


class TestRefusalsRemembered:
    def test_a_refusal_stands_for_a_while_then_lapses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1000.0]
        monkeypatch.setattr(refusals.time, "time", lambda: now[0])
        remembered = refusals.RecentRefusals(remembered_s=60)

        remembered.remember("scraper", refusals.REFUSED)
        now[0] += 59
        assert remembered.standing("scraper") is not None
        now[0] += 1
        assert remembered.standing("scraper") is None

    def test_it_says_how_long_ago(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(refusals.time, "time", lambda: now[0])
        remembered = refusals.RecentRefusals()
        remembered.remember("scraper", refusals.UNANSWERED)
        now[0] += 180

        standing = remembered.standing("scraper")

        assert standing is not None
        assert standing.said() == "not answered 3 minutes ago"

    def test_invented_names_do_not_grow_it_without_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(refusals, "MEMORY", 8)
        remembered = refusals.RecentRefusals()

        for i in range(50):
            remembered.remember(f"made-up-{i}", refusals.REFUSED)

        assert len(remembered._refusals) == 8
