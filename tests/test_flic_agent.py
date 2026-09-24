"""Flic buttons as triggers: what is published, and what is kept.

The buttons themselves are not here — a paired button is a radio in another
room, and a test that needs one passes in a checkout and fails everywhere else.
What is covered is everything around the radio: the parse, the topics a press
turns into, the private file the keys live in, and the promise that one button
out of range does not take the others down with it.
"""

import asyncio
import base64
import builtins
import contextlib
import json
import logging
import os
import stat
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from wactorz.catalogue_agents import flic_agent
from wactorz.catalogue_agents.flic_agent import (
    GESTURES,
    MISSING_LIB,
    TOPIC_ROOT,
    FlicAgent,
    FlicAgentCommand,
    FlicButton,
    flic_buttons_from_store,
    gesture_topics,
    parse_command,
    slug,
    unique_name,
)
from wactorz.core.actor import Message, MessageType

#: A key as the button gives it: bytes that are not text in any encoding.
PAIRING_KEY = bytes.fromhex("a9018f3c774e5b0126ff9d40b71c3e88")


class FakeDevice:
    """What a scan hands back: an address and nothing this agent needs."""

    def __init__(self, address: str) -> None:
        self.address = address


class FakeClient:
    """Stands in for `pyflic_ble.FlicClient`.

    Records what it was asked to do and lets a test fire a press, which the
    real client does from inside a BLE notification.
    """

    instances: ClassVar[list["FakeClient"]] = []
    fail_to_start: ClassVar[set[str]] = set()

    def __init__(
        self,
        address: str,
        ble_device: Any = None,
        pairing_id: int | None = None,
        pairing_key: bytes | None = None,
        serial_number: str | None = None,
        sig_bits: int = 0,
    ) -> None:
        self.address = address
        self.ble_device = ble_device
        self.pairing_id = pairing_id
        self.pairing_key = pairing_key
        self.serial_number = serial_number
        self.sig_bits = sig_bits
        self.on_button_event: Callable[[str, dict[str, Any]], None] | None = None
        self.state_callbacks: list[Callable[[Any], None]] = []
        #: Every device handed over after construction, which is what starts the
        #: real client's reconnect loop.
        self.devices_given: list[Any] = []
        self.is_connected = False
        self.started = False
        self.stopped = False
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.is_connected = True

    async def full_verify_pairing(self) -> tuple[int, bytes, str, int, int, bytes, int]:
        return (7, PAIRING_KEY, "BH16-F58317", 88, 31, b"\x01\x02\x03", 9)

    async def start(self) -> None:
        if self.address in FakeClient.fail_to_start:
            raise OSError("out of range")
        self.started = True
        self.is_connected = True

    async def stop(self) -> None:
        self.stopped = True
        self.is_connected = False

    async def disconnect(self) -> None:
        self.disconnected = True
        self.is_connected = False

    def register_state_callback(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        self.state_callbacks.append(callback)
        return lambda: self.state_callbacks.remove(callback)

    def set_ble_device(self, ble_device: Any) -> None:
        self.ble_device = ble_device
        self.devices_given.append(ble_device)

    def connection(self, connected: bool) -> None:
        """Report a connection change the way the library does."""
        self.is_connected = connected
        for callback in self.state_callbacks:
            callback(types.SimpleNamespace(connected=connected))

    def press(self, kind: str, **data: Any) -> None:
        """Fire a press the way the library does: synchronously."""
        assert self.on_button_event is not None, "nothing is listening to this button"
        self.on_button_event(kind, {"timestamp_ms": 4242, "was_queued": False, **data})


class Published:
    """Every MQTT publish the agent made, in order."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, Any, bool]] = []

    async def __call__(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.messages.append((topic, payload, retain))

    def topics(self) -> list[str]:
        return [topic for topic, _payload, _retain in self.messages]

    def payload_for(self, topic: str) -> Any:
        for seen, payload, _retain in self.messages:
            if seen == topic:
                return payload
        raise AssertionError(f"{topic} was never published; got {self.topics()}")

    def retained_empty(self) -> list[str]:
        """Topics whose retained message was taken back."""
        return [t for t, payload, retain in self.messages if retain and payload == b""]


#: Addresses a lookup does not find, as if those buttons were out of range.
OUT_OF_RANGE: set[str] = set()

#: The real lookup, kept before the autouse fixture swaps it for a stand-in.
FIND_BUTTON = flic_agent.find_button


@pytest.fixture(autouse=True)
def _reset_fake_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    FakeClient.fail_to_start = set()
    OUT_OF_RANGE.clear()
    # A lookup by address is a real Bluetooth scan; here every button is in
    # range unless a test says otherwise.
    monkeypatch.setattr(flic_agent, "find_button", _lookup)
    # One scan per `pair` unless a test is about waiting for a button.
    monkeypatch.setattr(flic_agent, "PAIR_WAIT_S", 0.0)
    monkeypatch.setattr(flic_agent, "PAIR_RESCAN_PAUSE_S", 0.0)


async def _lookup(address: str, _timeout: float) -> FakeDevice | None:
    return None if address in OUT_OF_RANGE else FakeDevice(address)


def make_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, has_lib: bool = True
) -> tuple[FlicAgent, Published]:
    """An agent with the radio replaced and its publishes captured."""
    agent = FlicAgent(persistence_dir=str(tmp_path))
    agent._checked_lib = True
    # Stands in for the real client class, which needs a button on the desk.
    agent._client_cls = cast(Any, FakeClient) if has_lib else None
    published = Published()
    monkeypatch.setattr(agent, "_mqtt_publish", published)
    monkeypatch.setattr(agent, "publish_manifest", _noop_manifest)
    return agent, published


async def _noop_manifest(**_kwargs: Any) -> None:
    """A manifest goes nowhere unless a test is looking at it."""


def button(name: str = "kitchen", address: str = "AA:BB:CC:DD:EE:FF") -> FlicButton:
    return FlicButton(
        name=name,
        address=address,
        pairing_id=7,
        pairing_key=PAIRING_KEY,
        serial_number="BH16-F58317",
        sig_bits=31,
        battery=88,
    )


async def settle(published: Published, count: int = 1) -> None:
    """Wait for the press pump to get through what it was handed."""
    for _ in range(100):
        if len(published.messages) >= count:
            return
        await asyncio.sleep(0.01)


class TestSayingWhatYouWant:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("scan", FlicAgentCommand.SCAN),
            ("please pair the button", FlicAgentCommand.PAIR),
            ("what buttons do i have", FlicAgentCommand.LIST),
            ("unpair kitchen", FlicAgentCommand.FORGET),
            ("stop", FlicAgentCommand.STOP),
            ("status", FlicAgentCommand.STATUS),
            ("", FlicAgentCommand.HELP),
            ("do something unrelated", FlicAgentCommand.HELP),
        ],
    )
    def test_the_first_word_that_means_something_wins(
        self, text: str, expected: FlicAgentCommand
    ) -> None:
        command, _arguments = parse_command(text)

        assert command is expected

    def test_a_name_is_taken_from_what_follows(self) -> None:
        command, arguments = parse_command("forget the kitchen button")

        assert command is FlicAgentCommand.FORGET
        assert arguments == {"name": "kitchen"}

    def test_a_rename_takes_both_names(self) -> None:
        command, arguments = parse_command("rename flic-1 kitchen")

        assert command is FlicAgentCommand.RENAME
        assert arguments == {"name": "flic-1", "new_name": "kitchen"}

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("rename flic-1 to Lamp Flic", {"name": "flic-1", "new_name": "Lamp Flic"}),
            ("rename flic-1 Lamp Flic", {"name": "flic-1", "new_name": "Lamp Flic"}),
            ('rename flic-1 "Lamp Flic"', {"name": "flic-1", "new_name": "Lamp Flic"}),
            ("rename Lamp Flic to hall", {"name": "Lamp Flic", "new_name": "hall"}),
            ("rename it to Lamp Flic", {"new_name": "Lamp Flic"}),
            ("rename flic-1", {"name": "flic-1"}),
        ],
    )
    def test_a_name_keeps_every_word_it_has(self, text: str, expected: dict[str, str]) -> None:
        # Taking only the last word turned "Lamp Flic" into "flic".
        command, arguments = parse_command(text)

        assert command is FlicAgentCommand.RENAME
        assert arguments == expected

    def test_a_new_pairing_can_be_given_a_name_of_several_words(self) -> None:
        assert parse_command("pair Lamp Flic") == (FlicAgentCommand.PAIR, {"name": "Lamp Flic"})

    async def test_renaming_the_only_button_needs_no_old_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1")]

        reply = await agent.chat("rename it to Lamp Flic")

        assert [b.name for b in agent._known_buttons] == ["lamp-flic"]
        assert f"{TOPIC_ROOT}/lamp-flic/<gesture>" in reply

    def test_a_name_becomes_a_topic_segment(self) -> None:
        # A name reaches MQTT, where a slash starts a new level and `+` and `#`
        # are wildcards.
        assert slug("Kitchen Light/Switch") == "kitchen-light-switch"

    def test_a_second_button_does_not_take_the_first_ones_name(self) -> None:
        assert unique_name("kitchen", {"kitchen"}) == "kitchen-2"
        assert unique_name("kitchen", {"kitchen", "kitchen-2"}) == "kitchen-3"


class TestAPressBecomesATopic:
    async def test_each_gesture_is_its_own_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        for gesture in GESTURES:
            await agent._publish_press("kitchen", gesture, 1000.0, {"was_queued": False})

        assert published.topics() == [f"{TOPIC_ROOT}/kitchen/{g}" for g in GESTURES]

    async def test_the_payload_names_the_button_and_the_gesture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press("kitchen", "click", 1000.5, {"was_queued": False})

        assert published.payload_for(f"{TOPIC_ROOT}/kitchen/click") == {
            "button": "kitchen",
            "serial": "BH16-F58317",
            "gesture": "click",
            "at": 1000.5,
        }

    @pytest.mark.parametrize("kind", ["down", "up"])
    async def test_down_and_up_are_not_published(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # They do not arrive reliably paired — a double click can produce a
        # `down` with no closing `up` — so nothing should be wired to them.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press("kitchen", kind, 1000.0, {"was_queued": False})

        assert published.messages == []

    async def test_a_press_stored_while_disconnected_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The button delivers presses it kept during a disconnection. Acting on
        # one turns a press from earlier into an action now.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press("kitchen", "click", 1000.0, {"was_queued": True})

        assert published.messages == []

    async def test_a_stored_press_is_published_when_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        agent.publish_queued = True

        await agent._publish_press("kitchen", "click", 1000.0, {"was_queued": True})

        assert published.topics() == [f"{TOPIC_ROOT}/kitchen/click"]

    async def test_a_press_is_stamped_with_the_host_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The button's own timestamp counts Bluetooth ticks from a clock nobody
        # else shares, so it cannot be compared with anything.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        agent._loop = asyncio.get_running_loop()
        agent._pump = asyncio.create_task(agent._publish_presses())
        monkeypatch.setattr(flic_agent.time, "time", lambda: 1700000000.0)

        agent._make_press_handler("kitchen")("click", {"timestamp_ms": 4242, "was_queued": False})
        await settle(published)
        agent._pump.cancel()

        assert published.payload_for(f"{TOPIC_ROOT}/kitchen/click")["at"] == 1700000000.0

    async def test_a_press_from_a_button_nobody_knows_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)

        await agent._publish_press("gone", "click", 1000.0, {"was_queued": False})

        assert published.messages == []

    async def test_one_press_that_cannot_be_published_does_not_end_the_pump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        agent._loop = asyncio.get_running_loop()
        calls = {"n": 0}

        async def flaky(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("broker went away")
            await published(topic, payload, retain, qos)

        monkeypatch.setattr(agent, "_mqtt_publish", flaky)
        agent._pump = asyncio.create_task(agent._publish_presses())

        handler = agent._make_press_handler("kitchen")
        handler("click", {"was_queued": False})
        handler("hold", {"was_queued": False})
        await settle(published)
        agent._pump.cancel()

        assert published.topics() == [f"{TOPIC_ROOT}/kitchen/hold"]


class TestWhatThePlannerIsTold:
    def test_every_gesture_of_every_button_is_declared(self) -> None:
        topics = gesture_topics([button("kitchen"), button("hall", "11:22:33:44:55:66")])

        assert topics == [
            f"{TOPIC_ROOT}/kitchen/click",
            f"{TOPIC_ROOT}/kitchen/double_click",
            f"{TOPIC_ROOT}/kitchen/hold",
            f"{TOPIC_ROOT}/kitchen/state",
            f"{TOPIC_ROOT}/hall/click",
            f"{TOPIC_ROOT}/hall/double_click",
            f"{TOPIC_ROOT}/hall/hold",
            f"{TOPIC_ROOT}/hall/state",
        ]

    async def test_a_paired_button_reaches_the_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))

        await agent._announce()

        assert f"{TOPIC_ROOT}/kitchen/click" in manifest["publishes"]
        assert "flic" in manifest["capabilities"]

    async def test_nothing_is_declared_without_the_library(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A declared topic that never publishes is worse than one left out: a
        # pipeline wired to it waits for a message that cannot arrive.
        agent, _published = make_agent(tmp_path, monkeypatch, has_lib=False)
        agent._known_buttons = [button()]
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))

        await agent._announce()

        assert manifest["publishes"] == []
        assert MISSING_LIB in manifest["description"]


def _capture(into: dict[str, Any]) -> Callable[..., Any]:
    async def capture(**kwargs: Any) -> None:
        into.update(kwargs)

    return capture


class TestKeepingAPairing:
    async def test_pairing_stores_the_button_and_starts_listening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF"))

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR, name="Kitchen")

        assert "kitchen" in reply
        assert [b.name for b in agent._known_buttons] == ["kitchen"]
        assert agent._clients["kitchen"].started is True  # pyright: ignore[reportAttributeAccessIssue]

    async def test_the_keys_file_is_readable_only_by_this_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pairing key is what proves a connection is ours, so it is not left
        # where anything else on the machine can read it.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        agent._remember()

        assert stat.S_IMODE(os.stat(agent._buttons_json).st_mode) == 0o600

    async def test_a_key_survives_the_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The key is random bytes, which are not text in any encoding, so JSON
        # carries them as base64.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        agent._remember()
        restored = flic_buttons_from_store(agent._restore())

        assert restored == [button()]
        stored = json.loads(agent._buttons_json.read_text(encoding="utf-8"))
        assert base64.b64decode(stored["buttons"][0]["pairing_key"]) == PAIRING_KEY

    async def test_a_restart_finds_its_buttons_and_listens_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pairing is the expensive part — someone walked to the button and
        # held it down — so it has to outlive the process.
        first, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF"))
        await first._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        second, _again = make_agent(tmp_path, monkeypatch)
        await second.on_start()

        assert [b.name for b in second._known_buttons] == ["kitchen"]
        assert second._known_buttons[0].pairing_key == PAIRING_KEY
        assert second._clients["kitchen"].started is True  # pyright: ignore[reportAttributeAccessIssue]
        await second.on_stop()

    def test_a_damaged_record_costs_only_itself(self) -> None:
        good = button().to_dict()

        restored = flic_buttons_from_store({"buttons": ["nonsense", {"name": "half"}, good]})

        assert [b.name for b in restored] == ["kitchen"]

    def test_a_bare_list_is_read_too(self) -> None:
        assert flic_buttons_from_store([button().to_dict()]) == [button()]

    async def test_an_unreadable_file_is_treated_as_an_absent_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Refusing to start over a damaged file leaves nothing working at all,
        # and a button can always be paired again.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._buttons_json.write_text("{ not json", encoding="utf-8")

        assert agent._restore() == []


def _found(*addresses: str) -> Callable[[float], Any]:
    async def discover(_timeout: float) -> list[Any]:
        return [FakeDevice(address) for address in addresses]

    return discover


class TestMovingAndRemovingButtons:
    async def test_a_rename_moves_the_topics_and_retracts_the_old_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1")]
        await agent._listen()
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))

        reply = await agent._handle_cmd(FlicAgentCommand.RENAME, name="flic-1", new_name="Kitchen")

        assert "kitchen" in reply
        assert f"{TOPIC_ROOT}/flic-1/state" in published.retained_empty()
        assert manifest["publishes"] == gesture_topics([button("kitchen")])

    async def test_a_rename_will_not_collide_with_another_button(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1"), button("kitchen", "11:22:33:44:55:66")]

        await agent._handle_cmd(FlicAgentCommand.RENAME, name="flic-1", new_name="kitchen")

        assert sorted(b.name for b in agent._known_buttons) == ["kitchen", "kitchen-2"]

    async def test_forgetting_takes_back_the_state_and_the_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()
        client = agent._clients["kitchen"]

        reply = await agent._handle_cmd(FlicAgentCommand.FORGET, name="kitchen")

        assert "Forgot 'kitchen'" in reply
        assert agent._known_buttons == []
        assert client.stopped is True  # pyright: ignore[reportAttributeAccessIssue]
        assert f"{TOPIC_ROOT}/kitchen/state" in published.retained_empty()
        assert json.loads(agent._buttons_json.read_text(encoding="utf-8")) == {"buttons": []}

    async def test_forgetting_a_button_that_is_not_there_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("kitchen"), button("hall", "11:22:33:44:55:66")]

        reply = await agent._handle_cmd(FlicAgentCommand.FORGET, name="garage")

        assert "No button called 'garage'" in reply
        assert len(agent._known_buttons) == 2


class TestReachingTheButtons:
    async def test_one_button_out_of_range_does_not_stop_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("kitchen"), button("hall", "11:22:33:44:55:66")]
        FakeClient.fail_to_start = {"AA:BB:CC:DD:EE:FF"}

        reply = await agent._handle_cmd(FlicAgentCommand.LISTEN)

        assert agent._clients["hall"].is_connected
        assert "1 could not be reached yet" in reply
        await agent._stop()

    async def test_a_connected_button_says_so_on_a_retained_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._listen()

        assert (f"{TOPIC_ROOT}/kitchen/state", {"connected": True, "battery": 88}, True) in (
            published.messages
        )

    async def test_a_client_that_will_not_let_go_is_dropped_anyway(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `stop()` can stall while disconnecting, and a shutdown that waits on a
        # radio is a shutdown that does not finish.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()

        async def never() -> None:
            await asyncio.sleep(3600)

        monkeypatch.setattr(agent._clients["kitchen"], "stop", never)
        monkeypatch.setattr(flic_agent, "STOP_TIMEOUT_S", 0.01)

        await asyncio.wait_for(agent._handle_cmd(FlicAgentCommand.STOP), timeout=5)

        assert agent._clients == {}
        assert agent._listening is False

    async def test_stopping_keeps_the_pairings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()

        await agent._handle_cmd(FlicAgentCommand.STOP)

        assert [b.name for b in agent._known_buttons] == ["kitchen"]


class TestWithoutTheLibrary:
    @pytest.mark.parametrize(
        "command",
        [
            FlicAgentCommand.SCAN,
            FlicAgentCommand.PAIR,
            FlicAgentCommand.LIST,
            FlicAgentCommand.LISTEN,
            FlicAgentCommand.FORGET,
        ],
    )
    async def test_every_command_says_what_to_install(
        self, command: FlicAgentCommand, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch, has_lib=False)

        assert await agent._handle_cmd(command) == MISSING_LIB

    async def test_help_and_status_still_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch, has_lib=False)

        assert "scan" in await agent._handle_cmd(FlicAgentCommand.HELP)
        assert await agent._handle_cmd(FlicAgentCommand.STATUS) == MISSING_LIB

    async def test_the_agent_still_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Being findable is what lets it answer with what to install; a silent
        # agent is one nobody can ask.
        agent, _published = make_agent(tmp_path, monkeypatch, has_lib=False)
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))

        await agent.on_start()

        assert manifest["capabilities"]
        assert agent._clients == {}


class TestBeingAsked:
    async def test_a_task_gets_an_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A sender that never gets a RESULT waits out its own timeout instead.
        agent, _published = make_agent(tmp_path, monkeypatch)
        sent: list[tuple[str, MessageType, Any]] = []

        async def record(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, msg_type, payload))
            return True

        monkeypatch.setattr(agent, "send", record)

        await agent.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="asker",
                payload={"action": "status", "task": "t-1"},
            )
        )

        target, msg_type, payload = sent[0]
        assert (target, msg_type) == ("asker", MessageType.RESULT)
        assert payload["action"] == "status"
        assert payload["task"] == "t-1"
        assert payload["ok"] is True

    async def test_free_text_reaches_the_same_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        assert "kitchen" in await agent.chat("list my buttons")

    async def test_deleting_the_agent_takes_back_its_retained_topics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pairings stay on disk, but a retained state nothing maintains any
        # more is told to every subscriber that connects later.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()

        await agent.handle_message(Message(type=MessageType.DELETE, sender_id="main"))

        assert f"{TOPIC_ROOT}/kitchen/state" in published.retained_empty()
        assert agent._clients == {}


class TestScanning:
    async def test_a_scan_says_which_buttons_are_already_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        monkeypatch.setattr(
            flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        )

        reply = await agent._handle_cmd(FlicAgentCommand.SCAN)

        assert "aa:bb:cc:dd:ee:ff — kitchen" in reply
        assert "11:22:33:44:55:66 — not paired" in reply

    async def test_an_empty_scan_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found())

        assert "No Flic buttons in range" in await agent._handle_cmd(FlicAgentCommand.SCAN)

    async def test_a_scan_that_never_returns_is_given_up_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _raises(TimeoutError()))

        assert "timed out" in await agent._handle_cmd(FlicAgentCommand.SCAN)

    async def test_a_scan_that_fails_says_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _raises(OSError("no adapter")))

        assert "no adapter" in await agent._handle_cmd(FlicAgentCommand.SCAN)

    async def test_only_flic_buttons_are_asked_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A scan that returned every piece of Bluetooth in the building would
        # be a list nobody can read.
        asked: dict[str, Any] = {}

        class FakeScanner:
            @staticmethod
            async def discover(timeout: float, service_uuids: list[str]) -> list[Any]:
                asked.update(timeout=timeout, service_uuids=service_uuids)
                return [FakeDevice("AA:BB:CC:DD:EE:FF")]

        module = types.ModuleType("bleak")
        module.BleakScanner = FakeScanner  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "bleak", module)

        found = await flic_agent.discover_buttons(2.0)

        assert asked["service_uuids"] == [
            flic_agent.FLIC_SERVICE_UUID,
            flic_agent.TWIST_SERVICE_UUID,
        ]
        assert [device.address for device in found] == ["AA:BB:CC:DD:EE:FF"]


def _raises(error: BaseException) -> Callable[[float], Any]:
    async def discover(_timeout: float) -> list[Any]:
        raise error

    return discover


class TestSayingWhyNot:
    async def test_pairing_with_nothing_in_pairing_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        monkeypatch.setattr(flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF"))

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "7 seconds" in reply
        assert len(agent._known_buttons) == 1

    async def test_a_scan_that_fails_before_pairing_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _raises(OSError("no adapter")))

        assert "Could not scan" in await agent._handle_cmd(FlicAgentCommand.PAIR)

    async def test_a_button_that_will_not_verify_is_not_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF"))

        async def refuse(self: FakeClient) -> tuple[int, bytes, str, int, int, bytes, int]:
            raise OSError("the button stopped listening")

        monkeypatch.setattr(FakeClient, "full_verify_pairing", refuse)

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "Pairing failed" in reply
        assert agent._known_buttons == []
        assert FakeClient.instances[-1].disconnected is True

    async def test_listing_and_listening_with_nothing_paired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)

        assert "No buttons paired" in await agent._handle_cmd(FlicAgentCommand.LIST)
        assert "No buttons paired" in await agent._handle_cmd(FlicAgentCommand.LISTEN)

    async def test_a_rename_needs_both_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)

        assert "rename <old> <new>" in await agent._handle_cmd(
            FlicAgentCommand.RENAME, name="kitchen"
        )

    async def test_renaming_a_button_that_is_not_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)

        reply = await agent._handle_cmd(FlicAgentCommand.RENAME, name="garage", new_name="hall")

        assert "No button called 'garage'" in reply

    async def test_forgetting_the_only_button_needs_no_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        assert "Forgot 'kitchen'" in await agent._handle_cmd(FlicAgentCommand.FORGET)


class TestTheSmallParts:
    async def test_a_client_left_over_from_a_forgotten_button_is_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        ghost = FakeClient(address="AA:BB:CC:DD:EE:FF")
        agent._clients["ghost"] = cast(Any, ghost)

        await agent._handle_cmd(FlicAgentCommand.STOP)

        assert ghost.stopped is True
        assert agent._clients == {}

    async def test_a_listed_button_shows_what_is_known_about_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()

        reply = await agent._handle_cmd(FlicAgentCommand.LIST)

        assert "kitchen — BH16-F58317, connected, battery 88%" in reply
        assert "not since start" in reply

    async def test_status_counts_what_is_connected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()

        assert "1 paired, 1 connected" in await agent._handle_cmd(FlicAgentCommand.STATUS)

    async def test_the_dashboard_is_told_what_the_agent_is_doing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        assert agent._current_task_description() == "flic (1 paired, listening=False)"

    async def test_nothing_is_paired_without_a_client_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch, has_lib=False)

        assert await agent._pair_device(FakeDevice("AA:BB:CC:DD:EE:FF"), "kitchen") is None
        assert await agent._start_button(button()) is False

    def test_the_library_is_looked_for_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whether the extra is installed is settled at the first question, so
        # the answer does not depend on what is installed where the tests run.
        agent = FlicAgent(persistence_dir=str(tmp_path))
        real_import = builtins.__import__
        attempts = {"n": 0}

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "pyflic_ble":
                attempts["n"] += 1
                raise ImportError("no pyflic_ble here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)

        assert agent._have_lib() is False
        assert agent._have_lib() is False
        assert attempts["n"] == 1

    @pytest.mark.parametrize(
        ("serial", "expected"),
        [("BH16-F58317", "flic2"), ("BT01-AAAAAA", "twist"), ("BD01-AAAAAA", "duo")],
    )
    def test_a_serial_number_says_which_button_it_is(self, serial: str, expected: str) -> None:
        assert flic_agent.device_type_of(serial) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(b"\x01\x02", "0102"), ("already-text", "already-text"), (None, "")]
    )
    def test_a_uuid_is_kept_as_text_however_it_arrives(self, value: Any, expected: str) -> None:
        assert flic_agent.as_hex(value) == expected

    async def test_stopping_the_agent_puts_every_button_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        agent._loop = asyncio.get_running_loop()
        agent._pump = asyncio.create_task(agent._publish_presses())
        await agent._listen()
        client = agent._clients["kitchen"]

        await agent.on_stop()

        assert client.stopped is True  # pyright: ignore[reportAttributeAccessIssue]
        assert agent._pump is None

    async def test_a_plain_string_task_is_understood(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        sent: list[Any] = []

        async def record(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append(payload)
            return True

        monkeypatch.setattr(agent, "send", record)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="asker", payload="status")
        )

        assert sent[0]["action"] == "status"

    async def test_a_task_carrying_free_text_is_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        sent: list[Any] = []

        async def record(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append(payload)
            return True

        monkeypatch.setattr(agent, "send", record)

        await agent.handle_message(
            Message(
                type=MessageType.TASK, sender_id="asker", payload={"text": "show me my buttons"}
            )
        )

        assert sent[0]["action"] == "list"

    async def test_a_message_that_is_neither_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)

        await agent.handle_message(Message(type=MessageType.RESULT, sender_id="somebody"))

        assert published.messages == []


# ── More than one kind of button ──────────────────────────────────────────────


class TestButtonsThatAreNotFlic2:
    async def test_a_two_button_device_says_which_half_was_pressed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Duo reports both halves as the same gesture, so the topic alone
        # cannot say which one it was.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press(
            "kitchen", "click", 1000.0, {"was_queued": False, "button_index": 1}
        )

        assert published.payload_for(f"{TOPIC_ROOT}/kitchen/click")["button_index"] == 1

    async def test_a_single_button_device_carries_no_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press("kitchen", "click", 1000.0, {"was_queued": False})

        assert "button_index" not in published.payload_for(f"{TOPIC_ROOT}/kitchen/click")

    async def test_an_event_nothing_is_wired_to_is_mentioned_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Twist's rotation reaches the agent and goes no further. Dropping it
        # in silence leaves someone watching a button that looks broken.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        with caplog_at_info() as records:
            for _ in range(3):
                await agent._publish_press(
                    "kitchen", "rotate_clockwise", 1000.0, {"was_queued": False}
                )
            await agent._publish_press("kitchen", "swipe_left", 1000.0, {"was_queued": False})

        assert published.messages == []
        assert [r for r in records if "rotate_clockwise" in r].__len__() == 1
        assert [r for r in records if "swipe_left" in r].__len__() == 1

    async def test_a_twist_is_looked_for_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: dict[str, Any] = {}

        class FakeScanner:
            @staticmethod
            async def discover(timeout: float, service_uuids: list[str]) -> list[Any]:
                asked.update(service_uuids=service_uuids)
                return []

        module = types.ModuleType("bleak")
        module.BleakScanner = FakeScanner  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "bleak", module)

        await flic_agent.discover_buttons(1.0)

        assert asked["service_uuids"] == [
            flic_agent.FLIC_SERVICE_UUID,
            flic_agent.TWIST_SERVICE_UUID,
        ]


@contextlib.contextmanager
def caplog_at_info() -> Iterator[list[str]]:
    """The agent's own log lines, as formatted text."""
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Collect()
    flic_agent.LOG.addHandler(handler)
    previous = flic_agent.LOG.level
    flic_agent.LOG.setLevel(logging.INFO)
    try:
        yield records
    finally:
        flic_agent.LOG.removeHandler(handler)
        flic_agent.LOG.setLevel(previous)


# ── Retained state ────────────────────────────────────────────────────────────


class TestSayingWhereAButtonIs:
    async def test_the_state_is_published_at_least_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dropped retained message leaves the broker telling every later
        # subscriber the opposite of where the button is.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        sent: list[dict[str, Any]] = []

        async def record(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            sent.append({"topic": topic, "retain": retain, "qos": qos})

        monkeypatch.setattr(agent, "_mqtt_publish", record)

        await agent._listen()
        await agent._handle_cmd(FlicAgentCommand.FORGET, name="kitchen")

        for message in sent:
            assert message["retain"] is True
            assert message["qos"] == 1, message


# ── Pairing with a crowd ──────────────────────────────────────────────────────


class TestMoreThanOneButtonWaiting:
    async def test_the_others_are_not_left_unmentioned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(
            flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        )

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "1 other button was in pairing mode" in reply
        assert len(agent._known_buttons) == 1

    async def test_one_button_alone_is_not_talked_about(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF"))

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "pairing mode" not in reply


def _client(agent: FlicAgent, name: str) -> FakeClient:
    return cast(FakeClient, agent._clients[name])


class TestFindingTheButton:
    """The library connects only to a device it is handed, never to an address.

    Home Assistant hands one over from every advertisement it hears. Here the
    agent has to find it, or no session ever starts: "No BLE device available".
    """

    async def test_a_session_starts_with_the_device_found_at_its_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._listen()

        client = _client(agent, "kitchen")
        assert client.started
        assert client.ble_device.address == "AA:BB:CC:DD:EE:FF"

    async def test_pairing_hands_the_session_the_device_it_just_paired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found("AA:BB:CC:DD:EE:FF"))
        # A second lookup would find the same button; making it fail proves
        # none is made.
        OUT_OF_RANGE.add("AA:BB:CC:DD:EE:FF")

        await agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        client = _client(agent, "kitchen")
        assert client.started
        assert client.ble_device is not None

    async def test_a_button_out_of_range_is_kept_and_looked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flic_agent, "FIND_INTERVAL_S", 0.01)
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        OUT_OF_RANGE.add("AA:BB:CC:DD:EE:FF")

        await agent._listen()
        client = _client(agent, "kitchen")
        assert not client.started
        assert client.ble_device is None

        OUT_OF_RANGE.clear()
        for _ in range(100):
            if client.devices_given:
                break
            await asyncio.sleep(0.01)

        # Handing the device over is what starts the library's reconnect loop.
        assert [device.address for device in client.devices_given] == ["AA:BB:CC:DD:EE:FF"]
        await agent._stop()

    async def test_a_failed_start_is_handed_to_the_librarys_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A start that raises leaves no retry behind it in the library.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        FakeClient.fail_to_start = {"AA:BB:CC:DD:EE:FF"}

        await agent._listen()

        client = _client(agent, "kitchen")
        assert client.devices_given, "nothing will retry this button"
        await agent._stop()

    async def test_stopping_stops_the_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        OUT_OF_RANGE.add("AA:BB:CC:DD:EE:FF")
        await agent._listen()
        finder = agent._finder
        assert finder is not None

        await agent._handle_cmd(FlicAgentCommand.STOP)
        await asyncio.sleep(0)

        assert finder.cancelled() or finder.done()
        assert agent._finder is None

    async def test_the_lookup_asks_bleak_for_that_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: dict[str, Any] = {}

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address: str, timeout: float) -> Any:
                asked.update(address=address, timeout=timeout)
                return FakeDevice(address)

        module = types.ModuleType("bleak")
        module.BleakScanner = FakeScanner  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "bleak", module)
        monkeypatch.setattr(flic_agent, "find_button", FIND_BUTTON)

        found = await flic_agent.find_button("AA:BB:CC:DD:EE:FF", 3.0)

        assert asked == {"address": "AA:BB:CC:DD:EE:FF", "timeout": 3.0}
        assert found.address == "AA:BB:CC:DD:EE:FF"


