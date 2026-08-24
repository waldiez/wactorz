"""Putting an agent on another machine.

Two configs leave this path and they are deliberately different. What goes over
the wire carries synthesized bridge code, because the remote runner only knows
how to run DynamicAgent-shaped source. What is written to the spawn registry
does not, so the bridge is regenerated on every spawn rather than accumulating
in the registry and being carried home again on a migrate-back. A move that
saved the wire config instead would look correct and would quietly grow the
registry with code nobody wrote.

Package installs happen before the spawn, over SSH, through the installer
agent. The node's address is looked for in three places, and the install is
allowed to fail: an agent that spawns without its packages reports a real error
later, whereas one that never spawns because a `pip` call timed out is a silent
loss. The payload carries the node's name rather than its credentials, so the
installer resolves the target itself.
"""

import asyncio
import json
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService
from wactorz.core.actor import MessageType


class _Installer:
    """The installer agent, holding whatever it recorded at deploy time."""

    def __init__(self, hosts: dict[str, str] | None = None) -> None:
        self.name = "installer"
        self.actor_id = "installer-id"
        self._hosts = hosts or {}

    def recall(self, key: str) -> str | None:
        return self._hosts.get(key)


class _Registry:
    """The actor registry, which here only ever answers about the installer."""

    def __init__(self, installer: _Installer | None) -> None:
        self._installer = installer

    def find_by_name(self, name: str) -> _Installer | None:
        return self._installer if name == "installer" else None


