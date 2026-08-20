"""What `MainActor.process_user_input` does with a command, pinned as it is.

The command surface is an if-chain, and its *sequence* carries meaning: exact
matches sit ahead of prefix matches, one command is rewritten into another and
falls through to it, and two spellings share a handler that works out which one
matched. None of that survives a naive "parse `/name`, look it up" rewrite, and
none of it is written down anywhere except the order of the branches.

These tests are that contract. They describe current behaviour rather than
desired behaviour: a change that breaks one is a change in what the chain does,
which is the thing to notice. Where a branch's own comment records a past bug,
the test names it, so the rewrite cannot reintroduce it quietly.

The stub gives the chain only what the command branches touch, and records what
each one reached for. Anything falling past the chain lands in `_classify_intent`,
which is how these tests tell "handled as a command" from "sent to the model".
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.delegation import DelegationManager
from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService


class _Registry:
    """The actor registry, with only the two lookups the chain performs."""

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self._by = {n: SimpleNamespace(name=n) for n in names}

    def find_by_name(self, name: str) -> SimpleNamespace | None:
        return self._by.get(name)

    def all_actors(self) -> list[SimpleNamespace]:
        return list(self._by.values())


class _Main:
    """A MainActor with the command surface's collaborators stubbed out.

    `calls` records what the chain reached for, which is what most of these
    tests assert on: the reply text says a command ran, and the recorded call
    says *which* branch ran it.
    """

    def __init__(
        self,
        *,
        agents: tuple[str, ...] = (),
        notification: str = "",
        plan_reply: str | None = None,
        collision_warning: str | None = None,
    ) -> None:
        m = MainActor.__new__(MainActor)
        m.name = "main"
        m._registry = _Registry(agents)  # pyright: ignore[reportAttributeAccessIssue]
        m.manifests = ManifestRegistry(m)
        m.nodes = NodeManager(m, m.manifests)
        m.delegation = DelegationManager(m)
        m.spawns = SpawnService(m)
        m.lifecycle = LifecycleService(m)
        m.migration = Migration(m, m.nodes)
        m._known_nodes = {}
        m._agent_manifests = {}
        # Real `Actor` attributes the branches reach through: `recall` consults
        # persistence, and several branches publish. Absent rather than faked,
        # so both take their own "nothing configured" path.
        m._persistence_api = None
        m._persistent_state = {}
        m._mqtt_client = None
        m._history_summary = ""
        m.get_user_facts = dict
        m.get_pending_plans = dict
        m.get_pipeline_rules = dict
        m.actor_id = "main-under-test"
        self.calls: dict[str, Any] = {
            "capabilities": [],
            "topics": [],
            "deleted": [],
            "classified": 0,
            "recorded": [],
        }

        m._drain_notifications = lambda: notification
        m.list_nodes = list
        m.list_topics = lambda keyword="": (self.calls["topics"].append(keyword), [])[1]
        m._get_spawn_registry = dict

        async def _delete(name: str) -> None:
            self.calls["deleted"].append(name)

        m.delete_spawned_agent = _delete

        def _capabilities(keyword: str = "") -> list[Any]:
            self.calls["capabilities"].append(keyword)
            return []

        m.list_capabilities = _capabilities

        async def _record(text: str, reply: str) -> None:
            self.calls["recorded"].append((text, reply))

        m._record_external_exchange = _record  # pyright: ignore[reportAttributeAccessIssue]

        async def _pending_plan(_text: str) -> str | None:
            self.calls["plan_checked"] = True
            return plan_reply

        m._handle_pending_plan_response = _pending_plan  # pyright: ignore[reportAttributeAccessIssue]

        def _collision(_text: str) -> str | None:
            return collision_warning

        m._warn_if_pending_plan_collision = _collision  # pyright: ignore[reportAttributeAccessIssue]

        async def _classify(_text: str) -> str:
            self.calls["classified"] += 1
            return "OTHER"

        m._classify_intent = _classify  # pyright: ignore[reportAttributeAccessIssue]

        async def _chat(text: str, attachments: Any = None) -> str:
            return f"<model answered {text!r}>"

        m.chat = _chat  # pyright: ignore[reportAttributeAccessIssue]

        # The tail of the fall-through path, past the command chain. Stubbed so
        # "went to the model" is observable rather than an exception, without
        # these tests taking a position on what the model path then does.
        m._conversation_history = []
        m._rebuild_system_prompt = lambda: None
        m._prefix_with_live_context = lambda text: text  # pyright: ignore[reportAttributeAccessIssue]
        m.persist = lambda key, value: None
        self.actor = m

    def __call__(self, text: str) -> str:
        return asyncio.run(self.actor.process_user_input(text))


def reply(text: str, **kwargs: Any) -> str:
    """Run one input through the real chain and return what it answered."""
    return _Main(**kwargs)(text)


def main_reply(text: str, **kwargs: Any) -> str:
    """The reply, for the cases where its exact wording is the behaviour."""
    return _Main(**kwargs)(text)


def went_to_the_model(text: str, **kwargs: Any) -> bool:
    """Whether `text` fell past every command branch."""
    main = _Main(**kwargs)
    main(text)
    return main.calls["classified"] == 1


class TestCommandsWithoutASlash:
    """Matching is on the stripped text, not on a leading `/`.

    These spellings are live, so a matcher that assumes "command means starts
    with a slash" drops them. They are also the reason the chain cannot be
    keyed on the first character.
    """

    @pytest.mark.parametrize("text", ["/help", "help", "/?"])
    def test_help_answers_to_three_spellings(self, text: str) -> None:
        assert "**Wactorz commands**" in reply(text)

    @pytest.mark.parametrize("text", ["/nodes", "list_nodes", "main.list_nodes"])
    def test_the_node_list_answers_to_three_spellings(self, text: str) -> None:
        assert "local" in reply(text)

    @pytest.mark.parametrize("text", ["help", "list_nodes", "main.list_nodes"])
    def test_a_bare_alias_is_still_a_command(self, text: str) -> None:
        assert not went_to_the_model(text)


class TestTheReadOnlyReports:
    """Commands that only look things up and print them.

    Each is pinned by the one thing that would break silently: that it is
    handled here at all. A command that quietly stops matching becomes a
    sentence for the model to answer, which reads like a plausible reply rather
    than a missing feature.
    """

    @pytest.mark.parametrize("text", ["/topics", "/mqtt", "/bus", "/registry"])
    def test_it_is_answered_without_the_model(self, text: str) -> None:
        assert not went_to_the_model(text)

    def test_topics_passes_its_keyword_through(self) -> None:
        main = _Main()
        main("/topics weather")

        assert main.calls["topics"] == ["weather"]

    def test_topics_with_no_keyword_asks_for_everything(self) -> None:
        main = _Main()
        main("/topics")

        assert main.calls["topics"] == [""]

    def test_topics_accepts_the_call_spelling(self) -> None:
        # A model writing it as a function still reaches the command, and the
        # brackets do not become part of the keyword.
        main = _Main()
        main("/topics(weather)")

        assert main.calls["topics"] == ["weather"]

    def test_a_near_miss_is_not_the_command(self) -> None:
        assert went_to_the_model("/topic")


class TestTheHeavyCommands:
    """`/webhook`, `/migrate`, `/deploy` and the node sub-verbs.

    None had a dispatch test. These do the most: deploy installs software on a
    machine over SSH, migrate moves a running agent between hosts. A command
    that stops matching becomes a sentence the model answers, which is a
    plausible-sounding reply where an action was expected.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "/webhook",
            "/webhook discord https://example.invalid/hook",
            "/webhook telegram https://example.invalid/hook",
            "/migrate",
            "/migrate collector rpi",
            "/deploy",
            "/deploy rpi-kitchen",
            "/nodes remove rpi",
            "/nodes restart rpi",
            "/nodes shutdown rpi",
        ],
    )
    def test_it_is_handled_without_the_model(self, text: str) -> None:
        assert not went_to_the_model(text)

    def test_migrate_without_arguments_explains_itself(self) -> None:
        # Two arguments are required; a bare call must say so rather than
        # silently doing nothing or moving the wrong agent.
        assert reply("/migrate") != reply("/migrate collector rpi")

    def test_deploy_without_a_node_lists_the_targets(self) -> None:
        assert reply("/deploy") != reply("/deploy rpi-kitchen")

    @pytest.mark.parametrize("text", ["/webhoo", "/migrat", "/deplo", "/node remove rpi"])
    def test_a_near_miss_is_not_the_command(self, text: str) -> None:
        assert went_to_the_model(text)


