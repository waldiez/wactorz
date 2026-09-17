"""How the terminal reaches an agent, and how it deploys a node.

An agent named with `@` is answered by the most direct route it offers: a
conversational agent's own `chat`, a generated agent's `handle_task`, a
generated agent's model, and finally a task sent through main. An agent that is
not local is looked for on the remote nodes and asked over MQTT, with a bounded
wait.

`/deploy` uses the configured target only. A target with no host is looked up
by one mDNS name rather than a sweep of the network, and a deploy that fails
says why.
"""

import asyncio
import json
import types
from dataclasses import replace
from typing import Any, cast

import pytest

from wactorz import config
from wactorz.agents.main import MainActor
from wactorz.config import DeployTarget
from wactorz.interfaces.chat import cli
from wactorz.interfaces.chat.cli import CLIInterface, resolve_host


class _Registry:
    def __init__(self, **agents: Any) -> None:
        self._agents = agents

    def find_by_name(self, name: str) -> Any:
        return self._agents.get(name)

    def all_actors(self) -> list[Any]:
        return [type("A", (), {"name": n})() for n in self._agents]


class _Main:
    def __init__(self) -> None:
        self._registry: _Registry | None = _Registry()
        self._known_nodes: dict[str, Any] = {}
        self._mqtt_broker = "broker"
        self._mqtt_port = 1883
        self.actor_id = "main-id"
        self.published: list[tuple[str, Any]] = []
        self.delegated: Any = None
        self.installer_result: dict[str, Any] = {"success": True}
        self.installer_calls: list[dict[str, Any]] = []

    async def delegate_task(self, name: str, message: str, timeout: float = 60.0) -> Any:
        return self.delegated

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))

    async def delegate_to_installer(self, payload: dict[str, Any], timeout: float) -> Any:
        self.installer_calls.append(payload)
        return self.installer_result


def _cli(main: _Main) -> CLIInterface:
    return CLIInterface(cast(MainActor, main))


class _Chatty:
    def __init__(self) -> None:
        #: Marks an LLM agent, which the CLI asks through its own `chat`.
        self._conversation_history: list[Any] = []

    async def chat(self, message: str) -> str:
        return f"chat: {message}"


class _Generated:
    def __init__(self, result: Any = None, llm: bool = False, raises: bool = False) -> None:
        self._result = result
        self._raises = raises
        self._api = types.SimpleNamespace()
        self._fn_handle_task = self._handle if result is not None or raises else None
        self._llm_provider = object() if llm else None
        if llm:

            class _Llm:
                async def chat(self, message: str) -> str:
                    return f"llm: {message}"

            self._api.llm = _Llm()

    async def _handle(self, api: Any, payload: dict[str, Any]) -> Any:
        if self._raises:
            raise RuntimeError("code crashed")
        return self._result


class _OnlyChat:
    async def chat(self, message: str) -> str:
        return "plain chat"


class TestLocalAgents:
    async def test_without_a_registry_or_agent_it_says_so(self) -> None:
        main = _Main()
        main._registry = _Registry(weather=object())

        assert (await _cli(main)._get_agent_response("ghost", "hi")).startswith(
            "[error] No agent named 'ghost'. Available: weather"
        )
        main._registry = None
        assert await _cli(main)._get_agent_response("x", "hi") == "[error] No registry available."

    @pytest.mark.parametrize(
        ("agent", "reply"),
        [
            (_Chatty(), "chat: hi"),
            (_Generated(result={"answer": "42"}), "42"),
            (_Generated(result={"other": 1}), "{'other': 1}"),
            (_Generated(result="plain"), "plain"),
            (_Generated(result=""), "[a] No response"),
            (_Generated(llm=True), "llm: hi"),
            (_OnlyChat(), "plain chat"),
            (_Generated(raises=True), "[error] a failed: code crashed"),
        ],
    )
    async def test_each_kind_of_agent_is_asked_its_own_way(self, agent: Any, reply: str) -> None:
        main = _Main()
        main._registry = _Registry(a=agent)

        assert await _cli(main)._get_agent_response("a", "hi") == reply

    @pytest.mark.parametrize(
        ("delegated", "reply"),
        [
            ({"reply": "via main"}, "via main"),
            ({"x": 1}, "{'x': 1}"),
            (None, "[a] Task sent (no synchronous response)"),
        ],
    )
    async def test_anything_else_is_sent_a_task_through_main(
        self, delegated: Any, reply: str
    ) -> None:
        main = _Main()
        main._registry = _Registry(a=object())
        main.delegated = delegated

        assert await _cli(main)._get_agent_response("a", "hi") == reply


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _Client:
    def __init__(self, payload: bytes | None) -> None:
        self._payload = payload
        self.subscribed: list[str] = []

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Client":
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._stream()

    async def _stream(self) -> Any:
        if self._payload is None:
            await asyncio.Event().wait()
        yield _Message(self._payload or b"")


