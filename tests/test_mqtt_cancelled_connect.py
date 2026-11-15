# SPDX-License-Identifier: Apache-2.0
"""A connection its opener was cancelled while making is closed, not abandoned.

aiomqtt connects on a worker thread. Cancelled while that connect is still
running, ``async with`` is left before it is entered, so ``__aexit__`` never
runs: the socket the worker opens stays registered with the event loop, with
nothing left to close it. An agent stopped just after it starts is such a task.
The client Wactorz builds lets the connect finish and then closes it, without
holding up the cancelled caller.
"""

import asyncio
from typing import Any

import aiomqtt
import pytest

from wactorz.core import mqtt
from wactorz.core.mqtt import Client, mqtt_client


class _Broker:
    """Stands in for the connect and disconnect aiomqtt would make."""

    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.release_connect = asyncio.Event()
        self.closed = 0
        self.fail_connect = False

    async def enter(self, client: aiomqtt.Client) -> aiomqtt.Client:
        await self.release_connect.wait()
        if self.fail_connect:
            raise aiomqtt.MqttError("refused")
        self.connected.set()
        return client

    async def exit(self, *_args: Any) -> None:
        self.closed += 1


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> _Broker:
    broker = _Broker()

    async def _aenter(self: aiomqtt.Client) -> aiomqtt.Client:
        return await broker.enter(self)

    async def _aexit(self: aiomqtt.Client, *args: Any) -> None:
        await broker.exit(*args)

    monkeypatch.setattr(aiomqtt.Client, "__aenter__", _aenter)
    monkeypatch.setattr(aiomqtt.Client, "__aexit__", _aexit)
    return broker


async def _hold(client: aiomqtt.Client) -> None:
    async with client:
        await asyncio.sleep(3600)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.real_mqtt_client  # builds a client and never connects it
async def test_every_connection_is_built_as_one() -> None:
    assert isinstance(mqtt_client("localhost", 1883), Client)


async def test_a_connect_cancelled_part_way_is_closed_once_it_completes(broker: _Broker) -> None:
    task = asyncio.create_task(_hold(Client("localhost", 1883)))
    await _settle()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1)

    assert task in done
    assert task.cancelled()
    assert broker.closed == 0  # still connecting

    broker.release_connect.set()
    await _settle()

    assert broker.connected.is_set()
    assert broker.closed == 1


async def test_a_cancelled_connect_that_then_fails_has_nothing_to_close(broker: _Broker) -> None:
    broker.fail_connect = True
    task = asyncio.create_task(_hold(Client("localhost", 1883)))
    await _settle()
    task.cancel()
    await asyncio.wait({task}, timeout=1)

    broker.release_connect.set()
    await _settle()

    assert broker.closed == 0


async def test_an_uncancelled_connection_closes_as_usual(broker: _Broker) -> None:
    broker.release_connect.set()
    async with Client("localhost", 1883) as client:
        assert isinstance(client, Client)

    assert broker.closed == 1


async def test_a_close_that_never_finishes_is_given_up(
    broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mqtt, "ABANDONED_CONNECT_CLOSE_S", 0.05)
    hung = asyncio.Event()

    async def _never(*_args: Any) -> None:
        hung.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(broker, "exit", _never)
    task = asyncio.create_task(_hold(Client("localhost", 1883)))
    await _settle()
    task.cancel()
    await asyncio.wait({task}, timeout=1)
    broker.release_connect.set()

    await asyncio.wait_for(hung.wait(), 1)
    await asyncio.sleep(0.2)

    assert not mqtt._closing_abandoned
