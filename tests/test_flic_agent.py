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
from collections.abc import Callable, Collection, Iterator
from dataclasses import replace
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
        push_twist_mode: Any = "default",
    ) -> None:
        self.address = address
        self.push_twist_mode = push_twist_mode
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
        return (7, PAIRING_KEY, serial_for(self.address), 88, 31, b"\x01\x02\x03", 9)

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

    def connection(self, connected: bool, battery_voltage: float | None = None) -> None:
        """Report a connection change the way the library does."""
        self.is_connected = connected
        state = types.SimpleNamespace(connected=connected, battery_voltage=battery_voltage)
        for callback in self.state_callbacks:
            callback(state)

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
    # No waiting for a button to come into pairing mode unless a test is about it.
    monkeypatch.setattr(flic_agent, "PAIR_WAIT_S", 0.0)


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


def serial_for(address: str) -> str:
    """A serial number of the real shape, one per address, as real buttons have."""
    return "BH16-" + address.replace(":", "")[-6:]


#: The default button's serial, and the key its topics are published under.
SERIAL = serial_for("AA:BB:CC:DD:EE:FF")
KEY = slug(SERIAL)
#: The same for the second button several tests pair beside it.
OTHER_KEY = slug(serial_for("11:22:33:44:55:66"))


def button(name: str = "kitchen", address: str = "AA:BB:CC:DD:EE:FF") -> FlicButton:
    return FlicButton(
        name=name,
        address=address,
        pairing_id=7,
        pairing_key=PAIRING_KEY,
        serial_number=serial_for(address),
        sig_bits=31,
        # Raw, as a Flic 2 reports it: 850 of 1024 against 3.6 V.
        battery=850,
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

        assert [b.name for b in agent._known_buttons] == ["Lamp Flic"]
        assert f"{TOPIC_ROOT}/{KEY}/<gesture>" in reply

    def test_a_slug_is_safe_in_a_topic(self) -> None:
        # A key reaches MQTT, where a slash starts a new level and `+` and `#`
        # are wildcards.
        assert slug("Kitchen Light/Switch") == "kitchen-light-switch"
        assert slug("BH16-F58317") == "bh16-f58317"

    def test_a_name_keeps_what_the_person_typed(self) -> None:
        assert unique_name("  Lamp   Flic ", set()) == "Lamp Flic"
        assert button("Lamp Flic").slug == "lamp-flic"

    def test_a_second_button_does_not_take_the_first_ones_name(self) -> None:
        # Unique by slug, so "kitchen" and "Kitchen" cannot both be paired.
        assert unique_name("Kitchen", {"kitchen"}) == "Kitchen 2"
        assert unique_name("Kitchen", {"kitchen", "kitchen-2"}) == "Kitchen 3"

    def test_a_name_with_nothing_to_address_it_by_is_not_kept(self) -> None:
        assert unique_name("!!!", set()) == "Flic"

    async def test_a_button_answers_to_its_name_however_it_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("Lamp Flic")]

        for said in ("Lamp Flic", "lamp flic", "lamp-flic", SERIAL, SERIAL.lower()):
            assert agent._button(said) == agent._known_buttons[0], said
        assert agent._button("") is None


