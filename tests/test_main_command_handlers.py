"""What main's slash commands say and do, handler by handler.

The handlers reach main only through `CommandContext.actor`, so a stand-in
that answers the few calls each one makes is enough to pin the reply text and
the effect. Dispatch itself — which spelling reaches which handler — is covered
where the registry is.

The commands that change something check before they act: a remote agent is
told to its node rather than stopped locally, an agent that is not stopped is
not started, and a plan that is not pending cannot be approved twice.
"""

import time
from dataclasses import replace
from typing import Any, cast

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.commands import agents as agent_cmds
from wactorz.agents.main.commands import info, state
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.agents.main.planning import PENDING_PLANS_KEY
from wactorz.config import CONFIG
from wactorz.core import topic_bus
from wactorz.core.actor import ActorState
from wactorz.core.topic_bus import TopicBus, TopicContract


class _Agent:
    def __init__(self, name: str, state: ActorState = ActorState.RUNNING) -> None:
        self.name = name
        self.actor_id = f"id-{name}"
        self.state = state
        self.commands: list[str] = []
        self.accept = True
        self.error: Exception | None = None

    async def apply_command(self, command: str) -> bool:
        self.commands.append(command)
        if self.error:
            raise self.error
        return self.accept


class _Registry:
    def __init__(self, *agents: _Agent) -> None:
        self._agents = list(agents)
        self.unregistered: list[str] = []

    def all_actors(self) -> list[_Agent]:
        return list(self._agents)

    def find_by_name(self, name: str) -> _Agent | None:
        return next((a for a in self._agents if a.name == name), None)

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


class _Nodes:
    def __init__(self, online: list[str]) -> None:
        self._online = online

    def online_names(self) -> list[str]:
        return list(self._online)


class _Main:
    """The parts of MainActor the command handlers call."""

    name = "main"

    def __init__(self) -> None:
        self._registry: _Registry | None = _Registry()
        self._store: dict[str, Any] = {}
        self._history_summary = ""
        self._mqtt_client: Any = None
        self._agent_manifests: dict[str, Any] = {}
        self._known_nodes: dict[str, Any] = {}
        self.nodes = _Nodes([])
        self.spawn_registry: dict[str, dict[str, Any]] = {}
        self.pipeline_rules: dict[str, dict[str, Any]] = {}
        self.node_list: list[dict[str, Any]] = []
        self.topic_list: list[dict[str, Any]] = []
        self.capability_list: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.published: list[tuple[str, Any]] = []
        self.migrate_result: Any = {"success": True, "message": "moved"}
        self.delete_error: Exception | None = None

    # memory
    def persist(self, key: str, value: Any) -> None:
        self._store[key] = value

    def recall(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def get_user_facts(self) -> dict[str, Any]:
        return dict(self._store.get("_user_facts", {}))

    def _rebuild_system_prompt(self) -> None:
        self.calls.append(("rebuild", None))

    def _inject_user_facts_into_prompt(self) -> None:
        self.calls.append(("inject", None))

    # rules and plans
    def get_pipeline_rules(self) -> dict[str, dict[str, Any]]:
        return self.pipeline_rules

    async def delete_pipeline_rule(self, rule_id: str) -> str:
        return f"deleted {rule_id}"

    def get_pending_plans(self) -> dict[str, dict[str, Any]]:
        return self._store.get(PENDING_PLANS_KEY, {})

    async def _execute_pending_plan(self, plan: dict[str, Any]) -> str:
        return f"executed {plan['plan_id']}"

    def _reject_pending_plan(self, plan: dict[str, Any]) -> str:
        return f"rejected {plan['plan_id']}"

    def _format_plan_proposal(self, plan: dict[str, Any]) -> str:
        return f"Proposal {plan['plan_id']}"

    # listings
    def list_nodes(self) -> list[dict[str, Any]]:
        return self.node_list

    def list_topics(self, keyword: str = "") -> list[dict[str, Any]]:
        self.calls.append(("topics", keyword))
        return self.topic_list

    def list_capabilities(self, keyword: str = "") -> list[dict[str, Any]]:
        return self.capability_list

    # agents
    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.spawn_registry

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))

    async def _spawn_from_config(self, config: dict[str, Any], **kwargs: Any) -> None:
        self.calls.append(("spawn", (config, kwargs)))

    async def migrate_agent(self, name: str, node: str) -> Any:
        if isinstance(self.migrate_result, Exception):
            raise self.migrate_result
        return self.migrate_result

    async def delete_spawned_agent(self, name: str) -> None:
        if self.delete_error:
            raise self.delete_error
        self.calls.append(("delete", name))

    def _record_agent_deletion(self, name: str, reason: str = "") -> None:
        self.calls.append(("record", (name, reason)))


