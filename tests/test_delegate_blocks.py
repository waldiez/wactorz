"""Delegations the model writes into its own reply.

Two forms reach an agent. A `<delegate>` block is the unambiguous one: valid
JSON, a named agent, a structured payload. An `@mention` is what the model
writes anyway, and it is looser — sometimes carrying JSON, sometimes just the
rest of the sentence.

Which `@mention`s count is the delicate part. One at the start of a line or
sentence is an instruction; one buried mid-sentence is the model telling the
*user* about an agent ("you can use @weather-agent to check"), and dispatching
that would both run something nobody asked for and rewrite the sentence
explaining it. A bare mention of a name nothing knows is left exactly as
written, because it is far more likely to be prose than intent.

The restricted path is the one with teeth: on a social channel only
allow-listed agents can be reached, and nothing may be spawned to satisfy a
delegation. That is enforced on the name before anything is resolved.
"""

import json
from typing import Any

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.nodes import NodeManager

_MAILBOX_CLOSED = "mailbox closed"


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


class _Registry:
    def __init__(self, running: tuple[str, ...] = ()) -> None:
        self._by = {n: _Agent(n) for n in running}

    def find_by_name(self, name: str) -> _Agent | None:
        return self._by.get(name)


class _Main:
    """A MainActor with the delegation surface stubbed.

    `resolves` maps the name asked for to the agent that answers, which is how
    a fuzzy alias reaches the recipe it meant.
    """

    def __init__(
        self,
        *,
        running: tuple[str, ...] = (),
        resolves: dict[str, str] | None = None,
        spawnable: tuple[str, ...] = (),
        auto_spawns: tuple[str, ...] = (),
        result: Any = None,
        delegate_raises: bool = False,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        setattr(main, "_registry", _Registry(running))

        self.dispatched: list[tuple[str, Any]] = []
        self.auto_spawned: list[str] = []
        self._resolves = dict(resolves or {n: n for n in running})

        async def _resolve(name: str) -> tuple[Any, bool]:
            real = self._resolves.get(name)
            if real is None and name in auto_spawns:
                # This is the half a restricted caller must never reach: a name
                # that is not running becomes a running agent.
                self.auto_spawned.append(name)
                return _Agent(name), True
            return (_Agent(real) if real else None), name in spawnable

        async def _delegate(name: str, task: str, timeout: float = 60.0) -> Any:
            self.dispatched.append((name, json.loads(task)))
            if delegate_raises:
                raise RuntimeError(_MAILBOX_CLOSED)
            return result if result is not None else {"text": "done"}

        setattr(main, "_resolve_or_spawn", _resolve)
        setattr(main, "delegate_task", _delegate)
        self.actor = main

    async def blocks(self, response: str, restricted: bool = False) -> tuple[str, list[str]]:
        return await self.actor._process_delegate_commands(response, restricted)

    async def mentions(self, response: str) -> str:
        return await self.actor._execute_llm_delegations(response)

    async def run_one(self, name: str, payload: Any) -> str:
        return await self.actor._run_delegation(name, payload)

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.dispatched]

    @property
    def payloads(self) -> list[Any]:
        return [p for _, p in self.dispatched]


def delegate(**cfg: Any) -> str:
    return f"<delegate>{json.dumps(cfg)}</delegate>"