class TestAPressBecomesATopic:
    async def test_each_gesture_is_its_own_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        for gesture in GESTURES:
            await agent._publish_press(KEY, gesture, 1000.0, {"was_queued": False})

        assert published.topics() == [f"{TOPIC_ROOT}/{KEY}/{g}" for g in GESTURES]

    async def test_the_payload_names_the_button_and_the_gesture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press(KEY, "click", 1000.5, {"was_queued": False})

        assert published.payload_for(f"{TOPIC_ROOT}/{KEY}/click") == {
            "button": "kitchen",
            "serial": SERIAL,
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

        await agent._publish_press(KEY, kind, 1000.0, {"was_queued": False})

        assert published.messages == []

    async def test_a_press_stored_while_disconnected_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The button delivers presses it kept during a disconnection. Acting on
        # one turns a press from earlier into an action now.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press(KEY, "click", 1000.0, {"was_queued": True})

        assert published.messages == []

    async def test_a_stored_press_is_published_when_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        agent.publish_queued = True

        await agent._publish_press(KEY, "click", 1000.0, {"was_queued": True})

        assert published.topics() == [f"{TOPIC_ROOT}/{KEY}/click"]

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

        agent._make_press_handler(KEY)("click", {"timestamp_ms": 4242, "was_queued": False})
        await settle(published)
        agent._pump.cancel()

        assert published.payload_for(f"{TOPIC_ROOT}/{KEY}/click")["at"] == 1700000000.0

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

        handler = agent._make_press_handler(KEY)
        handler("click", {"was_queued": False})
        handler("hold", {"was_queued": False})
        await settle(published)
        agent._pump.cancel()

        assert published.topics() == [f"{TOPIC_ROOT}/{KEY}/hold"]


class TestWhatThePlannerIsTold:
    def test_every_gesture_of_every_button_is_declared(self) -> None:
        topics = gesture_topics([button("kitchen"), button("hall", "11:22:33:44:55:66")])

        assert topics == [
            f"{TOPIC_ROOT}/{KEY}/click",
            f"{TOPIC_ROOT}/{KEY}/double_click",
            f"{TOPIC_ROOT}/{KEY}/hold",
            f"{TOPIC_ROOT}/{KEY}/state",
            f"{TOPIC_ROOT}/{OTHER_KEY}/click",
            f"{TOPIC_ROOT}/{OTHER_KEY}/double_click",
            f"{TOPIC_ROOT}/{OTHER_KEY}/hold",
            f"{TOPIC_ROOT}/{OTHER_KEY}/state",
        ]

    async def test_a_paired_button_reaches_the_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))

        await agent._announce()

        assert f"{TOPIC_ROOT}/{KEY}/click" in manifest["publishes"]
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
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR, name="Kitchen")

        # The name as given; the topics keyed by the serial the button reported.
        assert reply.startswith(f"Paired 'Kitchen' ({SERIAL})")
        assert f"{TOPIC_ROOT}/{KEY}/<gesture>" in reply
        assert [b.name for b in agent._known_buttons] == ["Kitchen"]
        assert _client(agent, "kitchen").started is True

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits; on Windows the folder's ACL decides who can read it",
    )
    async def test_the_keys_file_is_readable_only_by_this_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pairing key is what proves a connection is ours, so it is not left
        # where anything else on the machine can read it.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._remember()

        assert stat.S_IMODE(os.stat(agent._buttons_json).st_mode) == 0o600

    async def test_a_key_survives_the_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The key is random bytes, which are not text in any encoding, so JSON
        # carries them as base64.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._remember()
        restored = flic_buttons_from_store(await agent._restore())

        assert restored == [button()]
        stored = json.loads(agent._buttons_json.read_text(encoding="utf-8"))
        assert base64.b64decode(stored["buttons"][0]["pairing_key"]) == PAIRING_KEY

    async def test_a_restart_finds_its_buttons_and_listens_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pairing is the expensive part — someone walked to the button and
        # held it down — so it has to outlive the process.
        first, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))
        await first._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        second, _again = make_agent(tmp_path, monkeypatch)
        await second.on_start()
        client = _client(second, "kitchen")
        for _ in range(100):
            if client.devices_given:
                break
            await asyncio.sleep(0.01)

        assert [b.name for b in second._known_buttons] == ["kitchen"]
        assert second._known_buttons[0].pairing_key == PAIRING_KEY
        # Found in the background and handed to the library, which connects.
        assert [device.address for device in client.devices_given] == ["AA:BB:CC:DD:EE:FF"]
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

        assert await agent._restore() == []


def _found(*addresses: str) -> Callable[..., Any]:
    """A stand-in for `discover_buttons` or `watch_for_buttons` that sees these."""

    async def discover(_timeout: float, _skip: Collection[str] = ()) -> list[Any]:
        return [FakeDevice(address) for address in addresses]

    return discover


