"""Messages as a node publishes them: signed with its key, as main receives them.

Main takes a node's heartbeat, report, manifest or reply only when the node
signed it (see `wactorz.core.node_publications`), so a test that hands main a
message standing in for one off the broker signs it here, the way the node's
publisher does.
"""

from typing import Any

from paho.mqtt.properties import Properties

from wactorz.core.mqtt import publish_properties
from wactorz.core.node_publications import PublicationSigner
from wactorz.core.node_signing import node_key

#: One signer per node and key, so a node's messages count up within one run as
#: a real node's do. Keyed by the key too: a test that mints a fresh install
#: secret gets a fresh signer.
_signers: dict[tuple[str, str], PublicationSigner] = {}


def signed_properties(node: str, topic: str, payload: bytes) -> Properties:
    """The properties ``node`` would publish ``payload`` on ``topic`` with."""
    key = node_key(node)
    signer = _signers.setdefault((node, key), PublicationSigner(bytes.fromhex(key)))
    return publish_properties(signer.properties(topic, payload))


class SignedMessage:
    """A received message standing in for aiomqtt's, signed by ``node``.

    ``node`` defaults to the one a ``nodes/<node>/...`` topic names, and
    ``signed=False`` sends it with no signature at all. Signed when
    first read rather than when built, as a node signs a message when it sends
    it: a test may build its messages in any order, and they count up in the
    order main reads them.
    """

    def __init__(
        self, topic: str, payload: Any, node: str | None = None, *, signed: bool = True
    ) -> None:
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else str(payload).encode()
        self.node = node if node is not None else topic.split("/")[1]
        self.signed = signed
        self._properties: Properties | None = None

    @property
    def properties(self) -> Properties | None:
        if not self.signed:
            return None
        if self._properties is None:
            self._properties = signed_properties(self.node, self.topic, self.payload)
        return self._properties
