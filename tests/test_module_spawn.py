"""`type: "module"`: an agent the deployment brings, spawned by its import path.

Only a target that is registered as a plugin is spawned. A spawn config can be
written by the model, and resolving any `package.module:attr` from one would
run whatever that path reached.
"""

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from wactorz import plugins
from wactorz.agents.function_agent import FunctionAgent, agent
from wactorz.agents.llm_agent import LLMProvider
from wactorz.agents.mixins.spawning import SpawnMixin
from wactorz.agents.rule_agent import RuleAgent
from wactorz.core.actor import ActorState

if TYPE_CHECKING:
    from wactorz.core.registry import ActorRegistry


@agent(subscribes="in/x")
def detector(reading: dict) -> dict:
    return reading


class _Registry:
    def __init__(self) -> None:
        self._by_name: dict[str, Any] = {}

    def find_by_name(self, name: str) -> Any:
        return self._by_name.get(name)


class _Host(SpawnMixin):
    """The Actor-base surface the mixin relies on, and a record of each spawn."""

    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "id-main"
        self.llm = cast(LLMProvider, object())
        self._registry = cast("ActorRegistry", _Registry())
        self._result_futures: dict[str, asyncio.Future] = {}
        self._persistence_dir = Path(tempfile.mkdtemp()) / "main"
        self.spawn_calls: list[tuple[Any, dict[str, Any]]] = []
        self.registered: list[dict[str, Any]] = []
        self.state = ActorState.RUNNING

    async def spawn(self, actor_class: Any, **kwargs: Any) -> Any:
        self.spawn_calls.append((actor_class, kwargs))
        return actor_class(**kwargs)

    def _register_spawn(self, config: dict[str, Any]) -> None:
        self.registered.append(config)

    async def _mqtt_publish(self, topic: str, payload: Any, **_kw: Any) -> None:
        return None

    async def send(self, target_id: str, msg_type: Any, payload: Any = None) -> bool:
        return True

    def persist(self, key: str, value: Any) -> None:
        return None

    def recall(self, key: str, default: Any = None) -> Any:
        return default

    def run_detached(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        return asyncio.create_task(coro, name=name)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Any:
    plugins.clear()
    yield
    plugins.clear()


class TestModuleSpawns:
    async def test_a_registered_target_is_spawned_with_its_options(self) -> None:
        plugins.register(detector, target="tests.test_module_spawn:detector")
        host = _Host()

        actor = await host._spawn_local_from_config(
            {
                "name": "imu-left",
                "type": "module",
                "target": "tests.test_module_spawn:detector",
                "options": {"threshold": 2},
            }
        )

        assert isinstance(actor, FunctionAgent) and actor.name == "imu-left"
        assert actor.options == {"threshold": 2}
        _factory, kwargs = host.spawn_calls[0]
        assert kwargs["persistence_dir"] == str(host._persistence_dir.parent)
        assert host.registered[0]["target"] == "tests.test_module_spawn:detector"

    async def test_an_unregistered_target_is_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        host = _Host()

        actor = await host._spawn_local_from_config(
            {"name": "rogue", "type": "module", "target": "subprocess:run"}
        )

        assert actor is None
        assert host.spawn_calls == []
        assert "subprocess:run" in caplog.text
        assert "WACTORZ_AGENTS" in caplog.text

    async def test_a_missing_target_is_refused(self) -> None:
        host = _Host()

        assert await host._spawn_local_from_config({"name": "x", "type": "module"}) is None


class TestRuleSpawns:
    async def test_a_rule_config_spawns_a_rule_agent(self) -> None:
        host = _Host()

        actor = await host._spawn_local_from_config(
            {
                "name": "imu-alert",
                "type": "rule",
                "triggers": ["anomalies/imu"],
                "conditions": [{"field": "score", "op": "gt", "value": 10}],
                "actions": [{"type": "publish", "topic": "alerts/imu"}],
            }
        )

        assert isinstance(actor, RuleAgent) and actor.name == "imu-alert"
        assert actor.config.triggers == ("anomalies/imu",)
        assert host.registered[0]["type"] == "rule"

    async def test_an_invalid_rule_is_refused_by_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        host = _Host()

        actor = await host._spawn_local_from_config(
            {"name": "bad", "type": "rule", "triggers": "a/b"}
        )

        assert actor is None
        assert "at least one action" in caplog.text