class TestMovingAndRemovingButtons:
    async def test_a_rename_leaves_the_topics_where_they_are(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What is wired to a button keeps working through a rename.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1")]
        await agent._listen()
        client = _client(agent, "flic-1")
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))
        before = list(published.messages)

        reply = await agent._handle_cmd(FlicAgentCommand.RENAME, name="flic-1", new_name="Kitchen")

        assert (
            reply == f"'flic-1' is now 'Kitchen'. Its presses stay on {TOPIC_ROOT}/{KEY}/<gesture>."
        )
        assert published.messages == before
        assert _client(agent, "kitchen") is client
        assert client.stopped is False
        assert manifest["publishes"] == gesture_topics([button("flic-1")])

    async def test_the_manifest_says_which_button_is_which(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Topics named by serial mean nothing to a planner asked for "the
        # kitchen button" unless the manifest says which serial that is.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1")]
        manifest: dict[str, Any] = {}
        monkeypatch.setattr(agent, "publish_manifest", _capture(manifest))

        await agent._handle_cmd(FlicAgentCommand.RENAME, name="flic-1", new_name="Kitchen")

        assert f"'Kitchen' is {TOPIC_ROOT}/{KEY}" in manifest["description"]
        assert "flic-1" not in manifest["description"]

    async def test_a_press_after_a_rename_carries_the_new_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1")]
        await agent._handle_cmd(FlicAgentCommand.RENAME, name="flic-1", new_name="Kitchen")

        await agent._publish_press(KEY, "click", 1000.0, {"was_queued": False})

        assert published.payload_for(f"{TOPIC_ROOT}/{KEY}/click")["button"] == "Kitchen"

    async def test_a_rename_will_not_collide_with_another_button(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("flic-1"), button("kitchen", "11:22:33:44:55:66")]

        await agent._handle_cmd(FlicAgentCommand.RENAME, name="flic-1", new_name="Kitchen")

        assert sorted(b.name for b in agent._known_buttons) == ["Kitchen 2", "kitchen"]

    async def test_renaming_to_a_different_case_is_not_a_clash_with_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("kitchen")]

        await agent._handle_cmd(FlicAgentCommand.RENAME, name="kitchen", new_name="Kitchen")

        assert [b.name for b in agent._known_buttons] == ["Kitchen"]

    async def test_forgetting_takes_back_the_state_and_the_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()
        client = _client(agent, "kitchen")

        reply = await agent._handle_cmd(FlicAgentCommand.FORGET, name="kitchen")

        assert "Forgot 'kitchen'" in reply
        assert agent._known_buttons == []
        assert client.stopped is True  # pyright: ignore[reportAttributeAccessIssue]
        assert f"{TOPIC_ROOT}/{KEY}/state" in published.retained_empty()
        assert json.loads(agent._buttons_json.read_text(encoding="utf-8"))["buttons"] == []

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

        assert _client(agent, "hall").is_connected
        assert "1 could not be reached yet" in reply
        await agent._stop()

    async def test_a_connected_button_says_so_on_a_retained_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._listen()

        assert (
            f"{TOPIC_ROOT}/{KEY}/state",
            {"connected": True, "battery_voltage": 2.99},
            True,
        ) in (published.messages)

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

        monkeypatch.setattr(_client(agent, "kitchen"), "stop", never)
        monkeypatch.setattr(flic_agent, "STOP_TIMEOUT_S", 0.01)

        await asyncio.wait_for(agent._handle_cmd(FlicAgentCommand.STOP), timeout=5)

        assert agent._clients == {}
        assert agent._listening is False

    async def test_buttons_that_will_not_let_go_are_waited_for_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A controller that stops answering stalls every disconnect; waited
        # for one after another, a shutdown takes a timeout per button.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [
            button("kitchen"),
            button("hall", "11:22:33:44:55:66"),
            button("porch", "77:88:99:AA:BB:CC"),
        ]
        await agent._listen()

        async def never() -> None:
            await asyncio.sleep(3600)

        for name in ("kitchen", "hall", "porch"):
            monkeypatch.setattr(_client(agent, name), "stop", never)
        monkeypatch.setattr(flic_agent, "STOP_TIMEOUT_S", 0.2)
        loop = asyncio.get_running_loop()
        started = loop.time()

        await agent._stop(remember=False)

        assert loop.time() - started < 0.4
        assert agent._clients == {}
        disconnected = [
            topic
            for topic, payload, _keep in published.messages
            if payload == {"connected": False, "battery_voltage": 2.99}
        ]
        assert sorted(disconnected) == sorted(
            f"{TOPIC_ROOT}/{b.key}/state" for b in agent._known_buttons
        )

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
        # A retained state nothing maintains any more is told to every
        # subscriber that connects later, so the retraction must come last.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._listen()

        await agent.on_delete()

        state = f"{TOPIC_ROOT}/{KEY}/state"
        retained = [
            payload for topic, payload, keep in published.messages if keep and topic == state
        ]
        assert retained[-1] == b""
        assert agent._clients == {}

    async def test_deleting_the_agent_deletes_the_pairing_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A delete promises to leave nothing behind, and the store purge that
        # follows does not know about the buttons file.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._remember()
        assert agent._buttons_json.exists()

        await agent.on_delete()

        assert not agent._buttons_json.exists()
        assert agent._known_buttons == []

    async def test_the_delete_command_runs_the_hook_before_stopping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both delete paths call it: main's, and an actor's own `delete`.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._remember()

        await agent.apply_command("delete")

        assert not agent._buttons_json.exists()


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


