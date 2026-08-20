"""The streaming turn — what the CLI sees as an answer is produced.

Same decisions as the non-streaming path, delivered differently: text arrives in
pieces as the model writes it, and a final dict carries what happened. Anything
resolved without the model — a command, an actuation, a pipeline plan — has
nothing to stream, so it yields one piece and the dict.

The dict is the part other code depends on. `done` marks the end, `spawned`
carries the agents that now exist, and `system_msg` is what actually happened,
which the caller may show even though it was already yielded as text.

Order is the contract here as much as content. Notifications come before the
answer, delegation results after the block that asked for them, and the dict
last — a consumer that stops at the first dict must not stop early.
"""

from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.delegation import DelegationManager
from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService
from wactorz.agents.mixins.spawning import SpawnPlaceholder


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


class _Registry:
    def __init__(self, running: tuple[str, ...] = ()) -> None:
        self._by = {n: _Agent(n) for n in running}

    def find_by_name(self, name: str) -> _Agent | None:
        return self._by.get(name)

    def all_actors(self) -> list[_Agent]:
        return list(self._by.values())


class _Main:
    """A MainActor whose collaborators are stubbed to a fixed script."""

    def __init__(
        self,
        *,
        notification: str = "",
        plan_reply: str | None = None,
        collision: str | None = None,
        intent: str = "OTHER",
        chunks: tuple[str, ...] = ("hello ", "world"),
        spawned: tuple[Any, ...] = (),
        deleted: tuple[str, ...] = (),
        missing: tuple[str, ...] = (),
        delegate_results: tuple[str, ...] = (),
        mention_result: str | None = None,
        planner_reply: str = "planned",
        ha_result: dict[str, Any] | None = None,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        main.delegation = DelegationManager(main)
        main.lifecycle = LifecycleService(main)
        main._conversation_history = []
        main._agent_manifests = {}
        setattr(main, "_registry", _Registry())

        self.log: dict[str, Any] = {"recorded": [], "published": [], "attachments": []}
        self._mention_result = mention_result

        main._drain_notifications = lambda: notification
        setattr(main, "_warn_if_pending_plan_collision", lambda _t: collision)
        setattr(main, "_rebuild_system_prompt", lambda: None)
        setattr(main, "_prefix_with_live_context", lambda t: f"[live] {t}")
        setattr(main, "persist", lambda _k, _v: None)
        self._wire_the_decisions(main, plan_reply, intent, planner_reply, ha_result)
        self._wire_the_answer(main, chunks, spawned, deleted, missing, delegate_results)
        self.actor = main

    def _wire_the_decisions(
        self,
        main: MainActor,
        plan_reply: str | None,
        intent: str,
        planner_reply: str,
        ha_result: dict[str, Any] | None,
    ) -> None:
        """Everything consulted before the model is reached."""

        async def _plan(_text: str) -> str | None:
            return plan_reply

        async def _record(text: str, reply: str) -> None:
            self.log["recorded"].append((text, reply))

        async def _classify(_text: str) -> str:
            return intent

        async def _planner(_text: str) -> str:
            return planner_reply

        async def _pipeline(_text: str) -> str:
            return "pipeline done"

        async def _actuate(_text: str, **_kw: Any) -> str:
            return "switch flipped"

        async def _delegate_task(name: str, _task: str, timeout: float = 60.0) -> Any:
            return ha_result if name == "home-assistant-agent" else None

        setattr(main, "_handle_pending_plan_response", _plan)
        setattr(main, "_record_external_exchange", _record)
        setattr(main, "_classify_intent", _classify)
        setattr(main, "_run_planner", _planner)
        setattr(main, "_propose_or_execute_pipeline", _pipeline)
        setattr(main, "_handle_actuate_intent", _actuate)
        setattr(main.delegation, "delegate_task", _delegate_task)

    def _wire_the_answer(
        self,
        main: MainActor,
        chunks: tuple[str, ...],
        spawned: tuple[Any, ...],
        deleted: tuple[str, ...],
        missing: tuple[str, ...],
        delegate_results: tuple[str, ...],
    ) -> None:
        """The model, and everything that acts on what it wrote."""

        async def _chat_stream(_text: str, attachments: Any = None) -> Any:
            self.log["attachments"].append(attachments)
            for chunk in chunks:
                yield chunk

        async def _spawn_blocks(response: str) -> tuple[str, list[Any]]:
            return response, list(spawned)

        async def _delete_blocks(response: str) -> tuple[str, list[str], list[str]]:
            return response, list(deleted), list(missing)

        async def _delegate_blocks(response: str, restricted: bool = False) -> Any:
            return response, list(delegate_results)

        async def _mentions(response: str) -> str:
            return self._mention_result if self._mention_result is not None else response

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.log["published"].append((topic, payload))

        setattr(main, "chat_stream", _chat_stream)
        setattr(main.spawns, "_process_spawn_commands", _spawn_blocks)
        setattr(main.lifecycle, "_process_delete_commands", _delete_blocks)
        setattr(main.delegation, "_process_delegate_commands", _delegate_blocks)
        setattr(main.delegation, "_execute_llm_delegations", _mentions)
        setattr(main, "_mqtt_publish", _publish)

    async def run(self, text: str, attachments: Any = None) -> list[Any]:
        """Everything the turn yields, in order."""
        return [c async for c in self.actor.process_user_input_stream(text, attachments)]


def texts(yielded: list[Any]) -> list[str]:
    return [c for c in yielded if isinstance(c, str)]


def final(yielded: list[Any]) -> dict[str, Any]:
    """The closing dict, which is what a consumer waits for."""
    dicts = [c for c in yielded if isinstance(c, dict)]
    assert len(dicts) == 1, f"expected exactly one closing dict, got {len(dicts)}"
    return dicts[0]


class TestTheShapeOfAStream:
    """Every path ends the same way, whatever it did."""

    async def test_it_ends_with_a_done_dict(self) -> None:
        yielded = await _Main().run("hello")

        assert final(yielded)["done"] is True

    async def test_the_dict_comes_last(self) -> None:
        # A consumer that stops reading at the first dict would otherwise cut
        # the answer short.
        yielded = await _Main().run("hello")

        assert isinstance(yielded[-1], dict)

    async def test_the_model_text_arrives_in_pieces(self) -> None:
        yielded = await _Main(chunks=("one ", "two ", "three")).run("hello")

        assert texts(yielded) == ["one ", "two ", "three"]

    async def test_a_notification_comes_before_the_answer(self) -> None:
        yielded = await _Main(notification="[!] node down\n").run("hello")

        assert texts(yielded)[0] == "[!] node down\n"

    async def test_no_notification_yields_nothing_extra(self) -> None:
        yielded = await _Main(chunks=("hi",)).run("hello")

        assert texts(yielded) == ["hi"]


class TestTurnsThatNeverReachTheModel:
    """Resolved before the model, so there is nothing to stream."""

    async def test_a_pending_plan_answer_is_delivered_whole(self) -> None:
        main = _Main(plan_reply="Approved. Spawning 2 agents.")

        yielded = await main.run("yes")

        assert texts(yielded) == ["Approved. Spawning 2 agents."]
        assert main.log["recorded"] == [("yes", "Approved. Spawning 2 agents.")]

    async def test_a_collision_warning_is_delivered_whole(self) -> None:
        yielded = await _Main(collision="You have a plan pending.").run("make me an agent")

        assert texts(yielded) == ["You have a plan pending."]

    @pytest.mark.parametrize("text", ["/help", "@weather what now", "rules"])
    async def test_a_command_is_answered_by_the_non_streaming_path(self, text: str) -> None:
        # One implementation for both surfaces: routing here rather than
        # duplicating the command chain is what stops the two drifting.
        main = _Main()
        answers = []

        async def _plain(t: str) -> str:
            answers.append(t)
            return "command answer"

        setattr(main.actor, "process_user_input", _plain)

        yielded = await main.run(text)

        assert answers == [text]
        assert texts(yielded) == ["command answer"]

    async def test_deploy_streams_its_own_progress(self) -> None:
        # The one command that has to report as it goes, rather than after.
        main = _Main()

        async def _deploy(_stripped: str) -> Any:
            yield "[deploy] connecting"
            yield "[deploy] done"

        setattr(main.actor, "_slash_deploy_stream", _deploy)

        yielded = await main.run("/deploy rpi")

        assert texts(yielded) == ["[deploy] connecting", "[deploy] done"]

    async def test_a_planner_prefix_skips_the_classifier(self) -> None:
        main = _Main(planner_reply="Here is the pipeline.")

        yielded = await main.run("pipeline: watch the door")

        assert texts(yielded) == ["Here is the pipeline."]

    @pytest.mark.parametrize(
        ("intent", "expected"),
        [("PIPELINE", "pipeline done"), ("ACTUATE", "switch flipped")],
    )
    async def test_an_intent_is_answered_without_streaming(
        self, intent: str, expected: str
    ) -> None:
        yielded = await _Main(intent=intent).run("turn on the light")

        assert texts(yielded) == [expected]

    async def test_a_home_assistant_question_is_delegated(self) -> None:
        yielded = await _Main(intent="HA", ha_result={"result": "21 degrees"}).run("how warm is it")

        assert texts(yielded) == ["21 degrees"]

    async def test_an_unreachable_home_assistant_agent_says_so(self) -> None:
        yielded = await _Main(intent="HA", ha_result=None).run("how warm is it")

        assert "could not reach" in texts(yielded)[0]


class TestWhatTheModelWroteBeingActedOn:
    """Blocks in the finished response, and what the caller is told."""

    async def test_spawned_agents_reach_the_closing_dict(self) -> None:
        yielded = await _Main(spawned=(_Agent("kettle-watcher"),)).run("make me an agent")

        assert [a.name for a in final(yielded)["spawned"]] == ["kettle-watcher"]

    async def test_a_spawn_is_announced_in_the_summary(self) -> None:
        yielded = await _Main(spawned=(_Agent("kettle-watcher"),)).run("make me an agent")

        assert "Spawned 'kettle-watcher'" in final(yielded)["system_msg"]

    async def test_an_agent_still_installing_is_announced_as_such(self) -> None:
        # It will appear once its packages are in. Saying nothing about it reads
        # as the spawn having failed, and the user spawns it again.
        yielded = await _Main(spawned=(SpawnPlaceholder("chart-maker"),)).run("make me an agent")

        summary = final(yielded)["system_msg"]
        assert "Installing packages for 'chart-maker'" in summary
        assert "auto-restore" not in summary

    async def test_a_running_agent_and_an_installing_one_are_told_apart(self) -> None:
        yielded = await _Main(
            spawned=(_Agent("kettle-watcher"), SpawnPlaceholder("chart-maker"))
        ).run("make me two agents")

        summary = final(yielded)["system_msg"]
        assert "Spawned 'kettle-watcher'" in summary
        assert "Installing packages for 'chart-maker'" in summary

    async def test_a_deletion_is_announced(self) -> None:
        yielded = await _Main(deleted=("old-agent",)).run("delete it")

        assert "Deleted old-agent" in final(yielded)["system_msg"]

    async def test_a_deletion_that_found_nothing_is_announced_too(self) -> None:
        # Silence would read as having deleted it.
        yielded = await _Main(missing=("ghost",)).run("delete ghost")

        assert "Could not delete ghost" in final(yielded)["system_msg"]

    async def test_the_summary_is_also_yielded_as_text(self) -> None:
        # For consumers that render the stream and ignore the closing dict.
        yielded = await _Main(deleted=("old-agent",)).run("delete it")

        assert any("Deleted old-agent" in t for t in texts(yielded))

    async def test_nothing_happened_means_an_empty_summary(self) -> None:
        yielded = await _Main().run("hello")

        assert final(yielded)["system_msg"] == ""

    async def test_a_delegation_result_is_appended_after_the_answer(self) -> None:
        # The block itself was already streamed, so the result follows it rather
        # than replacing it.
        yielded = await _Main(
            chunks=("asking weather",), delegate_results=("✅ weather: 21C",)
        ).run("what is the weather")

        assert texts(yielded) == ["asking weather", "\n✅ weather: 21C"]

    async def test_a_mention_delegation_result_is_appended(self) -> None:
        main = _Main(chunks=("@weather check",), mention_result="@weather check\n✅ weather: 21C")

        yielded = await main.run("what is the weather")

        assert any("✅ weather: 21C" in t for t in texts(yielded))

    async def test_an_unchanged_response_appends_nothing(self) -> None:
        yielded = await _Main(chunks=("just talking",)).run("hello")

        assert texts(yielded) == ["just talking"]


class TestTheHistoryItLeaves:
    """The model is given live context; history keeps what the user typed."""

    async def test_the_model_sees_the_prefixed_text(self) -> None:
        main = _Main()
        seen = []
        original = main.actor.chat_stream

        async def _spy(text: str, attachments: Any = None) -> Any:
            seen.append(text)
            async for chunk in original(text, attachments):
                yield chunk

        setattr(main.actor, "chat_stream", _spy)

        await main.run("what agents exist")

        assert seen == ["[live] what agents exist"]

    async def test_history_keeps_what_the_user_typed(self) -> None:
        # Left prefixed, every turn would carry the agent list of the moment it
        # was sent, and the context window would fill with stale copies.
        main = _Main()
        main.actor._conversation_history = [
            {"role": "user", "content": "[live] what agents exist"},
            {"role": "assistant", "content": "these ones"},
        ]

        await main.run("what agents exist")

        assert main.actor._conversation_history[0]["content"] == "what agents exist"

    async def test_only_this_turn_is_rewritten(self) -> None:
        # The same question asked twice must not have its earlier answer's
        # question rewritten along with this one.
        main = _Main()
        main.actor._conversation_history = [
            {"role": "user", "content": "[live] what agents exist"},
            {"role": "assistant", "content": "an older answer"},
            {"role": "user", "content": "[live] what agents exist"},
        ]

        await main.run("what agents exist")

        contents = [m["content"] for m in main.actor._conversation_history]
        assert contents == [
            "[live] what agents exist",
            "an older answer",
            "what agents exist",
        ]

    async def test_the_assistant_turn_is_left_alone(self) -> None:
        main = _Main()
        main.actor._conversation_history = [
            {"role": "assistant", "content": "[live] what agents exist"},
        ]

        await main.run("what agents exist")

        assert main.actor._conversation_history[0]["content"] == "[live] what agents exist"

    async def test_attachments_reach_the_model(self) -> None:
        main = _Main()

        await main.run("what is this", [{"type": "image", "data": "..."}])

        assert main.log["attachments"] == [[{"type": "image", "data": "..."}]]

    async def test_the_turn_is_logged_to_the_broker(self) -> None:
        main = _Main()

        await main.run("hello")

        topics = [t for t, _ in main.log["published"]]
        assert "agents/main-id/logs" in topics
