"""What the generated access list lets each account do, asked of a real broker.

`tests/test_broker_accounts.py` pins the text of the file. This asks mosquitto
what the text means: every rule is checked by whether a message one account
publishes reaches another that subscribed, on a broker loaded with the accounts
and the list `acl_fixture` writes.

A second broker loads the list a server with no account writes, where the whole
broker goes to clients that give no account instead.

Skipped unless ``WACTORZ_TEST_ACL_BROKER=host:port`` and
``WACTORZ_TEST_ACL_UNNAMED_BROKER=host:port`` name those brokers and
``WACTORZ_TEST_ACL_STATE`` the state directory their node passwords were derived
under. ``make test-broker`` provides all three.
"""

import asyncio
import os
import uuid

import aiomqtt
import pytest

from wactorz.core import broker_accounts, node_signing

from .acl_fixture import LISTED, PASSWORD, SERVER, UNLISTED

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(120)]

#: How long a message that should arrive is waited for, and how long one that
#: should not is given to turn up anyway.
ARRIVES_S = 10.0
ABSENT_S = 1.0


#: A client that gives no account.
NO_ACCOUNT = None


def _broker(monkeypatch: pytest.MonkeyPatch, variable: str) -> tuple[str, int]:
    """The broker ``variable`` names, with the install its node passwords came from."""
    address = os.environ.get(variable, "").strip()
    state = os.environ.get("WACTORZ_TEST_ACL_STATE", "").strip()
    if not (address and state):
        pytest.skip(f"set {variable} and WACTORZ_TEST_ACL_STATE, or run `make test-broker`")
    monkeypatch.setenv("WACTORZ_STATE_DIR", state)
    monkeypatch.setattr(node_signing, "_secret", None)
    host, _, port = address.rpartition(":")
    return host, int(port)


@pytest.fixture(name="locked")
def locked_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[str, int]:
    """The broker where the server has an account and no client goes without one."""
    return _broker(monkeypatch, "WACTORZ_TEST_ACL_BROKER")


@pytest.fixture(name="unnamed")
def unnamed_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[str, int]:
    """The broker of a server that connects with no account."""
    return _broker(monkeypatch, "WACTORZ_TEST_ACL_UNNAMED_BROKER")


def _password(account: str) -> str:
    return broker_accounts.password(account) if account.startswith("node-") else PASSWORD


def _client(broker: tuple[str, int], account: str | None) -> aiomqtt.Client:
    host, port = broker
    if account is None:
        return aiomqtt.Client(host, port)
    return aiomqtt.Client(host, port, username=account, password=_password(account))


async def _hears_statistics(broker: tuple[str, int], account: str | None, wait: float) -> bool:
    """Whether ``account`` is sent the broker's own statistics within ``wait``."""
    async with _client(broker, account) as client:
        await client.subscribe("$SYS/broker/uptime")
        try:
            await asyncio.wait_for(anext(aiter(client.messages)), timeout=wait)
        except asyncio.TimeoutError:
            return False
        return True


async def _delivered(
    broker: tuple[str, int], sender: str | None, topic: str, receiver: str | None, wait: float
) -> bool:
    """Whether a message ``sender`` publishes on ``topic`` reaches ``receiver``."""
    marker = uuid.uuid4().hex
    async with _client(broker, receiver) as listening:
        await listening.subscribe(topic)
        async with _client(broker, sender) as publishing:
            await publishing.publish(topic, marker, qos=1)

        async def _wait() -> bool:
            async for message in listening.messages:
                if message.payload == marker.encode():
                    return True
            return False

        try:
            return await asyncio.wait_for(_wait(), timeout=wait)
        except asyncio.TimeoutError:
            return False


async def _may_write(
    broker: tuple[str, int], account: str | None, topic: str, server: str | None = SERVER
) -> bool:
    """The server can read anything, so it tells whether a write got through."""
    return await _delivered(broker, account, topic, server, ARRIVES_S)


async def _may_not_write(
    broker: tuple[str, int], account: str | None, topic: str, server: str | None = SERVER
) -> bool:
    return not await _delivered(broker, account, topic, server, ABSENT_S)


async def _may_read(
    broker: tuple[str, int], account: str | None, topic: str, server: str | None = SERVER
) -> bool:
    """The server can write anything, so it tells whether a read is allowed."""
    return await _delivered(broker, server, topic, account, ARRIVES_S)


async def _may_not_read(
    broker: tuple[str, int], account: str | None, topic: str, server: str | None = SERVER
) -> bool:
    return not await _delivered(broker, server, topic, account, ABSENT_S)


class TestANodesOwnTree:
    async def test_it_reads_and_writes_its_own(self, locked: tuple[str, int]) -> None:
        assert await _may_write(locked, "node-a", "nodes/node-a/heartbeat")
        assert await _may_read(locked, "node-a", "nodes/node-a/spawn")

    async def test_it_can_neither_drive_nor_watch_another_node(
        self, locked: tuple[str, int]
    ) -> None:
        assert await _may_not_write(locked, "node-a", "nodes/node-b/spawn")
        assert await _may_not_read(locked, "node-a", "nodes/node-b/heartbeat")


