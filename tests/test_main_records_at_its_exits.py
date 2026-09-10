"""main stores a turn where it leaves, as the user had it.

Most of what reaches main is answered without its model — a device, a Home
Assistant question, a refusal — and what it does hand its model is not what the
user typed: the live system state goes in front of it. So main stores nothing
from chat(); each exit of process_user_input and its two siblings stores the
words typed and the reply sent. The dashboard stores its own turns, and an
interface that already shows a turn in the chat says so in the task it sends.
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.delegation import DelegationManager
from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE
from wactorz.core.actor import Message, MessageType
from wactorz.core.persistence import WactorzDB, chat_turn_recorded

NS: dict[str, Any] = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)

PREFIX = (
    "[CURRENT SYSTEM STATE — auto-injected, NOT from the user]\n"
    "Currently running agents (live, just queried): lamp-agent\n"
    "[END SYSTEM STATE]\n\n"
)


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[WactorzDB, Any, None]:
    store = WactorzDB(str(tmp_path / "wactorz.db"))
    monkeypatch.setattr("wactorz.agents.llm_agent.get_db", lambda: store)
    yield store
    store.close()


def _rows(db: WactorzDB) -> list[tuple[str, str, str]]:
    """What was stored, in the order it was written, as (agent, role, content)."""
    rows = sorted(db.query_chat_log(), key=lambda r: r["id"])
    return [(r["agent_name"], r["role"], r["content"]) for r in rows]


async def _nothing(*_a: Any, **_kw: Any) -> None:
    return None


def _main(
    *, intent: str = "OTHER", reply: str = "hi there", delegated: tuple[str, ...] = ()
) -> MainActor:
    """A MainActor wired only as far as its three entry points reach.

    The recording is real: _record_external_exchange, _log_delivered_turn, and
    the _log_chat_turn main overrides. chat() and chat_stream() stand in for the
    base ones down to storing their turn through _log_chat_turn — with the
    prefixed message and the raw reply, as the base ones would.
    """
    m = MainActor.__new__(MainActor)
    m.name = "main"
    m.actor_id = "main-0001"
    m._conversation_history = []
    m.metrics = SimpleNamespace(messages_processed=0)
    m.delegation = DelegationManager(m)

    m._drain_notifications = lambda: ""
    m._rebuild_system_prompt = lambda: None
    m._prefix_with_live_context = lambda t: PREFIX + t
    m._warn_if_pending_plan_collision = lambda _t: None
    m.persist = lambda _k, _v: None
    m._maybe_summarize = _nothing
    m._extract_and_save_facts = _nothing
    m._handle_pending_plan_response = _nothing
    m._mqtt_publish = _nothing
    m.send = _nothing

    async def _classify(_t: str) -> str:
        return intent

    async def _actuate(_t: str, **_kw: Any) -> str:
        await asyncio.sleep(0.01)
        return "the lamp is on"

    async def _chat(message: str, attachments: Any = None) -> str:
        m._conversation_history.append({"role": "user", "content": message})
        m._log_chat_turn(message, reply, ts_user=1.0, ts_reply=2.0)
        return reply

    async def _chat_stream(message: str, attachments: Any = None) -> AsyncIterator[Any]:
        m._conversation_history.append({"role": "user", "content": message})
        yield reply[:3]
        yield reply[3:]
        m._log_chat_turn(message, reply, ts_user=1.0, ts_reply=2.0)
        yield {"input_tokens": 1}

    async def _spawn_blocks(response: str) -> tuple[str, list[Any]]:
        return response, []

    async def _delete_blocks(response: str) -> tuple[str, list[str], list[str]]:
        return response, [], []

    async def _delegate_blocks(response: str, restricted: bool = False) -> Any:
        return response, list(delegated)

    async def _mentions(response: str) -> str:
        return response

    m._classify_intent = _classify
    m._handle_actuate_intent = _actuate
    m.chat = _chat
    m.chat_stream = _chat_stream
    m._process_spawn_commands = _spawn_blocks
    m._process_delete_commands = _delete_blocks
    m._process_delegate_commands = _delegate_blocks
    m._execute_llm_delegations = _mentions
    return m


class TestATurnFromOutsideTheDashboard:
    async def test_answered_without_the_model_is_stored(self, db: WactorzDB) -> None:
        await _main(intent="ACTUATE").process_user_input_restricted("turn on the lamp")

        assert _rows(db) == [
            ("main", "user", "turn on the lamp"),
            ("main", "assistant", "the lamp is on"),
        ]

    async def test_is_stamped_with_when_it_arrived(self, db: WactorzDB) -> None:
        """chat_log orders by time alone, so the question must not share its answer's."""
        await _main(intent="ACTUATE").process_user_input("turn on the lamp")

        asked, answered = sorted(db.query_chat_log(), key=lambda r: r["id"])
        # The actuation took 10ms; a question stamped on the way out would not show it.
        assert answered["ts"] - asked["ts"] >= 0.005

    async def test_answered_by_the_model_is_stored_as_typed(self, db: WactorzDB) -> None:
        await _main(reply="hi there").process_user_input_restricted("hello")

        assert _rows(db) == [("main", "user", "hello"), ("main", "assistant", "hi there")]

    async def test_a_withheld_reply_is_not_what_is_stored(self, db: WactorzDB) -> None:
        m = _main(reply='Done! <spawn>{"name": "x"}</spawn>')

        sent = await m.process_user_input_restricted("make me an agent")

        assert "<spawn>" not in sent
        assert _rows(db) == [
            ("main", "user", "make me an agent"),
            ("main", "assistant", sent),
        ]

    async def test_from_the_api_is_stored_as_sent(self, db: WactorzDB) -> None:
        sent = await _main(reply="hi there").process_user_input("hello")

        assert _rows(db) == [("main", "user", "hello"), ("main", "assistant", sent)]

    async def test_streamed_is_stored_as_sent(self, db: WactorzDB) -> None:
        """Including what was appended after the model finished."""
        m = _main(reply="hi there", delegated=("✅ lamp-agent: on",))

        sent = [c async for c in m.process_user_input_stream("hello") if isinstance(c, str)]

        assert "".join(sent) == "hi there\n✅ lamp-agent: on"
        assert _rows(db) == [("main", "user", "hello"), ("main", "assistant", "".join(sent))]