class _Main:
    """A MainActor with the surface `_spawn_remote` touches, stubbed."""

    def __init__(
        self,
        *,
        known_nodes: dict[str, dict[str, Any]] | None = None,
        spawn_registry: dict[str, dict[str, Any]] | None = None,
        installer: _Installer | None = None,
        install_result: dict[str, Any] | None = None,
        install_hangs: bool = False,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        main._known_nodes = dict(known_nodes or {})
        setattr(main, "_registry", _Registry(installer))

        self.published: list[tuple[str, Any, dict[str, Any]]] = []
        self.desired_state: list[tuple[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        self.sent: list[tuple[str, Any, dict[str, Any]]] = []
        self._spawn_registry = dict(spawn_registry or {})
        main._result_futures = {}

        async def _publish(topic: str, payload: Any, **kw: Any) -> None:
            self.published.append((topic, payload, kw))

        async def _desired(node: str, cfg: Any = None) -> None:
            self.desired_state.append((node, cfg))

        async def _send(actor_id: str, kind: Any, payload: dict[str, Any]) -> None:
            self.sent.append((actor_id, kind, payload))
            if install_hangs:
                return
            future = main._result_futures.get(payload["_task_id"])
            if future is not None and not future.done():
                future.set_result(install_result or {"success": True})

        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_update_node_desired_state", _desired)
        setattr(main.spawns, "_get_spawn_registry", lambda: dict(self._spawn_registry))
        setattr(main.spawns, "_save_to_spawn_registry", self.saved.append)
        setattr(main, "send", _send)
        self.actor = main

    async def spawn(self, config: dict[str, Any], node: str = "rpi", save: bool = True) -> None:
        await self.actor.spawns._spawn_remote(config, node, save)

    def published_to(self, suffix: str) -> tuple[str, Any, dict[str, Any]]:
        """The one publish whose topic ends in `suffix`."""
        matches = [row for row in self.published if row[0].endswith(suffix)]
        assert len(matches) == 1, f"expected one {suffix} publish, got {len(matches)}"
        return matches[0]


def llm_agent(**over: Any) -> dict[str, Any]:
    """A config the runner cannot run until bridge code is synthesized for it."""
    return {"name": "helper", "type": "llm", "system_prompt": "Be brief.", **over}


class TestWhatGoesOverTheWireVersusWhatIsSaved:
    """The registry keeps the clean config; only the wire gets bridge code."""

    async def test_the_wire_config_carries_synthesized_code(self) -> None:
        main = _Main()

        await main.spawn(llm_agent())

        _, payload, _ = main.published_to("/spawn")
        assert payload["code"]

    async def test_the_saved_config_does_not(self) -> None:
        # Saving the wire config instead would look identical from the outside
        # and would carry the bridge home again on a migrate-back, as code
        # nobody wrote and nobody can edit.
        main = _Main()

        await main.spawn(llm_agent())

        assert main.saved == [llm_agent()]

    async def test_the_callers_config_is_left_alone(self) -> None:
        # The same dict is handed to several writers, so synthesis copies.
        main = _Main()
        config = llm_agent()

        await main.spawn(config)

        assert config == llm_agent()

    async def test_nothing_is_saved_when_the_caller_says_not_to(self) -> None:
        main = _Main()

        await main.spawn(llm_agent(), save=False)

        assert not main.saved


class TestReachingTheNode:
    """Three places know a node's address, tried in order of how much they know."""

    async def test_the_heartbeat_table_is_asked_first(self) -> None:
        main = _Main(
            known_nodes={"rpi": {"host": "10.0.0.9"}},
            spawn_registry={"other": {"node": "rpi", "host": "10.0.0.stale"}},
            installer=_Installer(),
        )

        await main.spawn(llm_agent(install=["httpx"]))

        assert main.sent[0][2]["host"] == "10.0.0.9"

    async def test_the_spawn_registry_answers_when_the_table_does_not(self) -> None:
        main = _Main(
            spawn_registry={"other": {"node": "rpi", "host": "10.0.0.7"}},
            installer=_Installer(),
        )

        await main.spawn(llm_agent(install=["httpx"]))

        assert main.sent[0][2]["host"] == "10.0.0.7"

    async def test_what_the_installer_recorded_answers_last(self) -> None:
        main = _Main(installer=_Installer({"node_host_rpi": "10.0.0.5"}))

        await main.spawn(llm_agent(install=["httpx"]))

        assert main.sent[0][2]["host"] == "10.0.0.5"

    async def test_a_missing_installer_is_reported_as_such(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The node's address is known, so telling the operator to go and find
        # one sends them after the wrong thing: what is missing is the agent
        # that does the installing.
        main = _Main(known_nodes={"rpi": {"host": "10.0.0.9"}}, installer=None)

        with caplog.at_level("WARNING"):
            await main.spawn(llm_agent(install=["httpx"]))

        assert "installer not found" in caplog.text
        assert "No host known" not in caplog.text

    async def test_an_unreachable_node_says_the_address_is_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        main = _Main(installer=_Installer())

        with caplog.at_level("WARNING"):
            await main.spawn(llm_agent(install=["httpx"]))

        assert "No host known" in caplog.text
        assert "installer not found" not in caplog.text

    async def test_an_unreachable_node_still_gets_the_agent(self) -> None:
        # Nothing knows the address, so the packages cannot be installed. The
        # spawn goes ahead: the agent will report a real import error, which is
        # findable, rather than never arriving at all.
        main = _Main(installer=_Installer())

        await main.spawn(llm_agent(install=["httpx"]))

        assert not main.sent
        assert main.published_to("/spawn")[0] == "nodes/rpi/spawn"


class TestInstallingPackagesFirst:
    """Installs run before the spawn, and are never allowed to prevent it."""

    async def test_a_comma_separated_string_is_a_list_of_packages(self) -> None:
        main = _Main(known_nodes={"rpi": {"host": "h"}}, installer=_Installer())

        await main.spawn(llm_agent(install="httpx, pandas numpy"))

        assert main.sent[0][2]["packages"] == ["httpx", "pandas", "numpy"]

    async def test_the_install_payload_carries_no_credentials(self) -> None:
        # The node's name is enough for the installer to resolve its own
        # configured target. Sending a password back to the agent that already
        # holds it would put it on the wire for no reason.
        main = _Main(known_nodes={"rpi": {"host": "h"}}, installer=_Installer())

        await main.spawn(llm_agent(install=["httpx"]))

        payload = main.sent[0][2]
        assert payload["node_name"] == "rpi"
        assert not {"password", "key", "secret", "credentials"} & set(payload)

    async def test_it_is_sent_as_a_task_to_the_installer(self) -> None:
        main = _Main(known_nodes={"rpi": {"host": "h"}}, installer=_Installer())

        await main.spawn(llm_agent(install=["httpx"]))

        actor_id, kind, _ = main.sent[0]
        assert actor_id == "installer-id"
        # Compared by value: under xdist the enum class can be imported twice,
        # and two members that are equal are then not the same object.
        assert kind == MessageType.TASK

    async def test_a_failed_install_does_not_stop_the_spawn(self) -> None:
        main = _Main(
            known_nodes={"rpi": {"host": "h"}},
            installer=_Installer(),
            install_result={"success": False, "error": "no disk space"},
        )

        await main.spawn(llm_agent(install=["httpx"]))

        assert main.published_to("/spawn")[0] == "nodes/rpi/spawn"

    async def test_the_waiting_future_is_always_dropped(self) -> None:
        # It is keyed by a fresh task id each time; keeping them would grow the
        # dict for the life of the process.
        main = _Main(known_nodes={"rpi": {"host": "h"}}, installer=_Installer())

        await main.spawn(llm_agent(install=["httpx"]))

        assert not main.actor._result_futures

    async def test_a_config_with_no_packages_never_asks_the_installer(self) -> None:
        main = _Main(known_nodes={"rpi": {"host": "h"}}, installer=_Installer())

        await main.spawn(llm_agent())

        assert not main.sent


class TestTellingEveryoneItHappened:
    """One spawn produces three writes, and all of them matter."""

    async def test_the_spawn_is_retained_so_a_rebooted_node_still_sees_it(self) -> None:
        main = _Main()

        await main.spawn(llm_agent())

        topic, _, options = main.published_to("/spawn")
        assert topic == "nodes/rpi/spawn"
        assert options["retain"] is True
        assert options["qos"] == 1

    async def test_the_nodes_desired_state_is_updated(self) -> None:
        # What the runner reconciles against after a reboot, so it gets the
        # wire config too — otherwise the agent comes back without its code.
        main = _Main()

        await main.spawn(llm_agent())

        node, config = main.desired_state[0]
        assert node == "rpi"
        assert config["code"]

    async def test_the_dashboard_is_told(self) -> None:
        main = _Main()

        await main.spawn(llm_agent())

        _, payload, _ = main.published_to("/logs")
        assert payload["type"] == "spawned"
        assert payload["child_name"] == "helper"
        assert payload["node"] == "rpi"


class TestSynthesizingTheBridge:
    """`_inject_llm_bridge_code` decides whether a config needs code at all."""

    def _inject(self, config: dict[str, Any]) -> dict[str, Any]:
        return _Main().actor.spawns._inject_llm_bridge_code(config)

    def test_an_llm_config_without_code_gets_some(self) -> None:
        assert self._inject(llm_agent())["code"]

    def test_a_config_that_brought_its_own_code_is_untouched(self) -> None:
        config = llm_agent(code="print(1)")

        assert self._inject(config) is config

    def test_a_config_of_another_type_is_untouched(self) -> None:
        # The runner already knows how to run these.
        config = {"name": "sensor", "type": "ha_actuator"}

        assert self._inject(config) is config

    def test_the_system_prompt_is_embedded_as_a_literal(self) -> None:
        # Interpolating it raw would end the string on the first quote and turn
        # a prompt into whatever followed it.
        config = llm_agent(system_prompt='He said "stop"; import os')

        assert json.dumps('He said "stop"; import os') in self._inject(config)["code"]

    def test_it_describes_itself_when_the_caller_did_not(self) -> None:
        assert "LLM-driven agent" in self._inject(llm_agent())["description"]

    def test_a_description_the_caller_gave_survives(self) -> None:
        config = llm_agent(description="Watches the kettle")

        assert self._inject(config)["description"] == "Watches the kettle"

    @pytest.mark.parametrize("given", [0, None, ""])
    def test_an_empty_history_bound_falls_back_to_the_default(self, given: Any) -> None:
        # A zero-length history would leave the agent unable to hold a
        # conversation at all.
        code = self._inject(llm_agent(max_history=given))["code"]

        assert "32" in code


class TestWhenTheInstallHangs:
    """A slow install is bounded; the spawn goes ahead without it."""

    async def test_the_spawn_survives_an_installer_that_never_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main(known_nodes={"rpi": {"host": "h"}}, installer=_Installer(), install_hangs=True)
        real_wait_for = asyncio.wait_for

        async def _immediate(awaitable: Any, timeout: float) -> Any:
            return await real_wait_for(awaitable, timeout=0.01)

        monkeypatch.setattr(asyncio, "wait_for", _immediate)

        await main.spawn(llm_agent(install=["httpx"]))

        assert main.published_to("/spawn")[0] == "nodes/rpi/spawn"
        assert not main.actor._result_futures
