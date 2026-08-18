"""Bringing an agent home from a remote node.

Migrating an agent back to main asks its node to send everything it has: the
spawn config and the persistent state. That arrives on `nodes/+/state_return`,
and main spawns the agent locally from it.

The config in that message is executed — it carries the agent's code. What makes
that acceptable is the token: main mints one per migration it started, and only
a message quoting that exact token is acted on. Tokens are single-use and expire,
so a replayed message spawns nothing.
"""

import asyncio
import json
import logging
import time
from typing import Any

import pytest

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.migration import Migration
from wactorz.agents.nodes import NodeManager
from wactorz.core.actor import ActorState


class _Message:
    def __init__(self, payload: bytes | None, topic: str = "nodes/rpi/state_return") -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self._messages:
            yield message
        self._on_drained()


class _Broker:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self.client = _Client(messages, on_drained)

    def __call__(self, _host: str, _port: int) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _Run:
    """One drive of the listener, and what it tried to spawn."""

    def __init__(self, main: MainActor) -> None:
        self.main = main
        self.spawned: list[dict[str, Any]] = []
        self.spawn_kwargs: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.spawn_error: Exception | None = None

    @property
    def only_spawn(self) -> dict[str, Any]:
        assert len(self.spawned) == 1, f"expected one spawn, got {self.spawned}"
        return self.spawned[0]

    @property
    def messages(self) -> list[str]:
        return [str(n.get("message", "")) for n in self.notifications]


TOKEN = "a1b2c3d4"


def config(**over: Any) -> dict[str, Any]:
    return {"name": "collector", "type": "dynamic", "code": "print(1)", **over}


def state_return(token: str = TOKEN, **over: Any) -> _Message:
    """A node returning an agent, as its runner publishes it."""
    body: dict[str, Any] = {
        "agent": "collector",
        "return_token": token,
        "config": config(),
        "state": {"count": 7},
        **over,
    }
    return _Message(json.dumps(body).encode())