@pytest.fixture(name="quick")
def quick_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the subscribe pause and the reply wait; nothing real is waited on."""
    real_sleep = asyncio.sleep
    real_wait_for = asyncio.wait_for

    async def _sleep(_delay: float) -> None:
        await real_sleep(0)

    async def _wait_for(awaitable: Any, timeout: float | None) -> Any:
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(cli.asyncio, "sleep", _sleep)
    monkeypatch.setattr(cli.asyncio, "wait_for", _wait_for)


class TestRemoteAgents:
    async def test_an_agent_on_no_node_is_reported(self) -> None:
        main = _Main()

        assert (await _cli(main)._get_remote_agent_response("cam", "hi")).endswith(
            "No remote nodes connected."
        )
        main._known_nodes = {"rpi": {"agents": ["sensor"]}}
        assert (await _cli(main)._get_remote_agent_response("cam", "hi")).endswith(
            "Remote agents: sensor"
        )

    @pytest.mark.parametrize(
        ("payload", "reply"),
        [
            (json.dumps({"reply": "snapshot taken"}).encode(), "snapshot taken"),
            (json.dumps({"error": "no camera"}).encode(), "[error] no camera"),
            (json.dumps({"n": 1}).encode(), "{'n': 1}"),
            (json.dumps("just text").encode(), "just text"),
            (json.dumps([1, 2]).encode(), "[1, 2]"),
            (b"not json", "not json"),
        ],
    )
    async def test_the_reply_is_read_from_the_reply_topic(
        self, monkeypatch: pytest.MonkeyPatch, quick: None, payload: bytes, reply: str
    ) -> None:
        main = _Main()
        main._known_nodes = {"rpi": {"agents": ["cam"]}}
        client = _Client(payload)
        monkeypatch.setattr(cli, "mqtt_client", client)

        assert await _cli(main)._get_remote_agent_response("cam", "snap") == reply
        (topic, task) = main.published[0]
        assert topic == "agents/by-name/cam/task"
        assert client.subscribed == [task["_reply_topic"]]

    async def test_a_silent_node_times_out(
        self, monkeypatch: pytest.MonkeyPatch, quick: None
    ) -> None:
        main = _Main()
        main._known_nodes = {"rpi": {"agents": ["cam"]}}
        monkeypatch.setattr(cli, "mqtt_client", _Client(None))

        assert await _cli(main)._get_remote_agent_response("cam", "snap") == (
            "[timeout] cam on rpi did not respond within 30s"
        )

    async def test_a_broker_that_refuses_is_reported(self, quick: None) -> None:
        main = _Main()
        main._known_nodes = {"rpi": {"agents": ["cam"]}}

        reply = await _cli(main)._get_remote_agent_response("cam", "snap")

        assert reply.startswith("[error] no broker in tests")


class TestDeploy:
    @staticmethod
    def _targets(monkeypatch: pytest.MonkeyPatch, *targets: DeployTarget) -> None:
        monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, deploy_targets=tuple(targets)))

    async def test_an_unconfigured_node_is_refused_with_help(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._targets(monkeypatch)

        await _cli(_Main())._deploy("rpi")

        assert capsys.readouterr().out.startswith("[error]")

    async def test_a_configured_node_is_deployed_through_the_installer(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._targets(
            monkeypatch, DeployTarget(name="rpi", host="10.0.0.5", user="pi", broker="10.0.0.1")
        )
        main = _Main()

        await _cli(main)._deploy("rpi")

        assert main.installer_calls == [
            {
                "action": "node_deploy",
                "host": "10.0.0.5",
                "node_name": "rpi",
                "broker": "10.0.0.1",
                "port": 1883,
            }
        ]
        assert "Node 'rpi' is live!" in capsys.readouterr().out

    async def test_a_host_is_found_by_mdns_and_a_failure_is_explained(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._targets(monkeypatch, DeployTarget(name="rpi", host="", user="pi"))

        async def _found(name: str) -> str:
            return "10.0.0.9"

        monkeypatch.setattr(cli, "resolve_host", _found)
        main = _Main()
        main.installer_result = {"success": False, "error": "No module named asyncssh"}

        await _cli(main)._deploy("rpi")

        out = capsys.readouterr().out
        assert "Found via mDNS: rpi.local → 10.0.0.9" in out
        assert "[error] Deploy failed: No module named asyncssh" in out
        assert "Hint: pip install asyncssh" in out
        assert main.installer_calls[0]["broker"] == "localhost"

    async def test_an_unresolvable_host_or_missing_installer_stops_the_deploy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._targets(
            monkeypatch,
            DeployTarget(name="rpi", host="", user="pi"),
            DeployTarget(name="box", host="h", user="u"),
        )

        async def _missing(name: str) -> None:
            return None

        monkeypatch.setattr(cli, "resolve_host", _missing)
        await _cli(_Main())._deploy("rpi")
        cli_without_installer = CLIInterface(cast(MainActor, object()))
        await cli_without_installer._deploy("box")

        out = capsys.readouterr().out
        assert "Set DEPLOY_RPI_HOST in your environment." in out
        assert "delegate_to_installer not available" in out

    async def test_resolving_a_name_that_does_not_exist_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(name: str) -> str:
            raise OSError("unknown host")

        monkeypatch.setattr(cli.socket, "gethostbyname", _fail)

        assert await resolve_host("nowhere.local") is None