def _raises(error: BaseException) -> Callable[..., Any]:
    async def discover(_timeout: float, _skip: Collection[str] = ()) -> list[Any]:
        raise error

    return discover


class TestSayingWhyNot:
    async def test_pairing_with_nothing_in_pairing_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "7 seconds" in reply
        assert len(agent._known_buttons) == 1

    async def test_a_scan_that_fails_before_pairing_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _raises(OSError("no adapter")))

        assert "Could not scan" in await agent._handle_cmd(FlicAgentCommand.PAIR)

    async def test_a_button_that_will_not_verify_is_not_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))

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

        assert f"kitchen — {TOPIC_ROOT}/{KEY}, connected, battery 2.99 V" in reply
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
        client = _client(agent, "kitchen")

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

        await agent._publish_press(KEY, "click", 1000.0, {"was_queued": False, "button_index": 1})

        assert published.payload_for(f"{TOPIC_ROOT}/{KEY}/click")["button_index"] == 1

    async def test_a_single_button_device_carries_no_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        await agent._publish_press(KEY, "click", 1000.0, {"was_queued": False})

        assert "button_index" not in published.payload_for(f"{TOPIC_ROOT}/{KEY}/click")

    async def test_an_event_nothing_is_wired_to_is_mentioned_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Twist's rotation reaches the agent and goes no further. Dropping it
        # in silence leaves someone watching a button that looks broken.
        agent, published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]

        with caplog_at_info() as records:
            for _ in range(3):
                await agent._publish_press(KEY, "rotate_clockwise", 1000.0, {"was_queued": False})
            await agent._publish_press(KEY, "swipe_left", 1000.0, {"was_queued": False})

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
            flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        )

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "Another button was in pairing mode" in reply
        assert len(agent._known_buttons) == 1

    async def test_one_button_alone_is_not_talked_about(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "pairing mode" not in reply


def _client(agent: FlicAgent, name: str) -> FakeClient:
    """The client of the button a person would call `name`."""
    found = agent._button(name)
    assert found is not None, f"no button called {name!r}"
    return cast(FakeClient, agent._clients[found.key])


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
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))
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

        _client(agent, "kitchen").connection(True, battery_voltage=3.01)
        await settle(published)

        # The voltage the library just read, not the one from pairing.
        assert (
            f"{TOPIC_ROOT}/{KEY}/state",
            {"connected": True, "battery_voltage": 3.01},
            True,
        ) in (published.messages)
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
            f"{TOPIC_ROOT}/{KEY}/state",
            {"connected": False, "battery_voltage": 2.99},
            True,
        )
        assert agent._finder is not None
        await agent.on_stop()

    async def test_after_a_drop_the_library_gets_the_first_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The library retries with the device it has; a scan started over
        # those retries can knock down the connection being made.
        monkeypatch.setattr(flic_agent, "FIND_INTERVAL_S", 0.2)
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._loop = asyncio.get_running_loop()
        agent._known_buttons = [button()]
        await agent._listen()
        lookups: list[str] = []

        async def lookup(address: str, _timeout: float) -> Any:
            lookups.append(address)
            return FakeDevice(address)

        monkeypatch.setattr(flic_agent, "find_button", lookup)
        client = _client(agent, "kitchen")

        client.connection(False)
        await asyncio.sleep(0.1)
        assert lookups == []

        await asyncio.sleep(0.2)
        assert lookups == ["AA:BB:CC:DD:EE:FF"]
        assert client.devices_given
        await agent.on_stop()

    async def test_a_button_the_library_brings_back_is_never_scanned_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flic_agent, "FIND_INTERVAL_S", 0.05)
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._loop = asyncio.get_running_loop()
        agent._known_buttons = [button()]
        await agent._listen()
        lookups: list[str] = []

        async def lookup(address: str, _timeout: float) -> Any:
            lookups.append(address)
            return FakeDevice(address)

        monkeypatch.setattr(flic_agent, "find_button", lookup)
        client = _client(agent, "kitchen")

        client.connection(False)
        await asyncio.sleep(0.01)
        client.connection(True)
        finder = agent._finder
        assert finder is not None
        await asyncio.wait_for(finder, timeout=1)

        assert lookups == []
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

        await agent._publish_connection(KEY, True)

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

        async def discover(_timeout: float, _skip: Collection[str]) -> list[Any]:
            order.append("scan")
            return [FakeDevice("AA:BB:CC:DD:EE:FF")]

        async def notify(text: str, **_extra: Any) -> None:
            order.append(text)

        monkeypatch.setattr(flic_agent, "watch_for_buttons", discover)
        monkeypatch.setattr(agent, "notify_user", notify)

        await agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        assert order[0] == flic_agent.PAIR_INSTRUCTIONS
        assert "7 seconds" in order[0]
        assert order[1] == "scan"

    async def test_it_watches_once_for_the_whole_wait_past_its_own_buttons(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Asking first and then picking the button up is the usual order, so
        # the watch covers the whole wait, and a button already paired here
        # does not end it.
        monkeypatch.setattr(flic_agent, "PAIR_WAIT_S", 5.0)
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("hall", "11:22:33:44:55:66")]
        asked: list[tuple[float, set[str]]] = []

        async def watch(timeout: float, skip: Collection[str]) -> list[Any]:
            asked.append((timeout, set(skip)))
            return [FakeDevice("11:22:33:44:55:66"), FakeDevice("AA:BB:CC:DD:EE:FF")]

        monkeypatch.setattr(flic_agent, "watch_for_buttons", watch)

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        assert asked == [(5.0, {"11:22:33:44:55:66"})]
        assert reply.startswith("Paired 'kitchen'")
        assert "Another button" not in reply

    async def test_it_gives_up_when_no_button_comes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flic_agent, "PAIR_WAIT_S", 0.05)
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found())

        reply = await asyncio.wait_for(agent._handle_cmd(FlicAgentCommand.PAIR), timeout=5)

        assert "No button came into pairing mode" in reply
        assert agent._known_buttons == []


