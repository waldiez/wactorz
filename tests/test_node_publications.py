"""A node signs what it publishes, and main checks it against the node it expects.

Every node may write the topics agents share, so without this a message on a
reply topic, or a manifest under an agent's id, could come from any of them.
What is pinned here is the signature itself -- that it binds the node, the topic
and the payload, and cannot pass for one of main's commands -- and that the
node's publisher puts one on everything it sends. What main does with a message
that carries none is pinned where each kind is received: the heartbeat, the
manifest and the reply tests.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from wactorz.core import node_publications, node_signing
from wactorz.core.mqtt import user_properties
from wactorz.core.node_publications import (
    SIGNATURE_PROPERTY,
    STAMP_PROPERTY,
    PublicationSigner,
    Stamp,
    admit,
    signed_stamp,
)
from wactorz.node import publishing
from wactorz.node.signing import ControlGuard


@pytest.fixture(autouse=True)
def _fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test is an install of its own, with its own signing secret."""
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(node_signing, "_secret", None)
    monkeypatch.setattr(node_publications, "_reported", type(node_publications._reported)())


def _signer(node: str = "rpi") -> PublicationSigner:
    return PublicationSigner(bytes.fromhex(node_signing.node_key(node)))


def _signed(topic: str, payload: bytes, node: str = "rpi") -> dict[str, str]:
    return dict(_signer(node).properties(topic, payload))


class TestTheSignature:
    def test_it_is_the_node_s(self) -> None:
        properties = _signed("main/reply/abc", b'{"ok": true}')

        assert signed_stamp("rpi", "main/reply/abc", b'{"ok": true}', properties) is not None

    def test_another_node_s_key_does_not_make_it(self) -> None:
        properties = _signed("main/reply/abc", b"{}", node="sandbox")

        assert signed_stamp("rpi", "main/reply/abc", b"{}", properties) is None

    def test_it_covers_the_payload(self) -> None:
        properties = _signed("main/reply/abc", b'{"result": "1"}')

        assert signed_stamp("rpi", "main/reply/abc", b'{"result": "2"}', properties) is None

    def test_it_covers_the_topic(self) -> None:
        # Or one reply could be delivered as the answer to another task.
        properties = _signed("main/reply/abc", b"{}")

        assert signed_stamp("rpi", "main/reply/def", b"{}", properties) is None

    def test_it_covers_the_stamp(self) -> None:
        properties = _signed("nodes/rpi/heartbeat", b"{}")
        stamp = Stamp.parse(properties[STAMP_PROPERTY])
        assert stamp is not None
        properties[STAMP_PROPERTY] = str(Stamp(stamp.run, stamp.count + 1))

        assert signed_stamp("rpi", "nodes/rpi/heartbeat", b"{}", properties) is None

    def test_none_is_none(self) -> None:
        assert signed_stamp("rpi", "main/reply/abc", b"{}", {}) is None

    def test_it_cannot_pass_for_a_command_from_main(self, tmp_path: Path) -> None:
        # The node checks main's commands with the same key: a signature a node
        # made must not be one it would act on.
        topic, payload = "nodes/rpi/spawn", b'{"name": "x"}'
        properties = _signed(topic, payload)
        stamp = Stamp.parse(properties[STAMP_PROPERTY])
        assert stamp is not None
        as_command = {
            node_signing.SEQUENCE_PROPERTY: str(stamp.count),
            node_signing.SIGNATURE_PROPERTY: properties[SIGNATURE_PROPERTY],
        }
        guard = ControlGuard(node_signing.node_key("rpi"), "", "enforce", str(tmp_path))

        assert not guard.admit("spawn", topic, payload, as_command)