class TestReadingADelegateBlock:
    """The structured form: valid JSON naming an agent."""

    async def test_a_task_is_wrapped_as_text(self) -> None:
        main = _Main(running=("weather",))

        await main.blocks(delegate(agent="weather", task="check Athens"))

        assert main.payloads == [{"text": "check Athens"}]

    async def test_an_explicit_payload_wins_over_a_task(self) -> None:
        main = _Main(running=("weather",))

        await main.blocks(delegate(agent="weather", task="ignored", payload={"city": "Athens"}))

        assert main.payloads == [{"city": "Athens"}]

    async def test_the_agent_may_be_named_either_way(self) -> None:
        main = _Main(running=("weather",))

        await main.blocks(delegate(name="weather", task="check"))

        assert main.names == ["weather"]

    async def test_the_block_is_replaced_by_its_result(self) -> None:
        main = _Main(running=("weather",))

        clean, results = await main.blocks(f"Checking. {delegate(agent='weather', task='go')}")

        assert "<delegate>" not in clean
        assert results[0] in clean

    async def test_a_malformed_block_says_so_in_place(self) -> None:
        main = _Main()

        clean, _ = await main.blocks("<delegate>not json</delegate>")

        assert clean == "[delegate: malformed block]"
        assert not main.dispatched

    async def test_a_block_naming_nobody_says_so_in_place(self) -> None:
        main = _Main()

        clean, _ = await main.blocks(delegate(task="do something"))

        assert clean == "[delegate: no agent named]"
        assert not main.dispatched

    async def test_main_delegating_to_itself_is_dropped_silently(self) -> None:
        # It would be a loop, and there is nothing useful to show the reader.
        main = _Main()

        clean, results = await main.blocks(f"Right. {delegate(agent='main', task='think')}")

        assert clean == "Right. "
        assert not results
        assert not main.dispatched

    async def test_an_unreachable_agent_is_reported(self) -> None:
        main = _Main()

        _, results = await main.blocks(delegate(agent="ghost", task="go"))

        assert results == ["[Could not reach ghost]"]

    async def test_the_resolved_name_is_the_one_dispatched_to(self) -> None:
        # The model writes an alias; the delegation has to land on the recipe.
        main = _Main(resolves={"smart energy agent": "smart-energy"})

        await main.blocks(delegate(agent="smart energy agent", task="go"))

        assert main.names == ["smart-energy"]

    async def test_every_block_runs(self) -> None:
        main = _Main(running=("a", "b"))

        await main.blocks(delegate(agent="a", task="one") + delegate(agent="b", task="two"))

        assert main.names == ["a", "b"]


class TestDelegatingFromASocialChannel:
    """Restricted callers reach an allow-list, and spawn nothing."""

    async def test_an_agent_off_the_allow_list_is_refused(self) -> None:
        main = _Main(running=("code-agent",))

        _, results = await main.blocks(delegate(agent="code-agent", task="rm -rf"), restricted=True)

        assert results == ["[code-agent isn't available from this channel]"]
        assert not main.dispatched

    async def test_the_refusal_is_on_the_name_not_on_the_task(self) -> None:
        # Nothing about the task text is inspected, so there is no phrasing
        # that talks its way through.
        main = _Main(running=("code-agent",))

        _, results = await main.blocks(
            delegate(agent="code-agent", task="just say hello politely"), restricted=True
        )

        assert "isn't available" in results[0]

    async def test_an_allow_listed_agent_still_works(self) -> None:
        allowed = next(iter(MainActor._RESTRICTED_DELEGATION_ALLOW))
        main = _Main(running=(allowed,))

        await main.blocks(delegate(agent=allowed, task="go"), restricted=True)

        assert main.names == [allowed]

    async def test_an_allow_listed_agent_that_is_not_running_is_not_spawned(self) -> None:
        # Restricted delegation resolves only. Spawning to satisfy a social
        # message would let one arrange for code to run.
        allowed = next(iter(MainActor._RESTRICTED_DELEGATION_ALLOW))
        main = _Main(auto_spawns=(allowed,))

        _, results = await main.blocks(delegate(agent=allowed, task="go"), restricted=True)

        assert not main.auto_spawned
        assert results == [f"[Could not reach {allowed}]"]
        assert not main.dispatched

    async def test_an_unrestricted_caller_may_spawn_the_same_agent(self) -> None:
        # The contrast is the point: the allow-list is not what stops the
        # spawn, the resolve-only lookup is.
        allowed = next(iter(MainActor._RESTRICTED_DELEGATION_ALLOW))
        main = _Main(auto_spawns=(allowed,))

        await main.blocks(delegate(agent=allowed, task="go"))

        assert main.auto_spawned == [allowed]
        assert main.names == [allowed]