class TestFromTheReview:
    async def test_starting_does_not_wait_on_the_radio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `Actor.start` runs `on_start` before the message loop and heartbeat
        # exist; a button that takes its time must not hold those back.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        await agent._remember()

        async def slow(_address: str, _timeout: float) -> Any:
            await asyncio.sleep(30)

        monkeypatch.setattr(flic_agent, "find_button", slow)

        await asyncio.wait_for(agent.on_start(), timeout=1)

        assert KEY in agent._clients
        await agent.on_stop()

    async def test_two_pairings_at_once_do_not_take_the_same_button(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Chat calls the agent directly, outside its mailbox, so two requests
        # can be running together.
        agent, _published = make_agent(tmp_path, monkeypatch)

        async def discover(_timeout: float, _skip: Collection[str]) -> list[Any]:
            # A real scan waits on the radio, which is when the other request
            # gets to run.
            await asyncio.sleep(0.01)
            return [FakeDevice("AA:BB:CC:DD:EE:FF")]

        monkeypatch.setattr(flic_agent, "watch_for_buttons", discover)

        first, second = await asyncio.gather(
            agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen"),
            agent._handle_cmd(FlicAgentCommand.PAIR, name="hall"),
        )

        assert [b.address for b in agent._known_buttons] == ["AA:BB:CC:DD:EE:FF"]
        assert first.startswith("Paired") != second.startswith("Paired")

    async def test_a_command_that_has_to_wait_says_what_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pairing holds the lock while it watches; a `listen` asked for
        # meanwhile should not look as if it was never heard.
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button("hall", "11:22:33:44:55:66")]
        watching = asyncio.Event()
        release = asyncio.Event()
        said: list[str] = []

        async def watch(_timeout: float, _skip: Collection[str]) -> list[Any]:
            watching.set()
            await release.wait()
            return [FakeDevice("AA:BB:CC:DD:EE:FF")]

        async def notify(text: str, **_extra: Any) -> None:
            said.append(text)

        monkeypatch.setattr(flic_agent, "watch_for_buttons", watch)
        monkeypatch.setattr(agent, "notify_user", notify)

        pairing = asyncio.create_task(agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen"))
        await watching.wait()
        listening = asyncio.create_task(agent._handle_cmd(FlicAgentCommand.LISTEN))
        await asyncio.sleep(0.01)

        assert said[-1] == "Waiting for 'pair' to finish, then doing 'listen'."
        assert not listening.done()

        release.set()
        assert (await pairing).startswith("Paired 'kitchen'")
        assert (await listening).startswith("Listening to")
        assert agent._busy_with is None

    async def test_a_command_with_nothing_ahead_of_it_says_nothing_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        said: list[str] = []

        async def notify(text: str, **_extra: Any) -> None:
            said.append(text)

        monkeypatch.setattr(agent, "notify_user", notify)

        await agent._handle_cmd(FlicAgentCommand.LISTEN)

        assert said == []
        await agent.on_stop()

    async def test_several_waiting_buttons_are_counted_properly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        monkeypatch.setattr(
            flic_agent,
            "watch_for_buttons",
            _found("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66", "77:88:99:AA:BB:CC"),
        )

        reply = await agent._handle_cmd(FlicAgentCommand.PAIR)

        assert "2 more buttons were in pairing mode" in reply

    async def test_an_action_nobody_knows_is_not_reported_as_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        sent: list[Any] = []

        async def record(_target: str, _kind: MessageType, payload: Any) -> None:
            sent.append(payload)

        monkeypatch.setattr(agent, "send", record)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload={"action": "levitate"})
        )

        assert sent[0]["ok"] is False
        assert sent[0]["result"] == flic_agent.HELP_TEXT

    async def test_a_twist_keeps_its_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _published = make_agent(tmp_path, monkeypatch)
        twist = replace(button(), device_type="twist", twist_push_mode="selector")
        agent._known_buttons = [twist]

        await agent._listen()

        assert _client(agent, "kitchen").push_twist_mode == "selector"

    def test_a_twist_reports_its_battery_in_millivolts(self) -> None:
        twist = replace(button(), device_type="twist", battery=2950)

        assert flic_agent.battery_volts(twist) == 2.95

    def test_no_reading_is_not_zero_volts(self) -> None:
        assert flic_agent.battery_volts(replace(button(), battery=0)) is None