class TestWhatAgentsShare:
    async def test_it_reads_and_writes_agent_traffic(self, locked: tuple[str, int]) -> None:
        assert await _may_write(locked, "node-a", "agents/abc/status")
        assert await _may_read(locked, "node-a", "agents/by-name/echo/task")

    async def test_the_deny_beats_the_allow_around_it(self, locked: tuple[str, int]) -> None:
        # `agents/#` is allowed and `agents/+/commands` denied: the commands that
        # stop the server's own agents stay out of a node's reach.
        assert await _may_not_write(locked, "node-a", "agents/abc/commands")
        assert await _may_not_read(locked, "node-a", "agents/abc/commands")


class TestAskingMain:
    @pytest.mark.parametrize("topic", ["main/llm_request", "main/reply/abc/123"])
    async def test_it_writes_and_never_reads(self, locked: tuple[str, int], topic: str) -> None:
        # Reading would let a node watch what the others answer, and learn a
        # reply topic to forge an answer into.
        assert await _may_write(locked, "node-a", topic)
        assert await _may_not_read(locked, "node-a", topic)

    async def test_the_rest_of_main_is_closed(self, locked: tuple[str, int]) -> None:
        assert await _may_not_write(locked, "node-a", "main/anything-else")


class TestData:
    @pytest.mark.parametrize("topic", ["custom/x", "sensors/temp", "home/state/a", "plant/soil"])
    async def test_the_conventional_and_the_added_prefixes_are_open(
        self, locked: tuple[str, int], topic: str
    ) -> None:
        assert await _may_write(locked, "node-a", topic)
        assert await _may_read(locked, "node-a", topic)

    @pytest.mark.parametrize("topic", ["homeassistant/state_changes/light/x", "weather/today"])
    async def test_a_read_only_prefix_is_read_and_not_written(
        self, locked: tuple[str, int], topic: str
    ) -> None:
        assert await _may_read(locked, "node-a", topic)
        assert await _may_not_write(locked, "node-a", topic)

    @pytest.mark.parametrize("topic", ["zigbee2mqtt/lock/set", "system/health", "anything/else"])
    async def test_what_is_not_named_is_closed(self, locked: tuple[str, int], topic: str) -> None:
        # What a stolen node must not reach: another system on the same broker.
        assert await _may_not_write(locked, "node-a", topic)
        assert await _may_not_read(locked, "node-a", topic)

    async def test_a_node_does_not_read_the_brokers_statistics(
        self, locked: tuple[str, int]
    ) -> None:
        assert await _hears_statistics(locked, SERVER, ARRIVES_S)
        assert not await _hears_statistics(locked, "node-a", 3.0)


class TestTheOtherAccounts:
    async def test_a_listed_account_has_the_whole_broker(self, locked: tuple[str, int]) -> None:
        assert await _may_write(locked, LISTED, "zigbee2mqtt/lock/set")
        assert await _may_read(locked, LISTED, "nodes/node-a/heartbeat")

    async def test_an_account_the_list_does_not_name_has_nothing(
        self, locked: tuple[str, int]
    ) -> None:
        assert await _may_not_write(locked, UNLISTED, "custom/x")
        assert await _may_not_read(locked, UNLISTED, "custom/x")


class TestAServerWithNoAccount:
    """The whole broker goes to clients that give no account; a node is still penned in."""

    async def test_a_client_with_no_account_has_the_whole_broker(
        self, unnamed: tuple[str, int]
    ) -> None:
        # The server reaches a node's control topics, and whatever else shares
        # the broker, as it would with an account of its own.
        assert await _delivered(unnamed, NO_ACCOUNT, "nodes/node-a/spawn", NO_ACCOUNT, ARRIVES_S)
        assert await _delivered(unnamed, NO_ACCOUNT, "zigbee2mqtt/lock/set", NO_ACCOUNT, ARRIVES_S)
        assert await _hears_statistics(unnamed, NO_ACCOUNT, ARRIVES_S)

    async def test_a_node_and_the_server_reach_each_other(self, unnamed: tuple[str, int]) -> None:
        assert await _may_write(unnamed, "node-a", "nodes/node-a/heartbeat", NO_ACCOUNT)
        assert await _may_read(unnamed, "node-a", "nodes/node-a/spawn", NO_ACCOUNT)
        assert await _may_write(unnamed, "node-a", "main/llm_request", NO_ACCOUNT)

    async def test_a_node_gets_none_of_what_the_unnamed_clients_have(
        self, unnamed: tuple[str, int]
    ) -> None:
        # The rules outside a `user` block are for clients with no account, and
        # do not add to what a named one may do.
        assert await _may_not_write(unnamed, "node-a", "nodes/node-b/spawn", NO_ACCOUNT)
        assert await _may_not_read(unnamed, "node-a", "nodes/node-b/heartbeat", NO_ACCOUNT)
        assert await _may_not_write(unnamed, "node-a", "zigbee2mqtt/lock/set", NO_ACCOUNT)
        assert await _may_not_write(unnamed, "node-a", "agents/abc/commands", NO_ACCOUNT)
        assert not await _hears_statistics(unnamed, "node-a", 3.0)