class TestCommandsThatChangeState:
    """`/memory`, `/rules`, `/plans` and `/clear-plans`.

    Pinned before they move because they had almost no dispatch coverage: the
    only tests naming them assert a social channel cannot reach them, which
    says nothing about whether the dashboard still can.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "/memory",
            "/memory clear",
            "/memory forget colour",
            "/rules",
            "rules",
            "/rules delete r1",
            "/plans",
            "/plans show p1",
            "/plans approve p1",
            "/plans reject p1",
            "/clear-plans",
        ],
    )
    def test_it_is_handled_without_the_model(self, text: str) -> None:
        assert not went_to_the_model(text)

    def test_the_bare_rules_listing_is_not_the_delete(self) -> None:
        # `/rules` matches exactly and `/rules delete` by prefix; the exact one
        # has to win, or listing rules would delete one named "delete".
        assert reply("/rules delete r1") != reply("/rules")

    def test_plans_takes_a_sub_verb(self) -> None:
        assert reply("/plans show p1") != reply("/plans")

    def test_clear_plans_is_its_own_command(self) -> None:
        # It is not `/plans clear`: a near spelling must not reach it, since
        # this one throws the pending proposals away.
        assert reply("/clear-plans") != reply("/plans")

    @pytest.mark.parametrize("text", ["/memor", "/plan", "/rule", "/clear-plan"])
    def test_a_near_miss_is_not_the_command(self, text: str) -> None:
        assert went_to_the_model(text)


class TestRewriteAndFallThrough:
    """`/delete` and `/stop` are rewritten and handled by a later branch.

    This is aliasing with mutation: the branch that matches is not the branch
    that answers. The equivalence below is the whole of its observable
    behaviour, so it is what a registry has to reproduce — an explicit rewrite
    step, not a second copy of the handler.
    """

    @pytest.mark.parametrize(
        ("short", "long"),
        [("/delete ghost", "/agents delete ghost"), ("/stop ghost", "/agents stop ghost")],
    )
    def test_the_short_form_answers_exactly_as_the_long_one(self, short: str, long: str) -> None:
        assert reply(short) == reply(long)

    def test_the_rewrite_takes_the_agent_name_with_it(self) -> None:
        assert "ghost" in reply("/delete ghost")

    def test_only_the_first_rewrite_applies(self) -> None:
        # The loop breaks on its first match, so a name that begins with the
        # other prefix is not rewritten twice.
        assert reply("/delete /stop x") == reply("/agents delete /stop x")


class TestSubVerbFamilies:
    """`<command> <verb> <argument>` — the verb chooses the behaviour."""

    @pytest.mark.parametrize("verb", ["stop", "delete", "pause", "remove"])
    def test_the_agents_verbs_are_handled_without_the_model(self, verb: str) -> None:
        assert not went_to_the_model(f"/agents {verb} ghost")

    @pytest.mark.parametrize("verb", ["remove", "restart", "shutdown"])
    def test_the_nodes_verbs_are_handled_without_the_model(self, verb: str) -> None:
        assert not went_to_the_model(f"/nodes {verb} somenode")

    def test_an_unknown_agents_verb_becomes_a_capability_search(self) -> None:
        # Not refused, and not sent to the model: the general `/agents ` block
        # sits after the verb branches and takes whatever they left, so the
        # whole remainder is read as a search keyword.
        main = _Main()
        main("/agents frobnicate ghost")

        assert main.calls["capabilities"] == ["frobnicate ghost"]
        assert not main.calls["classified"]

    def test_an_unknown_nodes_verb_goes_to_the_model_instead(self) -> None:
        # The asymmetry is worth keeping in view: `/nodes` matches exactly and
        # its verbs are separate prefixes, with no general block behind them, so
        # an unknown verb falls past the chain rather than being reinterpreted.
        # Two families, two answers to the same shape of mistake.
        assert went_to_the_model("/nodes frobnicate somenode")


class TestTwoNamesOneHandler:
    """`/agents` and `/capabilities` share a block that reads the keyword from
    whichever prefix matched."""

    @pytest.mark.parametrize("name", ["/agents", "/capabilities"])
    def test_both_bare_forms_ask_for_everything(self, name: str) -> None:
        main = _Main()
        main(name)

        assert main.calls["capabilities"] == [""]

    @pytest.mark.parametrize("name", ["/agents", "/capabilities"])
    def test_both_pass_the_keyword_through(self, name: str) -> None:
        main = _Main()
        main(f"{name} weather")

        assert main.calls["capabilities"] == ["weather"]

    def test_the_two_spellings_answer_identically(self) -> None:
        assert reply("/agents weather") == reply("/capabilities weather")


class TestOrderIsSemantic:
    """Branch order encodes precedence, so registration order is dispatch order.

    Each case below is a pair where a later branch would also match, and the
    earlier one has to win.
    """

    @pytest.mark.parametrize("verb", ["stop", "delete", "pause", "remove", "restart"])
    def test_a_verb_is_never_read_as_a_search_term(self, verb: str) -> None:
        # The general listing matches any `/agents …`, so ahead of the verbs it
        # would report that no agent matches "stop" — having stopped nothing,
        # and looking like an answer rather than a failure.
        main = _Main()

        main(f"/agents {verb} ghost")

        assert not main.calls["capabilities"]

    @pytest.mark.parametrize("verb", ["stop", "delete", "pause", "remove"])
    def test_a_verb_with_no_agent_named_deletes_nothing(self, verb: str) -> None:
        # An unfinished sentence, not a request to act on an agent called "".
        # A command that accepts it answers that it deleted something.
        main = _Main()

        answer = main(f"/agents {verb}")

        assert not main.calls["deleted"]
        assert "permanently deleted" not in answer

    @pytest.mark.parametrize("command", ["/start", "/pause", "/resume"])
    def test_a_bare_lifecycle_verb_asks_for_an_agent(self, command: str) -> None:
        assert main_reply(command) == f"Usage: {command} <agent-name>"

    def test_agents_restart_beats_the_general_agents_block(self) -> None:
        # `/agents restart x` matches the `/agents ` prefix too. If the general
        # block ran first it would treat "restart x" as a capability keyword.
        main = _Main()
        main("/agents restart ghost")

        assert not main.calls["capabilities"]

    def test_rules_delete_is_not_the_bare_rules_listing(self) -> None:
        assert reply("/rules delete r1") != reply("/rules")

    def test_the_bare_node_list_is_not_a_node_sub_verb(self) -> None:
        assert "local" in reply("/nodes")


class TestABarePrefixReachesItsUsageHint:
    """A branch's own comment records this: the text is already stripped, so a
    bare `/pause` never matched the prefix form and fell through to the model,
    leaving the usage hint unreachable. Both spellings are matched now.
    """

    @pytest.mark.parametrize("command", ["/start", "/pause", "/resume"])
    def test_it_answers_with_usage_rather_than_asking_the_model(self, command: str) -> None:
        answer = reply(command)

        assert answer == f"Usage: {command} <agent-name>"

    @pytest.mark.parametrize("command", ["/start", "/pause", "/resume"])
    def test_the_bare_form_does_not_reach_the_model(self, command: str) -> None:
        assert not went_to_the_model(command)

    def test_the_prefix_form_still_takes_its_argument(self) -> None:
        assert "ghost" in reply("/pause ghost")


class TestHowTheTextIsNormalised:
    """Everything the chain does to the input before matching it."""

    @pytest.mark.parametrize("text", ["  /help  ", "\t/help\n", "/help "])
    def test_surrounding_whitespace_is_ignored(self, text: str) -> None:
        assert "**Wactorz commands**" in reply(text)

    @pytest.mark.parametrize("text", ["/help()", "/nodes()", "/agents()"])
    def test_a_trailing_call_syntax_is_stripped(self, text: str) -> None:
        # A model writing `/help()` as if it were a function still reaches the
        # command, which is what `rstrip("()")` is for.
        assert not went_to_the_model(text)

    @pytest.mark.parametrize("text", ["/HELP", "Help", "/Agents"])
    def test_matching_is_case_sensitive(self, text: str) -> None:
        assert went_to_the_model(text)


class TestWhatComesBeforeTheChain:
    """A pending plan is answered first, and slash commands are exempt.

    The exemption is deliberate — a bare "yes" must not be read as a new
    prompt, but inspecting `/plans` while a plan waits must still work. It also
    means the non-slash aliases are *not* exempt, which a rewrite has to keep.
    """

    def test_a_plan_answer_is_taken_before_any_command(self) -> None:
        assert reply("yes", plan_reply="plan approved") == "plan approved"

    def test_a_slash_command_is_not_offered_to_the_pending_plan(self) -> None:
        main = _Main(plan_reply="plan approved")
        main("/help")

        assert "plan_checked" not in main.calls

    def test_an_at_mention_is_not_offered_to_the_pending_plan(self) -> None:
        main = _Main(plan_reply="plan approved")
        main("@ghost hello")

        assert "plan_checked" not in main.calls

    def test_a_non_slash_alias_is_offered_to_the_pending_plan_first(self) -> None:
        # `help` has no slash, so it goes through the plan check on its way to
        # the command chain. It survives only because the check declines it.
        main = _Main()
        answer = main("help")

        assert main.calls["plan_checked"] is True
        assert "**Wactorz commands**" in answer

    def test_a_pending_plan_can_swallow_a_non_slash_alias(self) -> None:
        # The consequence of the line above, stated outright: with a plan
        # waiting that claims this text, `help` never reaches the chain.
        assert reply("help", plan_reply="plan approved") == "plan approved"

    def test_a_collision_warning_stops_the_input(self) -> None:
        assert reply("build me a pipeline", collision_warning="resolve the plan first") == (
            "resolve the plan first"
        )


class TestNotificationsRideAlong:
    """Queued notifications prefix whatever the chain answers, on every path."""

    def test_a_command_reply_carries_the_prefix(self) -> None:
        assert reply("/help", notification="[alert] disk full\n").startswith("[alert] disk full\n")

    def test_a_usage_hint_carries_the_prefix(self) -> None:
        assert reply("/pause", notification="[alert] disk full\n") == (
            "[alert] disk full\nUsage: /pause <agent-name>"
        )