class TestStopIsKept:
    """A stop someone asked for outlives a restart; a shutdown is not a stop."""

    async def test_a_restart_after_stop_stays_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first, _published = make_agent(tmp_path, monkeypatch)
        first._known_buttons = [button()]
        await first._handle_cmd(FlicAgentCommand.LISTEN)
        await first._handle_cmd(FlicAgentCommand.STOP)

        second, _again = make_agent(tmp_path, monkeypatch)
        await second.on_start()

        assert second._clients == {}
        assert second._listening is False
        await second.on_stop()

    async def test_listen_undoes_it_for_the_next_restart_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first, _published = make_agent(tmp_path, monkeypatch)
        first._known_buttons = [button()]
        await first._handle_cmd(FlicAgentCommand.STOP)
        await first._handle_cmd(FlicAgentCommand.LISTEN)

        second, _again = make_agent(tmp_path, monkeypatch)
        await second.on_start()

        assert KEY in second._clients
        await second.on_stop()

    async def test_shutting_down_is_not_a_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first, _published = make_agent(tmp_path, monkeypatch)
        first._known_buttons = [button()]
        await first._remember()
        await first.on_start()
        await first.on_stop()

        second, _again = make_agent(tmp_path, monkeypatch)
        await second.on_start()

        assert KEY in second._clients
        await second.on_stop()

    async def test_pairing_ends_a_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pairing starts listening to the new button, so the stored choice
        # has to say the same.
        agent, _published = make_agent(tmp_path, monkeypatch)
        await agent._handle_cmd(FlicAgentCommand.STOP)
        monkeypatch.setattr(flic_agent, "watch_for_buttons", _found("AA:BB:CC:DD:EE:FF"))

        await agent._handle_cmd(FlicAgentCommand.PAIR, name="kitchen")

        assert json.loads(agent._buttons_json.read_text(encoding="utf-8"))["listening"] is True

    def test_a_file_from_before_the_choice_was_stored_means_listen(self) -> None:
        assert flic_agent.listening_from_store({"buttons": []}) is True
        assert flic_agent.listening_from_store([]) is True
        assert flic_agent.listening_from_store({"listening": False}) is False