class TestConnectionsTheLibraryMakes:
    """The library reconnects on its own, so the retained state has to follow it."""

    async def test_a_later_connection_is_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._loop = asyncio.get_running_loop()
        agent._pump = asyncio.create_task(agent._publish_presses())
        agent._known_buttons = [button()]
        FakeClient.fail_to_start = {"AA:BB:CC:DD:EE:FF"}
        await agent._listen()

        _client(agent, "kitchen").connection(True)
        await settle(published)

        assert (f"{TOPIC_ROOT}/kitchen/state", {"connected": True, "battery": 88}, True) in (
            published.messages
        )
        await agent.on_stop()

    async def test_a_drop_is_published_and_sets_the_search_going(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._loop = asyncio.get_running_loop()
        agent._pump = asyncio.create_task(agent._publish_presses())
        agent._known_buttons = [button()]
        await agent._listen()
        assert agent._finder is None

        _client(agent, "kitchen").connection(False)
        await settle(published, count=2)
        await asyncio.sleep(0)

        assert published.messages[-1] == (
            f"{TOPIC_ROOT}/kitchen/state",
            {"connected": False, "battery": 88},
            True,
        )
        assert agent._finder is not None
        await agent.on_stop()

    async def test_a_forgotten_button_publishes_nothing_more(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A callback from a client already dropped must not bring back the
        # state that forgetting took away.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()
        client = _client(agent, "kitchen")
        await agent._handle_cmd(FlicAgentCommand.FORGET, name="kitchen")
        before = list(published.messages)

        await agent._publish_connection("kitchen", True)

        assert published.messages == before
        assert client.stopped


class TestOneScanAtATime:
    async def test_scans_never_overlap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BlueZ answers "No discovery started" to a scan stopping while
        # another started over it.
        agent, _published = make_agent(tmp_path, monkeypatch)
        running = 0
        most = 0

        async def discover(_timeout: float) -> list[Any]:
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.01)
            running -= 1
            return []

        async def lookup(address: str, _timeout: float) -> Any:
            return await discover(_timeout)

        monkeypatch.setattr(flic_agent, "discover_buttons", discover)
        monkeypatch.setattr(flic_agent, "find_button", lookup)

        await asyncio.gather(agent._discover(), agent._discover(), agent._locate("AA"))

        assert most == 1


class TestTellingThePersonHowToPair:
    """A button shows up only while it is held in pairing mode, so `pair` says so first."""

    async def test_it_says_to_hold_the_button_before_it_looks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        order: list[str] = []

        async def discover(_timeout: float) -> list[Any]:
            order.append("scan")
            return [FakeDevice("AA:BB:CC:DD:EE:FF")]

        async def notify(text: str, **_extra: Any) -> None:
            order.append(text)

        monkeypatch.setattr(flic_agent, "discover_buttons", discover)
        monkeypatch.setattr(agent, "notify_user", notify)

        await agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        assert order[0] == flic_agent.PAIR_INSTRUCTIONS
        assert "7 seconds" in order[0]
        assert order[1] == "scan"

    async def test_it_waits_for_a_button_to_enter_pairing_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Asking first and then picking the button up is the usual order.
        monkeypatch.setattr(flic_agent, "PAIR_WAIT_S", 5.0)
        agent, _published = make_agent(tmp_path, monkeypatch)
        scans = 0

        async def discover(_timeout: float) -> list[Any]:
            nonlocal scans
            scans += 1
            return [FakeDevice("AA:BB:CC:DD:EE:FF")] if scans >= 3 else []

        monkeypatch.setattr(flic_agent, "discover_buttons", discover)

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        assert scans == 3
        assert reply.startswith("Paired 'kitchen'")

    async def test_it_gives_up_when_no_button_comes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flic_agent, "PAIR_WAIT_S", 0.05)
        monkeypatch.setattr(flic_agent, "PAIR_RESCAN_PAUSE_S", 0.01)
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "discover_buttons", _found())

        reply = await asyncio.wait_for(agent._handle_cmd(FlicAgentCommand.PAIR), timeout=5)

        assert "No button came into pairing mode" in reply
        assert agent._known_buttons == []
