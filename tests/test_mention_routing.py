"""Typing `@agent do the thing` and getting that agent's answer back.

The direct form: the user names an agent and main hands the task straight to it,
with no model in between. Three places the agent might be, tried in the order of
how cheaply they can be reached — running here, spawnable from the catalogue, or
running on another machine — and a different reply shape for each, because
"which machine answered" is something the reader wants to know.

An agent that is nowhere is answered rather than ignored. Naming the remote
agents that do exist turns "not found" into something the user can act on, since
the usual cause is a name typed from memory.

Distinct from the `@mentions` a model writes into its own reply, which are
guessed at rather than typed and are handled in `_execute_llm_delegations`.
"""

import asyncio
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.delegation import DelegationManager
from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService

_RECIPE_FAILED = "recipe exploded"


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


class _Catalogue:
    """The catalogue agent, which can bring a recipe up on demand."""

    def __init__(self, *, result: dict[str, Any] | None = None, raises: bool = False) -> None:
        self.name = "catalog"
        self.spawned: list[str] = []
        self._result = result
        self._raises = raises

    async def _action_spawn(self, name: str, _options: dict[str, Any]) -> dict[str, Any]:
        self.spawned.append(name)
        if self._raises:
            raise RuntimeError(_RECIPE_FAILED)
        return self._result if self._result is not None else {"ok": True}


class _Registry:
    def __init__(
        self,
        running: tuple[str, ...] = (),
        catalogue: _Catalogue | None = None,
        appears_after_spawn: str | None = None,
    ) -> None:
        self._by: dict[str, Any] = {n: _Agent(n) for n in running}
        if catalogue is not None:
            self._by["catalog"] = catalogue
        self._catalogue = catalogue
        self._appears = appears_after_spawn

    def find_by_name(self, name: str) -> Any:
        if (
            self._appears
            and name == self._appears
            and self._catalogue is not None
            and self._catalogue.spawned
        ):
            return _Agent(name)
        return self._by.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._by.values())