class TestLatePresses:
    def test_they_are_off_unless_the_spawn_config_asks(self, tmp_path: Path) -> None:
        assert FlicAgent(persistence_dir=str(tmp_path)).publish_queued is False
        on = FlicAgent(persistence_dir=str(tmp_path), publish_queued=True)
        assert on.publish_queued is True


class FakeScanner:
    """A `BleakScanner` whose adverts a test plays in, after it has started."""

    instances: ClassVar[list["FakeScanner"]] = []
    fail_to_stop: ClassVar[bool] = False

    def __init__(self, detection_callback: Callable[[Any, Any], None], service_uuids: list[str]):
        self.callback = detection_callback
        self.service_uuids = service_uuids
        self.started = 0
        self.stopped = 0
        FakeScanner.instances.append(self)

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1
        if FakeScanner.fail_to_stop:
            raise OSError("[org.bluez.Error.InProgress] Operation already in progress")

    def advertise(self, address: str) -> None:
        self.callback(FakeDevice(address), None)


class TestWatchingForAButton:
    """`pair` watches with one scan, which some controllers need to survive."""

    @pytest.fixture(autouse=True)
    def _bleak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeScanner.instances = []
        FakeScanner.fail_to_stop = False
        module = types.ModuleType("bleak")
        module.BleakScanner = FakeScanner  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "bleak", module)

    @staticmethod
    async def _scanner() -> FakeScanner:
        for _ in range(100):
            if FakeScanner.instances and FakeScanner.instances[0].started:
                return FakeScanner.instances[0]
            await asyncio.sleep(0.001)
        raise AssertionError("the scan never started")

    async def test_it_ends_as_soon_as_a_new_button_appears(self) -> None:
        watch = asyncio.create_task(flic_agent.watch_for_buttons(30.0, set()))
        scanner = await self._scanner()

        scanner.advertise("AA:BB:CC:DD:EE:FF")
        found = await asyncio.wait_for(watch, timeout=1)

        assert [device.address for device in found] == ["AA:BB:CC:DD:EE:FF"]
        assert (scanner.started, scanner.stopped) == (1, 1)
        assert scanner.service_uuids == [
            flic_agent.FLIC_SERVICE_UUID,
            flic_agent.TWIST_SERVICE_UUID,
        ]

    async def test_a_button_it_already_knows_does_not_end_it(self) -> None:
        watch = asyncio.create_task(flic_agent.watch_for_buttons(30.0, {"aa:bb:cc:dd:ee:ff"}))
        scanner = await self._scanner()

        scanner.advertise("AA:BB:CC:DD:EE:FF")
        await asyncio.sleep(0.01)
        assert not watch.done()

        scanner.advertise("11:22:33:44:55:66")
        found = await asyncio.wait_for(watch, timeout=1)

        # One scan the whole time: switching it on and off is what upsets controllers.
        assert len(FakeScanner.instances) == 1
        assert [device.address for device in found] == ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]

    async def test_it_gives_up_at_the_timeout_with_what_it_saw(self) -> None:
        watch = asyncio.create_task(flic_agent.watch_for_buttons(0.05, {"aa:bb:cc:dd:ee:ff"}))
        scanner = await self._scanner()
        scanner.advertise("AA:BB:CC:DD:EE:FF")

        found = await asyncio.wait_for(watch, timeout=1)

        assert [device.address for device in found] == ["AA:BB:CC:DD:EE:FF"]
        assert scanner.stopped == 1

    async def test_a_scan_that_will_not_stop_keeps_what_it_saw(self) -> None:
        # The controller that refused its stop still saw the button.
        FakeScanner.fail_to_stop = True
        watch = asyncio.create_task(flic_agent.watch_for_buttons(30.0, set()))
        scanner = await self._scanner()

        scanner.advertise("AA:BB:CC:DD:EE:FF")
        found = await asyncio.wait_for(watch, timeout=1)

        assert [device.address for device in found] == ["AA:BB:CC:DD:EE:FF"]


