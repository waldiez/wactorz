"""Signing the control messages main sends to a remote node.

A node acts on whatever arrives on its control topics: a spawn runs the code in
it, a desired state starts every agent it lists, and a stop can delete an agent's
state. The broker admits every client holding its account, and by default a node
holds the same account as the server, so without this anything that can publish
to the broker -- another node included -- can run code on every node.

Each node gets its own key, derived from a secret this install mints once and
keeps in its state directory. ``/deploy`` writes the key into the node's ``.env``
over SSH, the one channel that is authenticated before the key exists. What a
node holding a key does with a message not signed for it is set when it is
deployed (``WACTORZ_NODE_SIGNING``): ``warn`` acts on it and reports it,
``enforce`` refuses it. A node deployed before signing holds no key and acts on
everything, as it always has. Main signs either way.

The signature and its sequence number travel as MQTT v5 user properties, computed
over the payload exactly as it is sent. The payload itself is untouched, so a node
that cannot check a signature reads every message as it always did, and nothing
has to agree on how JSON is written. Mosquitto keeps user properties with a
retained message and with one queued for an offline session, so a retained desired
state and a spawn delivered on reconnect both arrive signed.

A signature covers the topic, so a message signed as one command cannot be
delivered as another, and a sequence number the node remembers, so a captured
message cannot be delivered twice. The sequence is this server's clock in
microseconds, kept strictly increasing, so a node needs no clock of its own to
check it -- a board without a battery-backed clock may boot into the wrong year.

The runner holds the other half and cannot import this module, since it is a
single file copied to the node, so the rule is written in both places.
``tests/test_node_control_signing.py`` holds the two copies to each other.
"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .mqtt import publish_properties
from .paths import ensure_state_dir

logger = logging.getLogger(__name__)

#: The topics under ``nodes/<node>/`` that main signs and a keyed node checks.
CONTROL_LEAVES = frozenset(
    {"spawn", "desired_state", "stop", "stop_all", "restart", "restart_agent", "migrate"}
)

#: The user properties a signed message carries.
SEQUENCE_PROPERTY = "wactorz-seq"
SIGNATURE_PROPERTY = "wactorz-sig"

#: The install's signing secret, under the state directory.
KEY_FILE = "node_signing.key"

#: The newest sequence number handed out, so a restart does not hand one out again.
SEQUENCE_FILE = "node_signing.seq"

#: Prefixed to a node's name when its key is derived, binding the key to this use.
_KEY_CONTEXT = b"wactorz-node-control-v1:"

#: How long a starting process waits for another one's freshly minted secret.
_READ_ATTEMPTS = 50
_READ_PAUSE_S = 0.01

_lock = threading.Lock()
_secret: bytes | None = None
_last_sequence: int | None = None


class UnreadableSigningKeyError(ValueError):
    """The install's signing secret is present but does not hold a key."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path} does not hold a node signing key. Restore it from a backup, or delete "
            "it and deploy every node again: a replacement key signs nothing the deployed "
            "nodes will accept."
        )


def control_leaf(topic: str) -> str | None:
    """The control command ``topic`` names, or None if it is not a node's control topic."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "nodes" and parts[1] and parts[2] in CONTROL_LEAVES:
        return parts[2]
    return None


def node_key(node: str) -> str:
    """The key ``node`` checks signatures with, as hex, for its ``.env``."""
    derived = hmac.new(_install_secret(), _KEY_CONTEXT + node.encode("utf-8"), hashlib.sha256)
    return derived.hexdigest()


def next_sequence() -> int:
    """A sequence number larger than any this install has handed out before."""
    global _last_sequence
    with _lock:
        path = Path(ensure_state_dir()) / SEQUENCE_FILE
        if _last_sequence is None:
            _last_sequence = _read_sequence(path)
        _last_sequence = max(_last_sequence + 1, time.time_ns() // 1000)
        # Written on every call: control messages come at an operator's pace, and
        # a number handed out but not recorded could be handed out again after a
        # restart whose clock had stepped back.
        _write_atomically(path, str(_last_sequence))
        return _last_sequence


def signing_input(topic: str, sequence: int, payload: bytes) -> bytes:
    """What a signature covers: the topic, the sequence number and the payload as sent."""
    return b"\n".join((topic.encode("utf-8"), str(sequence).encode("ascii"), payload))


def node_control_properties(topic: str, payload: Any) -> list[tuple[str, str]] | None:
    """The user properties that sign ``payload`` on ``topic``, or None if it needs none.

    None for any topic that is not a node's control topic, and for the empty payload
    that clears a retained message, which instructs nothing. ``payload`` is taken as
    it will be published: bytes as they are, anything else as its UTF-8 text.
    """
    if control_leaf(topic) is None:
        return None
    raw = _as_bytes(payload)
    if not raw:
        return None
    try:
        key = bytes.fromhex(node_key(topic.split("/")[1]))
        sequence = next_sequence()
    except (OSError, ValueError):
        # Sent rather than dropped: a node deployed before signing still acts on
        # it, and one holding a key reports it or refuses it.
        logger.exception(
            "[nodes] Could not sign %s; sending it unsigned, which a node holding a key reports",
            topic,
        )
        return None
    signature = hmac.new(key, signing_input(topic, sequence, raw), hashlib.sha256).hexdigest()
    return [(SEQUENCE_PROPERTY, str(sequence)), (SIGNATURE_PROPERTY, signature)]


def signed_publish_kwargs(topic: str, payload: Any) -> dict[str, Any]:
    """Keyword arguments for an aiomqtt publish of ``payload``: its signature, if it needs one."""
    pairs = node_control_properties(topic, payload)
    return {"properties": publish_properties(pairs)} if pairs else {}


def _as_bytes(payload: Any) -> bytes:
    """``payload`` as the bytes a publish sends, the way paho converts it."""
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    return str(payload).encode("utf-8")


def _install_secret() -> bytes:
    global _secret
    with _lock:
        if _secret is None:
            _secret = _load_or_mint(Path(ensure_state_dir()) / KEY_FILE)
        return _secret


def _load_or_mint(path: Path) -> bytes:
    """Read the install's secret, minting it on first use.

    Created with ``O_CREAT | O_EXCL`` so two starting processes agree: the one
    that loses the race reads the winner's. Unlike the install id, a secret that
    cannot be read or written is never replaced by a fresh one. Every node
    deployed with a key derived from it would reject what a replacement signed,
    so a missing or damaged file is an error to see rather than a value to
    regenerate quietly.
    """
    minted = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_secret(path)
    with os.fdopen(fd, "wb") as handle:
        handle.write(minted.hex().encode("ascii"))
    return minted


def _read_secret(path: Path) -> bytes:
    """Read a secret another process may still be writing, tolerating its empty window."""
    for _ in range(_READ_ATTEMPTS):
        text = path.read_text(encoding="ascii").strip()
        if text:
            secret = bytes.fromhex(text)
            if len(secret) != 32:
                raise UnreadableSigningKeyError(path)
            return secret
        time.sleep(_READ_PAUSE_S)
    raise UnreadableSigningKeyError(path)


def _read_sequence(path: Path) -> int:
    """The newest sequence number recorded, or 0 when none has been."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return 0
    try:
        recorded = int(text or 0)
    except ValueError:
        # The clock alone keeps numbers increasing across a restart; the file only
        # covers a clock that stepped back, so a damaged one is not fatal.
        logger.warning("[nodes] %s does not hold a sequence number; starting from the clock", path)
        recorded = 0
    return recorded


def _write_atomically(path: Path, text: str) -> None:
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_text(text, encoding="ascii")
    staging.replace(path)
