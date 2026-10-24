"""Signing what a node publishes, so main can tell which node said it.

Every node's broker account may write the topics agents share -- a task's
reply, an agent's manifest -- and on a broker Wactorz does not configure, its
own heartbeat topics are open to anyone as well. Without a signature, main takes
whatever arrives there as the word of the agent or node it names: the first
reply on a reply topic answers the task, a manifest for any name becomes a
contract the planner wires from, and a heartbeat decides which agents a node is
running. Code running on one node, the sandbox included, could speak for every
other.

So a node signs everything it publishes with its node key, the one ``/deploy``
gives it to check main's commands with, and main checks the signature against
the node it expects to hear from: the one its own records place the agent on,
or the one whose topic it is. A signature says which node published a message,
and nothing about which of that node's agents did -- the boundary is the
machine, as it is for the broker account.

The signature travels as MQTT v5 user properties, over the topic, a stamp and
the payload exactly as sent, as main's own signatures on commands do (see
:mod:`wactorz.core.node_signing`). The input starts with a context of its own,
so a node's signature can never be offered back to a node as one of main's
commands. The stamp is a run id, random for each start of the node's runner,
and a count within that run. It lets main refuse a heartbeat delivered twice
without trusting the node's clock, which on a board without a battery-backed
one may be years out until it reaches a time server.

What main does with a message that carries no valid signature follows
``WACTORZ_NODE_SIGNING``, as for every other check between main and a node:
``enforce`` refuses it, ``warn`` takes it and says so. :func:`admit` is that
rule, for the callers that need only a yes or no.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from ..config import NODE_SIGNING
from .node_signing import node_key

logger = logging.getLogger(__name__)

#: The user properties a node's publication carries. Named apart from the ones
#: on main's commands, so a reader of a packet can tell which way it went.
STAMP_PROPERTY = "wactorz-node-stamp"
SIGNATURE_PROPERTY = "wactorz-node-sig"

#: Binds a signature to this use: see the module docstring.
_CONTEXT = b"wactorz-node-publication-v1\n"

#: How many (node, kind of message) pairs to remember having reported. The node
#: is whoever the caller expected, which a sender can steer by the names it
#: uses, so the memory is bounded; one forgotten is at worst reported again.
REPORTED_MEMORY = 1024

_reported: OrderedDict[tuple[str, str, str], None] = OrderedDict()

#: A stamp as written: the run id, a dot, the count.
_STAMP = re.compile(r"(?P<run>[0-9a-f]{16})\.(?P<count>[1-9][0-9]{0,18})")


@dataclass(frozen=True)
class Stamp:
    """Which start of a node's runner published a message, and in what order."""

    run: str
    count: int

    def __str__(self) -> str:
        return f"{self.run}.{self.count}"

    @classmethod
    def parse(cls, text: str) -> Stamp | None:
        """The stamp ``text`` spells, or None if it spells none."""
        found = _STAMP.fullmatch(text)
        if found is None:
            return None
        return cls(found.group("run"), int(found.group("count")))


def signing_input(topic: str, stamp: str, payload: bytes) -> bytes:
    """What a publication's signature covers: the topic, the stamp and the payload as sent."""
    return _CONTEXT + b"\n".join((topic.encode("utf-8"), stamp.encode("ascii"), payload))


def _digest(key: bytes, topic: str, stamp: str, payload: bytes) -> str:
    return hmac.new(key, signing_input(topic, stamp, payload), hashlib.sha256).hexdigest()


class PublicationSigner:
    """A node's half: signs each message as it is handed to the broker.

    Called in the order messages are sent, so the count says that order. Not
    thread-safe, and need not be: one publisher sends them, from one loop.
    """

    def __init__(self, key: bytes, run: str | None = None) -> None:
        self._key = key
        self.run = run or secrets.token_hex(8)
        self._count = 0

    def properties(self, topic: str, payload: bytes) -> list[tuple[str, str]]:
        """The user properties that sign ``payload`` on ``topic``."""
        self._count += 1
        stamp = str(Stamp(self.run, self._count))
        return [
            (STAMP_PROPERTY, stamp),
            (SIGNATURE_PROPERTY, _digest(self._key, topic, stamp, payload)),
        ]


def signed_stamp(
    node: str, topic: str, payload: bytes, properties: Mapping[str, str]
) -> Stamp | None:
    """The stamp of a message signed with ``node``'s key, or None if it is not one."""
    signature = properties.get(SIGNATURE_PROPERTY)
    raw_stamp = properties.get(STAMP_PROPERTY, "")
    stamp = Stamp.parse(raw_stamp)
    if not node or signature is None or stamp is None:
        return None
    try:
        key = bytes.fromhex(node_key(node))
    except (OSError, ValueError):
        logger.exception("[nodes] Could not derive %r's key to check what it published", node)
        return None
    expected = _digest(key, topic, raw_stamp, payload)
    if not hmac.compare_digest(signature.encode("utf-8", "replace"), expected.encode("ascii")):
        return None
    return stamp


def admit(node: str, topic: str, payload: bytes, properties: Mapping[str, str], what: str) -> bool:
    """Whether to take ``payload`` on ``topic`` as ``node``'s word.

    Signed with its key, it is. Otherwise ``WACTORZ_NODE_SIGNING`` decides, and
    the log says which way, at warning the first time for a node and a kind of
    message and at debug after that: ``what`` names the kind.
    """
    if signed_stamp(node, topic, payload, properties) is not None:
        return True
    if NODE_SIGNING == "enforce":
        _report(
            (node, what, "refused"),
            "[main] Refused %s on %s: not signed by node %r (WACTORZ_NODE_SIGNING=enforce). "
            "A node deployed before nodes signed what they publish needs deploying again.",
            what,
            topic,
            node,
        )
        return False
    _report(
        (node, what, "taken"),
        "[main] Took %s on %s although node %r did not sign it (WACTORZ_NODE_SIGNING=warn)",
        what,
        topic,
        node,
    )
    return True


def _report(key: tuple[str, str, str], message: str, *args: object) -> None:
    if key in _reported:
        logger.debug(message, *args)
        return
    _reported[key] = None
    while len(_reported) > REPORTED_MEMORY:
        _reported.popitem(last=False)
    logger.warning(message, *args)