@pytest.fixture(name="main")
def main_fixture() -> _Main:
    return _Main()


def _ctx(main: _Main) -> CommandContext:
    return CommandContext(actor=cast(MainActor, main))


class TestMemory:
    async def test_facts_are_shown_grouped_with_the_summary(self, main: _Main) -> None:
        main._store["_user_facts"] = {
            "pref_name": "Ada",
            "device_lamp": "light.hall",
            "policy_quiet": "after 22:00",
            "legacy": "x",
        }
        main._history_summary = "s" * 400

        text = await state.manage_memory(_ctx(main), "")

        assert "User facts (4):" in text
        assert "  [Preferences & identity]\n    name: Ada" in text
        assert "  [Devices & setup]\n    lamp: light.hall" in text
        assert "  [Standing policies]\n    quiet: after 22:00" in text
        assert "  [Other / legacy]\n    legacy: x" in text
        assert "s" * 300 + "..." in text

    async def test_nothing_stored_says_so(self, main: _Main) -> None:
        text = await state.manage_memory(_ctx(main), "")

        assert "No user facts stored yet." in text
        assert "No conversation summary yet." in text

    async def test_clear_wipes_facts_and_summary(self, main: _Main) -> None:
        main._store["_user_facts"] = {"pref_name": "Ada"}
        main._history_summary = "old"

        text = await state.manage_memory(_ctx(main), "clear")

        assert text.startswith("Memory cleared")
        assert main._store["_user_facts"] == {}
        assert main._history_summary == ""
        assert ("rebuild", None) in main.calls

    async def test_forget_removes_one_fact(self, main: _Main) -> None:
        main._store["_user_facts"] = {"pref_name": "Ada", "pref_tz": "UTC"}

        assert await state.manage_memory(_ctx(main), "forget pref_name") == "Forgotten: 'pref_name'"
        assert main._store["_user_facts"] == {"pref_tz": "UTC"}
        assert (
            await state.manage_memory(_ctx(main), "forget nope") == "No fact found with key 'nope'."
        )


