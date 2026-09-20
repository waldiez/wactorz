"""Which control messages this node acts on.

The signing rule itself lives in :mod:`wactorz.core.node_signing`, which main
uses to sign and this uses to check — one definition of what a signature covers,
rather than two that have to be kept in step. What belongs here is the receiving
half: the record of which sequence numbers this node has already accepted, and
what it does with a message that does not check out.
"""

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from ..core.atomic_io import write_text
from ..core.node_signing import SEQUENCE_PROPERTY, SIGNATURE_PROPERTY, signing_input

logger = logging.getLogger(__name__)

#: The topics whose empty payload is a retained message being cleared, which
#: their handlers ignore. Every other control topic acts on an empty payload --
#: `stop_all` shuts the node down whatever it carries -- so it is checked too.
CLEARABLE_LEAVES = frozenset({"spawn", "desired_state"})

#: How far behind the newest message already accepted on a topic another may
#: arrive and still be accepted, in microseconds. Main publishes through more
#: than one broker connection, and two connections need not deliver in the order
#: they were written to.
REORDER_WINDOW_US = 300 * 1_000_000

#: How many accepted sequence numbers are remembered per topic.
SEEN_PER_TOPIC = 256

#: Where accepted sequence numbers are kept, under the node's state directory, so
#: a message accepted before a restart is still refused after it.
SEEN_FILE = "_control_seen.json"


def message_bytes(payload: Any) -> bytes:
    """A received payload as the bytes it was sent as."""
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    return str(payload).encode("utf-8")


class ControlGuard:
    """Decides which control messages this node acts on.

    Without a key -- a node deployed before signing -- every message is accepted,
    as it always was, and the node says once that it is open. With one, a message
    has to be signed with this node's key and not be one already accepted. What
    happens to one that is not follows the mode it was deployed with: ``warn`` acts
    on it and counts it, ``enforce`` refuses it and counts it. The count travels in
    the heartbeat, so main can say so where someone will see it.
    """

    def __init__(self, key_hex: str, since: str, mode: str, state_dir: str) -> None:
        self._path = Path(state_dir) / SEEN_FILE
        self._key: bytes | None = None
        self._invalid = False
        key_hex = key_hex.strip()
        if key_hex:
            try:
                self._key = bytes.fromhex(key_hex)
            except ValueError:
                self._invalid = True
        try:
            #: The sequence number main had reached when this node was deployed. A
            #: message older than that was published for an earlier deployment.
            self._since = int(since.strip() or 0)
        except ValueError:
            self._since = 0
        self._enforce = mode.strip().lower() == "enforce"
        self._seen: dict[str, list[int]] | None = None
        self._said_open = False
        #: Messages that were not signed for this node, acted on or not.
        self.failures = 0

    @property
    def mode(self) -> str:
        """What the heartbeat reports: ``off``, ``warn``, ``enforce`` or ``invalid``."""
        if self._invalid:
            return "invalid"
        if self._key is None:
            return "off"
        return "enforce" if self._enforce else "warn"

    def admit(self, leaf: str, topic: str, payload: bytes, properties: dict[str, str]) -> bool:
        """Whether the control message ``payload`` on ``topic`` may be acted on."""
        if self._invalid:
            # Refused rather than treated as unkeyed: a node that was given a key
            # and cannot read it must not quietly go back to trusting everyone.
            self.failures += 1
            logger.error(
                "[runner] Refused %s: WACTORZ_NODE_KEY is not a valid key. Deploy this node again.",
                topic,
            )
            return False
        if self._key is None:
            if not self._said_open:
                self._said_open = True
                logger.warning(
                    "[runner] This node holds no signing key, so it acts on unsigned control "
                    "messages from anything on the broker. Deploy it again to give it one."
                )
            return True
        problem = self._problem(self._key, leaf, topic, payload, properties)
        if problem is None:
            return True
        self.failures += 1
        if self._enforce:
            logger.warning("[runner] Refused %s: %s.", topic, problem)
            return False
        logger.warning(
            "[runner] Acting on %s although %s (WACTORZ_NODE_SIGNING=warn).", topic, problem
        )
        return True

    def _problem(
        self, key: bytes, leaf: str, topic: str, payload: bytes, properties: dict[str, str]
    ) -> str | None:
        """Why a message is not one to trust, or None when it is."""
        signature = properties.get(SIGNATURE_PROPERTY)
        raw_sequence = properties.get(SEQUENCE_PROPERTY, "")
        if signature is None or not (raw_sequence.isascii() and raw_sequence.isdigit()):
            return "it is not signed"
        sequence = int(raw_sequence)
        expected = hmac.new(key, signing_input(topic, sequence, payload), hashlib.sha256)
        if not hmac.compare_digest(
            signature.encode("utf-8", "replace"), expected.hexdigest().encode("ascii")
        ):
            return "its signature was not made with this node's key"
        if not self._fresh(leaf, sequence):
            return "it was accepted before, or predates this node's deployment"
        self._remember(leaf, sequence)
        return None

    def _fresh(self, leaf: str, sequence: int) -> bool:
        seen = self._seen_for(leaf)
        if leaf == "desired_state":
            # Retained, so the broker hands the latest one over again on every
            # reconnect, and applying it again is what brings agents back after a
            # reboot. Only an older one is refused, and the deployment's starting
            # point does not apply: a desired state published before this node
            # was redeployed is still the one it should be running.
            return sequence >= max(seen, default=0)
        floor = max(self._since, max(seen, default=0) - REORDER_WINDOW_US)
        if len(seen) >= SEEN_PER_TOPIC:
            # Anything older than the oldest one remembered could have been
            # forgotten, so it is refused rather than possibly accepted twice.
            floor = max(floor, seen[0])
        return sequence > floor and sequence not in seen

    def _remember(self, leaf: str, sequence: int) -> None:
        seen = [*self._seen_for(leaf), sequence]
        newest = max(seen)
        kept = sorted(s for s in seen if s > newest - REORDER_WINDOW_US)[-SEEN_PER_TOPIC:]
        if self._seen is not None:
            self._seen[leaf] = kept
        self._save()

    def _seen_for(self, leaf: str) -> list[int]:
        if self._seen is None:
            self._seen = self._load()
        return self._seen.setdefault(leaf, [])

    def _load(self) -> dict[str, list[int]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning(
                "[runner] %s is unreadable; accepted control messages are remembered from "
                "this node's deployment onward only",
                self._path,
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(leaf): sorted(s for s in values if type(s) is int)
            for leaf, values in raw.items()
            if isinstance(values, list)
        }

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_text(self._path, json.dumps(self._seen))
        except OSError:
            logger.warning(
                "[runner] Could not record accepted control messages; one could be accepted "
                "again after a restart",
                exc_info=True,
            )