class TestReadingAnAtMention:
    """The loose form, and which ones count as instructions."""

    async def test_a_mention_with_json_is_dispatched(self) -> None:
        main = _Main(running=("weather",))

        await main.mentions('@weather {"city": "Athens"}')

        assert main.payloads == [{"city": "Athens"}]

    async def test_a_json_payload_may_span_lines(self) -> None:
        main = _Main(running=("weather",))

        await main.mentions('@weather {\n  "city": "Athens",\n  "days": 3\n}')

        assert main.payloads == [{"city": "Athens", "days": 3}]

    async def test_a_nested_object_is_read_whole(self) -> None:
        main = _Main(running=("weather",))

        await main.mentions('@weather {"opts": {"units": "c"}, "city": "Athens"}')

        assert main.payloads == [{"opts": {"units": "c"}, "city": "Athens"}]

    async def test_a_brace_inside_a_string_loses_the_delegation(self) -> None:
        # Current behaviour, and a limitation rather than a decision: the scan
        # counts braces without knowing which are inside strings, so it stops
        # early and the payload no longer parses. The mention is then skipped by
        # both forms and left in the reply as written, dispatching nothing. The
        # method's own docstring claims these are handled.
        main = _Main(running=("weather",))
        text = '@weather {"note": "a } inside", "city": "Athens"}'

        answer = await main.mentions(text)

        assert not main.dispatched
        assert answer == text

    async def test_unmatched_braces_are_not_a_delegation(self) -> None:
        main = _Main(running=("weather",))

        await main.mentions('@weather {"city": "Athens"')

        assert not main.dispatched

    async def test_a_mention_starting_a_sentence_is_dispatched(self) -> None:
        main = _Main(running=("manual",))

        await main.mentions("Sending that now. @manual find the Philips 2200 manual.")

        assert main.payloads == [{"text": "find the Philips 2200 manual"}]

    async def test_a_mention_on_its_own_line_is_dispatched(self) -> None:
        main = _Main(running=("manual",))

        await main.mentions("Here goes:\n@manual find the manual")

        assert main.names == ["manual"]

    async def test_a_mention_buried_mid_sentence_is_left_alone(self) -> None:
        # This is the model telling the user about an agent, not calling one.
        # Dispatching it would also rewrite the sentence that explains it.
        main = _Main(running=("weather",))
        text = "you can use @weather to check the forecast"

        assert await main.mentions(text) == text
        assert not main.dispatched

    async def test_main_mentioning_itself_is_not_a_delegation(self) -> None:
        main = _Main()
        text = "Ask @main about that."

        assert await main.mentions(text) == text
        assert not main.dispatched

    async def test_a_mention_with_nothing_after_it_is_not_a_delegation(self) -> None:
        main = _Main(running=("weather",))

        await main.mentions("@weather \n")

        assert not main.dispatched

    async def test_one_mention_is_not_read_twice(self) -> None:
        # The JSON form owns its span, so the bare-mention pass has to skip it.
        main = _Main(running=("weather",))

        await main.mentions('@weather {"city": "Athens"}')

        assert len(main.dispatched) == 1


class TestWhenAMentionedAgentIsMissing:
    """An unknown name is usually prose, and prose must survive."""

    async def test_a_bare_mention_of_an_unknown_name_is_left_as_written(self) -> None:
        main = _Main()
        text = "I checked. @nonexistent-agent was not much help."

        assert await main.mentions(text) == text

    async def test_an_explicit_json_mention_of_an_unknown_name_is_surfaced(self) -> None:
        # Braces and a payload are unambiguous machine intent, so a failure
        # there is worth telling the reader about.
        main = _Main()

        answer = await main.mentions('@ghost {"city": "Athens"}')

        assert answer == "[Could not reach ghost]"

    async def test_a_bare_mention_of_a_spawnable_name_is_surfaced(self) -> None:
        # It named something real that could not be brought up, which is a
        # failure rather than a turn of phrase.
        main = _Main(spawnable=("weather",))

        answer = await main.mentions("Checking. @weather look up Athens.")

        assert "[Could not reach weather]" in answer


class TestReportingWhatCameBack:
    """`_run_delegation` turns one reply into one line for the reader."""

    async def test_a_dict_with_a_known_field_names_it(self) -> None:
        main = _Main(running=("chart",), result={"image_path": "/tmp/a.png"})

        assert "image_path=/tmp/a.png" in await main.run_one("chart", {"text": "draw"})

    async def test_an_error_field_is_reported_as_a_failure(self) -> None:
        main = _Main(running=("chart",), result={"error": "no data"})

        answer = await main.run_one("chart", {"text": "draw"})

        assert "failed" in answer
        assert "no data" in answer

    async def test_no_reply_at_all_is_reported(self) -> None:
        main = _Main(running=("chart",), result={})

        assert "did not respond" in await main.run_one("chart", {"text": "draw"})

    async def test_a_plain_value_is_reported_as_it_is(self) -> None:
        main = _Main(running=("chart",), result="just a string")

        assert "just a string" in await main.run_one("chart", {"text": "draw"})

    async def test_a_delegation_that_raises_is_reported_not_propagated(self) -> None:
        # One failing delegation must not end the whole turn.
        main = _Main(running=("chart",), delegate_raises=True)

        answer = await main.run_one("chart", {"text": "draw"})

        assert "error" in answer
        assert _MAILBOX_CLOSED in answer