class TestTheStamp:
    def test_each_message_counts_up_within_a_run(self) -> None:
        signer = _signer()

        first = Stamp.parse(dict(signer.properties("t", b""))[STAMP_PROPERTY])
        second = Stamp.parse(dict(signer.properties("t", b""))[STAMP_PROPERTY])

        assert first is not None and second is not None
        assert (first.run, first.count + 1) == (second.run, second.count)

    def test_every_run_has_its_own_id(self) -> None:
        assert _signer().run != _signer().run

    @pytest.mark.parametrize("text", ["", "abc", "0123456789abcdef.0", "0123456789abcdef.x", "1.2"])
    def test_one_that_is_not_one_is_not_read(self, text: str) -> None:
        assert Stamp.parse(text) is None


class TestTakingANodeAtItsWord:
    def test_a_signed_message_is_taken(self) -> None:
        properties = _signed("main/reply/abc", b"{}")

        assert admit("rpi", "main/reply/abc", b"{}", properties, "a reply")

    def test_an_unsigned_one_is_refused_under_enforce(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(node_publications, "NODE_SIGNING", "enforce")

        assert not admit("rpi", "main/reply/abc", b"{}", {}, "a reply")

    def test_an_unsigned_one_is_taken_under_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(node_publications, "NODE_SIGNING", "warn")

        assert admit("rpi", "main/reply/abc", b"{}", {}, "a reply")

    def test_a_refusal_is_said_once_per_node_and_kind_at_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(node_publications, "NODE_SIGNING", "enforce")

        with caplog.at_level(logging.DEBUG, logger="wactorz.core.node_publications"):
            for _ in range(3):
                admit("rpi", "main/reply/abc", b"{}", {}, "a reply")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "deploying again" in warnings[0].getMessage()


class _Client:
    """A paho client that records what it was given."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, Any]] = []

    def publish(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False, properties: Any = None
    ) -> None:
        self.sent.append((topic, payload, properties))


class _Received:
    def __init__(self, properties: Any) -> None:
        self.properties = properties


async def _send(publisher: publishing.NodePublisher, topic: str, payload: Any) -> _Client:
    publisher._queue = asyncio.Queue()
    client = _Client()
    await publisher.publish(topic, payload)
    await publisher.publish_one_queued(client)
    return client


class TestTheNodePublisher:
    async def test_it_signs_what_it_sends(self) -> None:
        publisher = publishing.NodePublisher("localhost", 1883, "rpi")
        publisher.signer = _signer()

        client = await _send(publisher, "agents/abc/manifest", {"name": "camera"})

        ((topic, payload, properties),) = client.sent
        received = user_properties(_Received(properties))
        assert signed_stamp("rpi", topic, payload, received) is not None

    async def test_it_signs_what_an_agent_hands_it_with_properties_of_its_own(self) -> None:
        # An agent's code may pass properties; the node's signature is what goes.
        publisher = publishing.NodePublisher("localhost", 1883, "rpi")
        publisher.signer = _signer()
        publisher._queue = asyncio.Queue()
        client = _Client()

        await publisher.publish("main/reply/abc", b"{}", user_properties=[("x", "y")])
        await publisher.publish_one_queued(client)

        received = user_properties(_Received(client.sent[0][2]))
        assert set(received) == {STAMP_PROPERTY, SIGNATURE_PROPERTY}

    async def test_a_node_without_a_key_sends_unsigned(self) -> None:
        publisher = publishing.NodePublisher("localhost", 1883, "rpi")

        client = await _send(publisher, "agents/abc/manifest", {"name": "camera"})

        assert client.sent[0][2] is None


class TestTheGuardsSigner:
    def test_a_node_with_a_key_has_one(self, tmp_path: Path) -> None:
        guard = ControlGuard(node_signing.node_key("rpi"), "", "enforce", str(tmp_path))

        signer = guard.publication_signer()

        assert signer is not None
        properties = dict(signer.properties("t", b"x"))
        assert signed_stamp("rpi", "t", b"x", properties) is not None

    @pytest.mark.parametrize("key", ["", "not hex"])
    def test_a_node_with_no_usable_key_has_none(self, tmp_path: Path, key: str) -> None:
        assert ControlGuard(key, "", "enforce", str(tmp_path)).publication_signer() is None