async def run_listener(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[_Message],
    *,
    pending: dict[str, dict[str, Any]] | None = None,
    spawn_error: Exception | None = None,
) -> _Run:
    """Drive the real listener over `messages` until they are exhausted."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.migration = Migration(main, main.nodes)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883
    main.migration.pending_returns = dict(pending or {})

    run = _Run(main)
    run.spawn_error = spawn_error

    async def _spawn(cfg: dict[str, Any], **kwargs: Any) -> None:
        run.spawned.append(cfg)
        run.spawn_kwargs.append(kwargs)
        if spawn_error is not None:
            raise spawn_error

    setattr(main, "_spawn_from_config", _spawn)
    setattr(main, "_queue_notification", run.notifications.append)
    setattr(main, "_restore_earned_trust", lambda name, cfg: False)

    def _stop() -> None:
        main.state = ActorState.STOPPED

    broker = _Broker(messages, _stop)
    monkeypatch.setattr("wactorz.agents.migration.mqtt_client", broker)

    await asyncio.wait_for(main._state_return_listener(), timeout=5)
    return run


def waiting(token: str = TOKEN, age_s: float = 0.0) -> dict[str, dict[str, Any]]:
    """A migration main started `age_s` ago and is still waiting on."""
    return {
        token: {"agent_name": "collector", "from_node": "rpi", "started_at": time.time() - age_s}
    }


class TestTheToken:
    """The config in a state_return is code that gets run, so only a message
    answering a migration main itself started is acted on."""

    async def test_a_matching_token_spawns_the_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [state_return()], pending=waiting())

        assert run.only_spawn["name"] == "collector"

    async def test_an_unknown_token_spawns_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [state_return(token="forged")], pending=waiting())

        assert not run.spawned

    async def test_no_token_spawns_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [state_return(token="")], pending=waiting())

        assert not run.spawned

    async def test_nothing_pending_means_nothing_spawns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(monkeypatch, [state_return()], pending={})

        assert not run.spawned

    async def test_a_token_works_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Replaying the message must not spawn a second time. The token is
        # consumed by the first, so the second finds nothing pending.
        run = await run_listener(monkeypatch, [state_return(), state_return()], pending=waiting())

        assert len(run.spawned) == 1

    async def test_an_expired_token_spawns_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A node that never replied leaves the token behind; it stops being
        # usable rather than waiting indefinitely.
        run = await run_listener(monkeypatch, [state_return()], pending=waiting(age_s=301))

        assert not run.spawned

    async def test_a_token_just_inside_the_window_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(monkeypatch, [state_return()], pending=waiting(age_s=299))

        assert len(run.spawned) == 1

    async def test_an_expired_token_is_forgotten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Otherwise a node that never answers grows the map for the life of
        # the process.
        run = await run_listener(monkeypatch, [state_return()], pending=waiting(age_s=301))

        assert not run.main.migration.pending_returns

    async def test_a_refusal_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="wactorz.agents.migration"):
            await run_listener(monkeypatch, [state_return(token="forged")], pending=waiting())

        assert "token" in caplog.text


class TestTheConfigThatIsSpawned:
    async def test_the_remote_node_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # It is coming home; leaving the node on would send it straight back.
        run = await run_listener(
            monkeypatch, [state_return(config=config(node="rpi"))], pending=waiting()
        )

        assert "node" not in run.only_spawn

    async def test_the_returned_state_is_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [state_return()], pending=waiting())

        assert run.only_spawn["_initial_state"] == {"count": 7}

    async def test_a_stale_initial_state_in_the_config_is_replaced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The state field is what the node has now; one embedded in the config
        # is whatever it was carrying at spawn time.
        run = await run_listener(
            monkeypatch,
            [state_return(config=config(_initial_state={"count": 0}))],
            pending=waiting(),
        )

        assert run.only_spawn["_initial_state"] == {"count": 7}

    async def test_no_state_leaves_the_key_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [state_return(state={})], pending=waiting())

        assert "_initial_state" not in run.only_spawn

    async def test_it_replaces_any_local_leftover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A previous local instance of the same name would otherwise collide.
        run = await run_listener(monkeypatch, [state_return()], pending=waiting())

        assert run.only_spawn["replace"] is True

    async def test_a_config_without_a_name_takes_the_agent_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = config()
        cfg.pop("name")

        run = await run_listener(monkeypatch, [state_return(config=cfg)], pending=waiting())

        assert run.only_spawn["name"] == "collector"

    async def test_a_missing_config_spawns_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # There is nothing to build the agent from.
        run = await run_listener(monkeypatch, [state_return(config={})], pending=waiting())

        assert not run.spawned


class TestTheGeneratedBridgeCode:
    """An LLM agent gets synthesized code on the way out to a node.

    Locally the type routes to the real class and the code is ignored, so
    carrying it back would bloat the spawn registry with something misleading
    to anyone reading it.
    """

    MARKER = "# Auto-generated LLM bridge\nprint(1)"

    async def test_it_is_dropped_for_an_llm_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = config(type="llm", code=self.MARKER)

        run = await run_listener(monkeypatch, [state_return(config=cfg)], pending=waiting())

        assert "code" not in run.only_spawn

    async def test_hand_written_code_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only the synthesized template carries the marker, so a user's own
        # code survives even on an llm-typed agent.
        cfg = config(type="llm", code="print('mine')")

        run = await run_listener(monkeypatch, [state_return(config=cfg)], pending=waiting())

        assert run.only_spawn["code"] == "print('mine')"

    async def test_a_dynamic_agent_keeps_its_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = config(type="dynamic", code=self.MARKER)

        run = await run_listener(monkeypatch, [state_return(config=cfg)], pending=waiting())

        assert run.only_spawn["code"] == self.MARKER


class TestReportingTheOutcome:
    async def test_a_success_is_announced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [state_return()], pending=waiting())

        assert run.notifications[0]["severity"] == "info"
        assert "succeeded" in run.messages[0]

    async def test_a_failed_spawn_is_announced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The agent is now on neither machine, so silence would lose it.
        run = await run_listener(
            monkeypatch,
            [state_return()],
            pending=waiting(),
            spawn_error=RuntimeError("no such module"),
        )

        assert run.notifications[0]["severity"] == "warning"
        assert "no such module" in run.messages[0]

    async def test_a_failed_spawn_does_not_end_the_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = {**waiting("t1"), **waiting("t2")}

        run = await run_listener(
            monkeypatch,
            [state_return("t1"), state_return("t2")],
            pending=pending,
            spawn_error=RuntimeError("boom"),
        )

        assert len(run.notifications) == 2

    async def test_the_announcement_names_where_it_came_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(monkeypatch, [state_return()], pending=waiting())

        assert "rpi" in run.messages[0]
        assert "collector" in run.messages[0]


class TestMessagesThatCannotBeUsed:
    @pytest.mark.parametrize(
        "payload",
        [b"", None, b"{not json", b"[1, 2]", b"42"],
        ids=["empty", "none", "malformed", "list", "number"],
    )
    async def test_they_spawn_nothing(
        self, monkeypatch: pytest.MonkeyPatch, payload: bytes | None
    ) -> None:
        run = await run_listener(monkeypatch, [_Message(payload)], pending=waiting())

        assert not run.spawned

    async def test_a_later_good_message_is_still_acted_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(
            monkeypatch, [_Message(b"{not json"), state_return()], pending=waiting()
        )

        assert len(run.spawned) == 1