class TestADashboardTurn:
    async def test_is_left_to_the_dashboard(self, db: WactorzDB) -> None:
        m = _main()

        async def turn() -> None:
            chat_turn_recorded.set(True)
            async for _chunk in m.process_user_input_stream("hello"):
                pass

        await asyncio.create_task(turn())

        assert _rows(db) == []


class TestAnInterfaceTurn:
    @staticmethod
    async def _ask(m: MainActor, **flags: Any) -> None:
        payload = {
            "text": "hello",
            "_via_interface": True,
            "_interface_source": "reachy-mini",
            **flags,
        }
        msg = Message(type=MessageType.TASK, sender_id="reachy-mini-0001", payload=payload)
        await m._handle_interface_request(payload, msg)

    async def test_already_in_the_chat_is_not_stored_again(self, db: WactorzDB) -> None:
        await self._ask(_main(), _chat_recorded=True)

        assert _rows(db) == []
        assert not chat_turn_recorded.get(), "the mark outlived the turn"

    async def test_not_in_the_chat_is_stored(self, db: WactorzDB) -> None:
        await self._ask(_main(reply="hi there"))

        assert _rows(db) == [("main", "user", "hello"), ("main", "assistant", "hi there")]


class _Robot:
    """The side of a Reachy agent that the bridge to main touches."""

    name = "reachy-mini"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_to(self, _target: str, payload: dict[str, Any], timeout: float = 0.0) -> Any:
        self.sent.append(payload)
        return {"text": ""}

    async def log(self, _text: str, level: str = "info") -> None:
        return None


class TestTheBridgeToMain:
    async def test_says_so_when_the_caller_shows_the_turn(self) -> None:
        robot = _Robot()

        await NS["_bridge_to_main"](robot, "hello", recorded_by_caller=True)

        assert robot.sent[0]["_chat_recorded"] is True

    async def test_says_so_when_the_dashboard_stored_the_turn(self) -> None:
        robot = _Robot()

        async def turn() -> None:
            chat_turn_recorded.set(True)
            await NS["_bridge_to_main"](robot, "hello")

        await asyncio.create_task(turn())

        assert robot.sent[0]["_chat_recorded"] is True

    async def test_says_nothing_otherwise(self) -> None:
        robot = _Robot()

        await NS["_bridge_to_main"](robot, "hello")

        assert "_chat_recorded" not in robot.sent[0]

    async def test_still_bridges_where_wactorz_cannot_be_imported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A node runs this program without the package: no mark to read, same bridge."""
        monkeypatch.setitem(sys.modules, "wactorz.core.persistence", None)
        robot = _Robot()

        await NS["_bridge_to_main"](robot, "hello")
        await NS["_bridge_to_main"](robot, "hello", recorded_by_caller=True)

        assert "_chat_recorded" not in robot.sent[0]
        assert robot.sent[1]["_chat_recorded"] is True