class _ReplyTopic:
    """Stands in for the subscribed reply channel a remote task answers on."""

    def __init__(self, answer: Any, times_out: bool) -> None:
        self._answer = answer
        self._times_out = times_out
        self.opened = 0

    def __call__(self) -> "_ReplyTopic":
        self.opened += 1
        return self

    async def __aenter__(self) -> tuple[str, Any]:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        if not self._times_out:
            future.set_result(self._answer)
        return "main/reply/main-id/abcd", future

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _Main:
    """A MainActor with the surface an `@mention` turn touches."""

    def __init__(
        self,
        *,
        running: tuple[str, ...] = (),
        manifests: dict[str, dict[str, Any]] | None = None,
        catalogue: _Catalogue | None = None,
        appears_after_spawn: str | None = None,
        nodes: dict[str, dict[str, Any]] | None = None,
        local_result: Any = None,
        remote_answer: Any = None,
        remote_times_out: bool = False,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        main.delegation = DelegationManager(main)
        main.lifecycle = LifecycleService(main)
        main._agent_manifests = dict(manifests or {})
        main._known_nodes = dict(nodes or {})
        main._conversation_history = []
        setattr(main, "_registry", _Registry(running, catalogue, appears_after_spawn))

        self.delegated: list[tuple[str, str]] = []
        self.published: list[tuple[str, Any]] = []
        self.reply_topic = _ReplyTopic(remote_answer, remote_times_out)

        async def _delegate(name: str, task: str, timeout: float = 60.0) -> Any:
            self.delegated.append((name, task))
            return local_result

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.published.append((topic, payload))

        main._drain_notifications = lambda: ""
        setattr(main.delegation, "delegate_task", _delegate)
        setattr(main.delegation, "_reply_topic", self.reply_topic)
        setattr(main, "_mqtt_publish", _publish)
        self.actor = main

    async def ask(self, text: str) -> str:
        return await self.actor.process_user_input(text)


def node(*agents: str) -> dict[str, Any]:
    return {"agents": list(agents), "last_seen": 0}


def recipe(**over: Any) -> dict[str, Any]:
    return {"spawnable": True, "catalog": "catalog", **over}


class TestReadingWhatWasTyped:
    """The name and the task, out of one line."""

    async def test_the_task_is_everything_after_the_name(self) -> None:
        main = _Main(running=("weather",), local_result={"result": "21C"})

        await main.ask("@weather what is it like in Athens")

        assert main.delegated == [("weather", "what is it like in Athens")]

    @pytest.mark.parametrize("written", ["@weather:", "@weather,"])
    async def test_punctuation_after_the_name_is_not_part_of_it(self, written: str) -> None:
        # Written the way a person writes it, rather than the way a parser
        # would like it.
        main = _Main(running=("weather",), local_result={"result": "21C"})

        await main.ask(f"{written} how warm")

        assert main.delegated[0][0] == "weather"

    async def test_a_bare_mention_sends_the_whole_line(self) -> None:
        # Nothing follows the name, so there is no task to separate out; the
        # agent is given what was typed rather than an empty string.
        main = _Main(running=("weather",), local_result={"result": "21C"})

        await main.ask("@weather")

        assert main.delegated == [("weather", "@weather")]


class TestAnAgentRunningHere:
    """The cheapest answer: it is already in this process."""

    async def test_its_answer_is_labelled_with_its_name(self) -> None:
        main = _Main(running=("weather",), local_result={"result": "21C"})

        answer = await main.ask("@weather how warm")

        assert answer == "**weather**: 21C"

    @pytest.mark.parametrize("field", ["result", "response"])
    async def test_either_reply_field_is_read(self, field: str) -> None:
        main = _Main(running=("weather",), local_result={field: "21C"})

        assert "21C" in await main.ask("@weather how warm")

    async def test_a_reply_in_neither_field_is_still_shown(self) -> None:
        # Better a raw dict than silence: the agent did answer.
        main = _Main(running=("weather",), local_result={"temperature": 21})

        assert "21" in await main.ask("@weather how warm")

    async def test_an_agent_that_says_nothing_is_reported(self) -> None:
        main = _Main(running=("weather",), local_result=None)

        assert await main.ask("@weather how warm") == "weather did not respond."


class TestAnAgentThatIsNotRunningYet:
    """A catalogue recipe is brought up rather than refused."""

    async def test_a_spawnable_name_is_spawned_and_then_asked(self) -> None:
        catalogue = _Catalogue()
        main = _Main(
            manifests={"weather": recipe()},
            catalogue=catalogue,
            appears_after_spawn="weather",
            local_result={"result": "21C"},
        )

        answer = await main.ask("@weather how warm")

        assert catalogue.spawned == ["weather"]
        assert answer == "**weather**: 21C"

    async def test_a_name_the_catalogue_refuses_says_why(self) -> None:
        catalogue = _Catalogue(result={"ok": False, "message": "no recipe"})
        main = _Main(manifests={"weather": recipe()}, catalogue=catalogue)

        answer = await main.ask("@weather how warm")

        assert "Could not spawn 'weather'" in answer
        assert "no recipe" in answer

    async def test_a_recipe_that_raises_does_not_take_the_turn_down(self) -> None:
        catalogue = _Catalogue(raises=True)
        main = _Main(manifests={"weather": recipe()}, catalogue=catalogue)

        answer = await main.ask("@weather how warm")

        assert "Could not spawn 'weather'" in answer
        assert _RECIPE_FAILED in answer

    async def test_a_manifest_that_is_not_spawnable_is_not_spawned(self) -> None:
        catalogue = _Catalogue()
        main = _Main(manifests={"weather": recipe(spawnable=False)}, catalogue=catalogue)

        await main.ask("@weather how warm")

        assert not catalogue.spawned


class TestAnAgentOnAnotherMachine:
    """Reached over the broker, and the reply says which machine answered."""

    async def test_the_task_goes_to_the_agents_own_topic(self) -> None:
        main = _Main(nodes={"rpi": node("sensor")}, remote_answer={"result": "on"})

        await main.ask("@sensor status")

        topic, payload = main.published[0]
        assert topic == "agents/by-name/sensor/task"
        assert payload["_remote_task"] is True
        assert payload["text"] == "status"

    async def test_the_reply_names_the_node(self) -> None:
        # Which machine answered is worth knowing when the same agent name can
        # exist in more than one place.
        main = _Main(nodes={"rpi": node("sensor")}, remote_answer={"result": "on"})

        assert await main.ask("@sensor status") == "**sensor** (on rpi): on"

    async def test_the_reply_channel_is_opened_before_publishing(self) -> None:
        # Publishing first loses the answer of a node quick enough to reply
        # before anything is listening.
        main = _Main(nodes={"rpi": node("sensor")}, remote_answer={"result": "on"})

        await main.ask("@sensor status")

        assert main.reply_topic.opened == 1

    async def test_a_node_that_does_not_answer_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The wait is shortened rather than waited out: the behaviour under
        # test is what the user is told, not how long it took to say it.
        main = _Main(nodes={"rpi": node("sensor")}, remote_times_out=True)
        real_wait_for = asyncio.wait_for

        async def _immediate(awaitable: Any, timeout: float) -> Any:
            return await real_wait_for(awaitable, timeout=0.01)

        monkeypatch.setattr(asyncio, "wait_for", _immediate)

        answer = await main.ask("@sensor status")

        assert "did not respond within 30s" in answer

    async def test_a_local_agent_wins_over_the_same_name_on_a_node(self) -> None:
        main = _Main(
            running=("sensor",), nodes={"rpi": node("sensor")}, local_result={"result": "local"}
        )

        await main.ask("@sensor status")

        assert not main.published


class TestAnAgentThatIsNowhere:
    """Answered, and told what does exist."""

    async def test_an_unknown_name_is_reported(self) -> None:
        main = _Main()

        assert await main.ask("@ghost hello") == "Agent 'ghost' not found."

    async def test_the_remote_agents_that_do_exist_are_named(self) -> None:
        # The usual cause is a name typed from memory, so listing the real ones
        # is what makes the answer useful.
        main = _Main(nodes={"rpi": node("sensor", "camera")})

        answer = await main.ask("@ghost hello")

        assert "sensor" in answer
        assert "camera" in answer