class TestWhenBluetoothFails:
    """A scan that fails is not a button away: the search keeps checking often."""

    async def test_it_does_not_back_off_while_scanning_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flic_agent, "FIND_INTERVAL_S", 0.01)
        monkeypatch.setattr(flic_agent, "FIND_MAX_INTERVAL_S", 10.0)
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        lookups = 0

        async def no_adapter(_address: str, _timeout: float) -> Any:
            nonlocal lookups
            lookups += 1
            raise OSError("No Bluetooth adapters found.")

        monkeypatch.setattr(flic_agent, "find_button", no_adapter)
        agent._watch_all()
        await asyncio.sleep(0.3)

        # Backing off from 0.01 doubles past 0.3 in a handful of rounds; a
        # steady interval fits many more.
        assert lookups > 10
        await agent._stop(remember=False)

    async def test_the_button_comes_back_with_the_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flic_agent, "FIND_INTERVAL_S", 0.01)
        agent, _published = make_agent(tmp_path, monkeypatch)
        agent._known_buttons = [button()]
        adapter = False

        async def lookup(address: str, _timeout: float) -> Any:
            if not adapter:
                raise OSError("No Bluetooth adapters found.")
            return FakeDevice(address)

        monkeypatch.setattr(flic_agent, "find_button", lookup)
        with caplog_at_info() as records:
            agent._watch_all()
            await asyncio.sleep(0.05)
            adapter = True
            client = _client(agent, "kitchen")
            for _ in range(100):
                if client.devices_given:
                    break
                await asyncio.sleep(0.01)

        assert [device.address for device in client.devices_given] == ["AA:BB:CC:DD:EE:FF"]
        # Said when it starts and when it ends, not every round in between.
        assert len([r for r in records if "scanning is failing" in r]) == 1
        assert len([r for r in records if "works again" in r]) == 1
        await agent._stop(remember=False)
