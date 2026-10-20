"""Agents the model wrote before there was a sandbox are moved there.

The first time the node model-written code goes to reports after a start, main
moves the model-written agents still running in its own process there, as
``/migrate`` would, and says in chat what moved. One that reads the history
without ``await`` stays, since only main answers that call that way, and so does
one whose move is refused; chat says why for each. It happens once per start,
whatever the number of heartbeats.
"""

import asyncio
import dataclasses
from typing import Any, cast

import pytest

from wactorz.agents.main import placement
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.placement import Placement, stays_on_main
from wactorz.config import DeployTarget

NODE = "sandbox"

PLAIN = "async def handle_task(agent, payload):\n    return {'ok': True}\n"
READS_AWAITED = "async def handle_task(agent, payload):\n    return await agent.query_ts(hours=1)\n"
READS_BARE = "async def handle_task(agent, payload):\n    return len(agent.query_ts(hours=1))\n"


def dynamic(name: str, code: str = PLAIN, **over: Any) -> dict[str, Any]:
    return {"name": name, "type": "dynamic", "code": code, **over}


class _Registry:
    def __init__(self, *running: str) -> None:
        self.running = set(running)

    def find_by_name(self, name: str) -> Any:
        return name if name in self.running else None


class _Migration:
    """Moves what it is asked to, unless told to refuse one."""

    def __init__(self, refuse: dict[str, str] | None = None) -> None:
        self.moved: list[tuple[str, str]] = []
        self.refuse = refuse or {}

    async def migrate_agent(self, name: str, node: str, **_kw: Any) -> dict[str, Any]:
        if name in self.refuse:
            return {"success": False, "message": self.refuse[name]}
        self.moved.append((name, node))
        return {"success": True, "message": "moved"}


class _Host:
    """Main as placement sees it, with a spawn registry and a way to move agents."""

    def __init__(
        self, registry: dict[str, dict[str, Any]], running: tuple[str, ...], mover: _Migration
    ) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.heard: dict[str, dict[str, Any]] = {NODE: {"agents": []}}
        self.spawn_registry = registry
        self.actors = _Registry(*running)
        self.mover = mover
        self.notices: list[dict[str, Any]] = []

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        return self.heard

    @property
    def _registry(self) -> Any:
        return self.actors

    @property
    def migration(self) -> Migration:
        return cast("Migration", self.mover)  # a fake standing in for main's

    async def notify_user(self, text: str, *, options: Any = ()) -> str:
        return "q1"

    async def _mqtt_publish(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.spawn_registry

    def _node_refuses_tasks(self, node_name: str) -> str | None:
        return None

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.notices.append(notice)


@pytest.fixture(autouse=True)
def _sandbox_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """WACTORZ_MODEL_CODE_NODE unset, on an install with no deploy targets."""
    monkeypatch.setattr(placement, "MODEL_CODE_NODE", "")
    monkeypatch.setattr(
        placement, "CONFIG", dataclasses.replace(placement.CONFIG, deploy_targets=())
    )


class TestWhatCannotRunOnANode:
    def test_a_history_read_without_await(self) -> None:
        assert "query_ts() without await" in (stays_on_main(READS_BARE) or "")

    def test_the_counts_too(self) -> None:
        assert stays_on_main("def f(agent):\n    return agent.ts_stats()\n") is not None

    def test_one_awaited_runs_anywhere(self) -> None:
        assert stays_on_main(READS_AWAITED) is None

    def test_a_program_that_reads_no_history(self) -> None:
        assert stays_on_main(PLAIN) is None

    def test_one_that_cannot_be_read_stays(self) -> None:
        assert stays_on_main("def (") is not None


class TestMoving:
    async def test_the_model_s_agents_on_main_go_to_the_node(self) -> None:
        mover = _Migration()
        host = _Host(
            {"scraper": dynamic("scraper"), "reader": dynamic("reader", READS_AWAITED)},
            ("scraper", "reader"),
            mover,
        )

        await Placement(host).rehome(NODE)

        assert sorted(mover.moved) == [("reader", NODE), ("scraper", NODE)]
        assert "Moved to node 'sandbox'" in host.notices[0]["message"]

    @pytest.mark.parametrize(
        ("config", "running"),
        [
            (dynamic("far", node="raspberrypi"), True),  # already on a node
            (dynamic("weather", trusted=True), True),  # the catalogue's own code
            ({"name": "helper", "type": "llm", "system_prompt": "be brief"}, True),
            (dynamic("gone"), False),  # in the registry, not running here
        ],
    )
    async def test_what_is_not_model_code_on_main_is_left(
        self, config: dict[str, Any], running: bool
    ) -> None:
        mover = _Migration()
        name = str(config["name"])
        host = _Host({name: config}, (name,) if running else (), mover)

        await Placement(host).rehome(NODE)

        assert mover.moved == []
        assert host.notices == []

    async def test_one_that_reads_history_bare_stays_and_is_named(self) -> None:
        mover = _Migration()
        host = _Host({"trends": dynamic("trends", READS_BARE)}, ("trends",), mover)

        await Placement(host).rehome(NODE)

        assert mover.moved == []
        (notice,) = host.notices
        assert notice["severity"] == "warning"
        assert "Kept 'trends' on main: it calls query_ts() without await" in notice["message"]

    async def test_one_whose_move_is_refused_stays_and_says_why(self) -> None:
        mover = _Migration(refuse={"big": "its state is over the limit"})
        host = _Host({"big": dynamic("big")}, ("big",), mover)

        await Placement(host).rehome(NODE)

        assert "Kept 'big' on main: its state is over the limit" in host.notices[0]["message"]


class TestOncePerStart:
    def test_only_for_the_node_model_code_goes_to(self) -> None:
        deciding = Placement(_Host({}, (), _Migration()))

        assert deciding.claim_rehoming("raspberrypi") is False
        assert deciding.claim_rehoming(NODE) is True
        assert deciding.claim_rehoming(NODE) is False

    def test_not_while_the_code_is_kept_on_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(placement, "MODEL_CODE_NODE", "main")

        assert Placement(_Host({}, (), _Migration())).claim_rehoming(NODE) is False

    async def test_main_starts_one_move_whatever_the_heartbeats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mover = _Migration()
        host = _Host({"scraper": dynamic("scraper")}, ("scraper",), mover)
        main = MainActor.__new__(MainActor)
        main.placement = Placement(host)
        main._tasks = []

        for _ in range(3):
            main.node_reported(NODE)
        await asyncio.gather(*main._tasks)

        assert len(main._tasks) == 1
        assert mover.moved == [("scraper", NODE)]

    def test_a_sandbox_named_as_a_deploy_target_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            placement,
            "CONFIG",
            dataclasses.replace(placement.CONFIG, deploy_targets=(DeployTarget(name=NODE),)),
        )
        host = _Host({}, (), _Migration())
        host.heard = {}

        assert Placement(host).claim_rehoming(NODE) is True