class TestRules:
    async def test_no_rules_suggests_how_to_make_one(self, main: _Main) -> None:
        assert (await state.show_rules(_ctx(main), "")).startswith("No pipeline rules active.")

    async def test_rules_are_listed_with_which_agents_are_running(self, main: _Main) -> None:
        main._registry = _Registry(_Agent("door-watcher"))
        main.pipeline_rules = {
            "r2": {"task": "second", "agents": ["gone"], "created_at": 0},
            "r1": {"task": "first", "agents": ["door-watcher", "notifier"], "created_at": 1},
        }

        text = await state.show_rules(_ctx(main), "")

        assert text.index("[r2]") < text.index("[r1]")
        assert "🟢 [r1] — first" in text
        assert "   stopped : notifier" in text
        assert "🔴 [r2] — second" in text
        assert "   created : unknown" in text

    async def test_delete_is_handed_to_main(self, main: _Main) -> None:
        assert await state.delete_rule(_ctx(main), "r1") == "deleted r1"

    async def test_clear_plans_empties_the_cache(self, main: _Main) -> None:
        main._store["_plan_cache"] = {"k": {}}

        assert await state.clear_plans(_ctx(main), "") == "Plan cache cleared."
        assert main._store["_plan_cache"] == {}

    async def test_a_failing_clear_is_reported(
        self, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(*_args: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(main, "persist", _refuse)

        assert await state.clear_plans(_ctx(main), "") == "Failed to clear plan cache: read-only"


class TestPlans:
    @staticmethod
    def _plans(main: _Main) -> None:
        now = time.time()
        main._store[PENDING_PLANS_KEY] = {
            "p1": {
                "plan_id": "p1",
                "status": "pending",
                "task": "when the door opens",
                "created_at": now,
                "envelope": {
                    "plan": [
                        {"name": "watcher", "spawn_config": {"code": "x" * 2100}},
                        {"name": "native", "spawn_config": {}},
                    ]
                },
            },
            "p2": {"plan_id": "p2", "status": "approved", "task": "old", "created_at": now - 5},
            "p3": {"plan_id": "p3", "status": "weird", "task": "odd", "created_at": now - 9},
        }

    async def test_the_list_shows_pending_and_recent_resolved(self, main: _Main) -> None:
        self._plans(main)

        text = await state.manage_plans(_ctx(main), "")

        assert "**Pending plans (1)**" in text
        assert "`p1` (2 agent(s)," in text
        assert "✅ `p2` (approved) — old" in text
        assert "? `p3` (weird) — odd" in text

    async def test_no_plans_says_so(self, main: _Main) -> None:
        assert await state.manage_plans(_ctx(main), "") == "No pending plans."

    async def test_show_prints_each_agents_code_truncated(self, main: _Main) -> None:
        self._plans(main)

        text = await state.manage_plans(_ctx(main), "show p1")

        assert text.startswith("Proposal p1")
        assert "--- watcher ---" in text
        assert "... (100 more chars truncated)" in text
        assert "(no code — pre-built type)" in text
        assert await state.manage_plans(_ctx(main), "show nope") == "No plan with id `nope`."

    async def test_approve_and_reject_only_a_pending_plan(self, main: _Main) -> None:
        self._plans(main)
        ctx = _ctx(main)

        assert await state.manage_plans(ctx, "approve p1") == "executed p1"
        assert await state.manage_plans(ctx, "reject p1") == "rejected p1"
        assert (
            await state.manage_plans(ctx, "approve p2") == "Plan `p2` is `approved`, not pending."
        )
        assert await state.manage_plans(ctx, "reject nope") == "No plan with id `nope`."

    async def test_clear_keeps_only_pending_plans(self, main: _Main) -> None:
        self._plans(main)

        text = await state.manage_plans(_ctx(main), "clear")

        assert text == "Cleared 2 resolved plan(s). 1 still pending."
        assert list(main._store[PENDING_PLANS_KEY]) == ["p1"]


class TestWebhooks:
    @pytest.fixture(autouse=True)
    def _no_env_webhook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(state, "CONFIG", replace(CONFIG, discord_webhook_url=""))

    async def test_nothing_stored_explains_how_to_store_one(self, main: _Main) -> None:
        assert (await state.manage_webhooks(_ctx(main), "")).startswith("No notification URLs")

    async def test_a_url_is_stored_and_listed(self, main: _Main) -> None:
        ctx = _ctx(main)

        saved = await state.manage_webhooks(ctx, "Slack https://hooks.slack.com/x")
        listed = await state.manage_webhooks(ctx, "")

        assert saved == "Saved slack webhook URL. Pipelines will use it automatically."
        assert listed == "Stored notification URLs:\n  slack: https://hooks.slack.com/x"

    async def test_a_service_without_a_url_gets_usage(self, main: _Main) -> None:
        assert (await state.manage_webhooks(_ctx(main), "discord")).startswith("Usage:")


class TestHelpAndNodes:
    async def test_help_lists_every_group(self, main: _Main) -> None:
        text = await info.show_help(_ctx(main), "")

        for heading in ("**Agents**", "**Nodes**", "**Memory**", "**Notifications**"):
            assert heading in text

    async def test_nodes_lists_local_then_remote(self, main: _Main) -> None:
        main._registry = _Registry(_Agent("weather"), _Agent("main"))
        main.node_list = [
            {"node": "rpi-b", "online": False, "agents": [], "last_seen": time.time() - 90},
            {"node": "rpi-a", "online": True, "agents": ["cam"], "last_seen": time.time()},
        ]

        text = await info.show_nodes(_ctx(main), "")

        lines = text.splitlines()
        assert "agents: @main, @weather" in lines[1]
        assert lines[2].lstrip().startswith("rpi-a")
        assert "🔴 offline  |  agents: (no agents)" in lines[3]
        assert text.endswith("To remove a remote node: /nodes remove <node-name>")

    async def test_no_remote_nodes_suggests_deploying_one(self, main: _Main) -> None:
        main._registry = None

        text = await info.show_nodes(_ctx(main), "")

        assert "agents: (none)" in text
        assert "deploy one with /deploy" in text


class TestTopicsAndMqtt:
    async def test_topics_strip_call_syntax_and_list_publishers(self, main: _Main) -> None:
        main.topic_list = [
            {"topic": "sensors/t", "agents": [{"name": "thermo", "node": "rpi"}, {"name": "sim"}]}
        ]

        text = await info.show_topics(_ctx(main), "(temp)")

        assert ("topics", "temp") in main.calls
        assert text.startswith("Known MQTT topics matching 'temp':")
        assert "← thermo (rpi), sim" in text

    @pytest.mark.parametrize(
        ("keyword", "fragment"), [("", "No topics found."), ("x", "matching 'x'")]
    )
    async def test_no_topics_explains_when_they_appear(
        self, main: _Main, keyword: str, fragment: str
    ) -> None:
        assert fragment in await info.show_topics(_ctx(main), keyword)

    async def test_mqtt_without_a_client(self, main: _Main) -> None:
        assert await info.show_mqtt_status(_ctx(main), "") == "MQTT publisher not initialised."

    async def test_mqtt_status_warns_about_a_queue(self, main: _Main) -> None:
        class _Client:
            connected = False
            queue_depth = 3
            _client_id = "wactorz-pub-x"
            _db_path = "/state/outbox.db"

        main._mqtt_client = _Client()

        text = await info.show_mqtt_status(_ctx(main), "")

        assert "🔴 connected   : False" in text
        assert "client_id   : wactorz-pub-x" in text
        assert "⚠️  3 message(s) queued" in text


class TestBus:
    async def test_no_bus(self, main: _Main, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(topic_bus, "_topic_bus", None)

        assert await info.show_bus(_ctx(main), "") == "TopicBus not initialised."

    async def test_contracts_and_wiring_are_listed(
        self, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        bus.register_contract(
            TopicContract(name="thermo", publishes=["t/1"], node="rpi", triggers_when={"hot": True})
        )
        bus.register_contract(TopicContract(name="fan", subscribes=["t/#"]))
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)

        text = await info.show_bus(_ctx(main), "")

        assert "agents with contracts : 2" in text
        assert "  [thermo] on rpi\n    publishes : t/1\n    triggers  : {'hot': True}" in text
        assert "    subscribes: t/#" in text
        assert "  thermo → fan  via t/1" in text

    async def test_a_broken_bus_is_reported(
        self, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> TopicBus:
            raise RuntimeError("corrupt")

        monkeypatch.setattr(topic_bus, "get_topic_bus", _broken)

        assert await info.show_bus(_ctx(main), "") == "TopicBus error: corrupt"


class TestRegistryDiagnostic:
    async def test_agreeing_sources_report_consistency(self, main: _Main) -> None:
        main._registry = _Registry(_Agent("weather"), _Agent("main"))
        main.spawn_registry = {"weather": {"node": ""}}
        main._agent_manifests = {"weather": {}}

        text = await info.show_registry(_ctx(main), "")

        assert "    weather  (RUNNING)" in text
        assert "    weather  on local" in text
        assert "✅ All three sources agree" in text

    async def test_every_kind_of_disagreement_is_named(self, main: _Main) -> None:
        main._registry = _Registry(_Agent("adhoc"))
        main.spawn_registry = {"missing": {"node": ""}, "remote": {"node": "rpi"}}
        main._agent_manifests = {"ghost": {"node": "old"}, "cam": {}}
        main._known_nodes = {"rpi": {"agents": ["cam"]}}

        text = await info.show_registry(_ctx(main), "")

        assert "'adhoc' is RUNNING but NOT in spawn registry" in text
        assert "'missing' is in spawn registry but NOT running locally" in text
        assert "'ghost' is in manifest cache but nowhere else" in text
        assert "'remote' assigned to node 'rpi' which is OFFLINE" in text
        assert "    ghost  on old" in text

    async def test_empty_sources_say_none(self, main: _Main) -> None:
        main._registry = None

        text = await info.show_registry(_ctx(main), "")

        assert text.count("    (none)") == 3


class TestMigrate:
    async def test_too_few_arguments_shows_usage(self, main: _Main) -> None:
        assert (await agent_cmds.migrate_agent_cmd(_ctx(main), "sensor")).startswith("Usage:")

    @pytest.mark.parametrize(
        ("result", "reply"),
        [
            ({"success": True, "message": "moved"}, "[OK] moved"),
            ({"success": False}, "[FAIL] {'success': False}"),
            (RuntimeError("node offline"), "Migrate failed: node offline"),
        ],
    )
    async def test_the_outcome_is_reported(self, main: _Main, result: Any, reply: str) -> None:
        main.migrate_result = result

        assert await agent_cmds.migrate_agent_cmd(_ctx(main), "sensor rpi") == reply


class TestRestart:
    async def test_a_name_is_required(self, main: _Main) -> None:
        assert (
            await agent_cmds.restart_agent(_ctx(main), "") == "Usage: /agents restart <agent-name>"
        )

    async def test_a_remote_agent_is_restarted_by_its_node(self, main: _Main) -> None:
        main.spawn_registry = {"cam": {"node": "rpi"}}

        text = await agent_cmds.restart_agent(_ctx(main), "cam")

        assert main.published == [("nodes/rpi/restart_agent", {"name": "cam"})]
        assert "on node 'rpi'" in text

    async def test_an_unknown_local_agent_is_refused(self, main: _Main) -> None:
        text = await agent_cmds.restart_agent(_ctx(main), "ghost")

        assert text == "Agent 'ghost' not found in spawn registry."

    async def test_a_local_agent_is_stopped_then_respawned_trusted(self, main: _Main) -> None:
        running = _Agent("weather")
        registry = _Registry(running)
        main._registry = registry
        main.spawn_registry = {"weather": {"type": "native", "node": ""}}

        text = await agent_cmds.restart_agent(_ctx(main), "weather")

        assert text == "Agent 'weather' restarted locally."
        assert running.commands == ["stop"]
        assert registry.unregistered == ["id-weather"]
        ((_kind, (config, kwargs)),) = [c for c in main.calls if c[0] == "spawn"]
        assert config["replace"] is True
        assert kwargs == {"save": True, "from_registry": True}


class TestStart:
    async def test_a_name_is_required(self, main: _Main) -> None:
        assert await agent_cmds.start_agent(_ctx(main), "") == "Usage: /start <agent-name>"

    async def test_a_remote_agent_is_pointed_at_restart(self, main: _Main) -> None:
        main.spawn_registry = {"cam": {"node": "rpi"}}

        assert "Use /agents restart cam" in await agent_cmds.start_agent(_ctx(main), "cam")

    async def test_without_a_registry_or_agent_it_says_so(self, main: _Main) -> None:
        assert await agent_cmds.start_agent(_ctx(main), "x") == "Agent 'x' not found locally."
        main._registry = None
        assert await agent_cmds.start_agent(_ctx(main), "x") == "No actor registry available."

    async def test_a_running_agent_is_not_started_again(self, main: _Main) -> None:
        main._registry = _Registry(_Agent("weather"))

        text = await agent_cmds.start_agent(_ctx(main), "weather")

        assert text == "Agent 'weather' is not stopped (state: RUNNING)."

    async def test_a_stopped_agent_is_started(self, main: _Main) -> None:
        stopped = _Agent("weather", ActorState.STOPPED)
        main._registry = _Registry(stopped)

        assert await agent_cmds.start_agent(_ctx(main), "weather") == "Agent 'weather' started."
        assert stopped.commands == ["start"]

    async def test_a_refused_or_failing_start_is_reported(self, main: _Main) -> None:
        stopped = _Agent("weather", ActorState.STOPPED)
        main._registry = _Registry(stopped)
        stopped.accept = False

        assert await agent_cmds.start_agent(_ctx(main), "weather") == "'weather' refused start."

        stopped.error = RuntimeError("setup crashed")
        text = await agent_cmds.start_agent(_ctx(main), "weather")

        assert text == "Failed to start 'weather': setup crashed"


class TestStopAndDelete:
    async def test_a_name_is_required(self, main: _Main) -> None:
        assert await agent_cmds.stop_agent(_ctx(main), "") == "Usage: /agents stop <agent-name>"

    @pytest.mark.parametrize("handler", [agent_cmds.delete_agent, agent_cmds.remove_agent])
    async def test_delete_goes_through_mains_permanent_delete(
        self, main: _Main, handler: Any
    ) -> None:
        text = await handler(_ctx(main), "weather")

        assert ("delete", "weather") in main.calls
        assert text.startswith("Agent 'weather' permanently deleted")

    async def test_a_failed_delete_is_reported(self, main: _Main) -> None:
        main.delete_error = RuntimeError("locked")

        text = await agent_cmds.delete_agent(_ctx(main), "weather")

        assert text == "Delete of 'weather' failed: locked"

    async def test_a_remote_agent_is_stopped_by_its_node(self, main: _Main) -> None:
        main.spawn_registry = {"cam": {"node": "rpi"}}

        text = await agent_cmds.stop_agent(_ctx(main), "cam")

        assert main.published == [("nodes/rpi/stop", {"name": "cam"})]
        assert text.startswith("Stop signal sent to 'cam' on node 'rpi'.")

    async def test_a_local_agent_is_stopped_and_recorded(self, main: _Main) -> None:
        running = _Agent("weather")
        main._registry = _Registry(running)

        text = await agent_cmds.stop_agent(_ctx(main), "weather")

        assert running.commands == ["stop"]
        assert text.startswith("Agent 'weather' stopped.")
        assert ("record", ("weather", "manually stopped via /agents")) in main.calls

    async def test_a_protected_agent_refuses(self, main: _Main) -> None:
        running = _Agent("monitor")
        running.accept = False
        main._registry = _Registry(running)

        text = await agent_cmds.stop_agent(_ctx(main), "monitor")

        assert text == "'monitor' refused stop (it may be protected or essential)."

    async def test_an_unknown_local_agent_is_not_found(self, main: _Main) -> None:
        assert (
            await agent_cmds.stop_agent(_ctx(main), "ghost") == "Agent 'ghost' not found locally."
        )


class TestListAgents:
    async def test_nothing_known_explains_when_agents_appear(self, main: _Main) -> None:
        assert "No agents found matching 'x'." in await agent_cmds.list_agents(_ctx(main), "x")

    async def test_each_agent_shows_its_state_and_schemas(self, main: _Main) -> None:
        main.capability_list = [
            {
                "name": "weather",
                "running": True,
                "spawnable": False,
                "node": "rpi",
                "description": "forecasts",
                "capabilities": ["weather"],
                "input_schema": {"city": "str"},
                "output_schema": {"temp": "float"},
            },
            {
                "name": "gmail",
                "running": False,
                "spawnable": True,
                "description": "mail",
                "capabilities": [],
                "input_schema": {},
                "output_schema": {},
            },
            {
                "name": "old",
                "running": False,
                "spawnable": False,
                "description": "stopped",
                "capabilities": [],
                "input_schema": {},
                "output_schema": {},
            },
        ]

        text = await agent_cmds.list_agents(_ctx(main), "")

        assert "🟢 [weather] on rpi" in text
        assert "    input       : {'city': 'str'}" in text
        assert "📦 [gmail]" in text
        assert "    spawnable   : yes — @catalog spawn gmail" in text
        assert "🔴 [old]" in text
