"""SWID (Spatial Web ID) generation, parsing, and validation.

Consolidates the project's Spatial Web identifier logic in one pure,
dependency-free module. It was previously an ad-hoc helper embedded in the
Home Assistant integration; ``ha_helper.generate_swid`` now delegates here.

A SWID names a **long-lived entity** (Space, Device, Agent, User). Its shape is
three colon-separated, topic-safe segments after the :data:`PREFIX`::

    swid:<class>:<namespace>:<local-id>

**Not the official ``did:swid`` method.** SWF-STD-5 ``did:swid`` is a
self-certifying DID whose id is a base58 hash of a signed Cryptographic Event
Log, with a mandatory HSTP service endpoint and Multikey verification. This
module is the SYNAPSE pilot's *pragmatic, human-readable, registry-authoritative*
identifier -- deliberately a different scheme. Spec for the eventual real thing:
https://spatial-web-foundation.github.io/swf-std-5-did-swid-spec/documents/document.html 
So it uses its own ``swid:`` prefix and does **not** claim the ``did:swid:`` namespace, 
which stays reserved for a future conformant implementation. A real ``did:swid:z<scid>`` 
therefore does not parse here (single segment, not three) -- by design, so the two can
coexist. Migration seam: entities keep their stable ``natural_key`` and mint
deterministically, so real SCIDs can be minted alongside later without a churn.

Two generators exist on purpose:

* :func:`generate_swid` -- the general, disciplined API (class restricted to
  :data:`KNOWN_CLASSES`, base32 fingerprint, no dots). Use this for new work.
* :func:`legacy_home_swid` -- reproduces the **incumbent** Home Assistant string
  ``did:swid:home:<area>:<name>-<sha256[:6]>`` byte-for-byte, so already-emitted
  ids keep matching. It is grandfathered (hence still on the old prefix) and bakes
  the room into the id; the forward replacement is room-independent
  :func:`device_swid`.

:func:`parse_swid` / :func:`is_valid_swid` are intentionally **lenient**: they
validate structure only (three topic-safe segments) and accept both the ``swid:``
scheme and the grandfathered ``did:swid:`` incumbents (legacy ``home`` class,
dotted segments). The project previously had no way to validate a SWID at all.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any

# The pilot's own scheme (NOT the official ``did:swid`` DID method -- see module
# docstring). ``did:swid:`` is reserved for a future conformant implementation.
PREFIX = "swid:"
# Grandfathered prefix of already-emitted Home Assistant ids, still accepted by
# the parser and reproduced verbatim by :func:`legacy_home_swid`.
LEGACY_PREFIX = "did:swid:"

# Advisory vocabulary for the *new* generator. Not enforced by ``parse_swid``,
# which must keep accepting the legacy ``home`` class.
KNOWN_CLASSES = frozenset({"space", "device", "agent", "user"})

# A valid segment: lowercase alnum start, then alnum/dot/hyphen/underscore. No
# ':' (delimiter) and no '/' or MQTT wildcards, so a SWID drops verbatim into a
# topic. Dots are allowed so legacy names like ``name.v2`` validate.
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

DEFAULT_FINGERPRINT_LEN = 8
_MIN_FINGERPRINT_LEN = 6
_MAX_FINGERPRINT_LEN = 32


class InvalidSwidError(ValueError):
    """A string is not a syntactically valid SWID."""


@dataclass(frozen=True, slots=True)
class Swid:
    """A parsed SWID. ``str(swid)`` returns the canonical ``swid:...`` form.

    Note: a grandfathered ``did:swid:`` value parses (lenient), but ``str()``
    canonicalises it to the ``swid:`` prefix -- so treat legacy ids as opaque
    strings rather than reconstructing them via ``str(parse_swid(...))``.
    """

    entity_class: str
    namespace: str
    local_id: str

    def __str__(self) -> str:  # pyright: ignore[reportImplicitOverride]
        return f"{PREFIX}{self.entity_class}:{self.namespace}:{self.local_id}"


def slugify(text: str) -> str:
    """Lowercase and collapse anything outside ``[a-z0-9]`` to ``-`` (no dots).

    Used by the *new* generator. Returns ``""`` if nothing survives.
    """
    return _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")


def normalize_segment(text: str) -> str:
    """Legacy segment normaliser: like :func:`slugify` but **keeps dots**.

    Preserves the exact behaviour of the former
    ``ha_helper._normalize_swid_segment`` (``name.v2`` -> ``name.v2``).
    """
    segment = text.strip().lower()
    segment = re.sub(r"[\s_]+", "-", segment)
    segment = re.sub(r"[^a-z0-9\-.]", "", segment)
    segment = re.sub(r"-{2,}", "-", segment).strip("-")
    return segment


def fingerprint(
    namespace: str, natural_key: str, length: int = DEFAULT_FINGERPRINT_LEN
) -> str:
    """Deterministic base32-lower fingerprint of a stable natural key.

    Scoped by ``namespace`` so the same key in two domains cannot collide.
    ``natural_key`` should be the entity's most stable handle (MAC / serial /
    Home Assistant device id) -- never a display name.
    """
    if not (_MIN_FINGERPRINT_LEN <= length <= _MAX_FINGERPRINT_LEN):
        msg = (
            "fingerprint length must be in "
            f"[{_MIN_FINGERPRINT_LEN}, {_MAX_FINGERPRINT_LEN}]"
        )
        raise ValueError(msg)
    material = f"{namespace}\x00{natural_key}".encode()
    digest = hashlib.blake2s(material).digest()
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:length]


def _require_segment(value: str, field: str) -> str:
    if not value or not _SEGMENT_RE.match(value):
        raise InvalidSwidError(
            f"{field} must match [a-z0-9][a-z0-9._-]* (got {value!r})"
        )
    return value


def generate_swid(
    entity_class: str,
    namespace: str,
    natural_key: str,
    *,
    name: str | None = None,
    fingerprint_len: int = DEFAULT_FINGERPRINT_LEN,
) -> Swid:
    """Deterministically mint a SWID for a long-lived entity (new, disciplined).

    Identical inputs always return an identical SWID, so re-onboarding the same
    entity is idempotent without a registry round-trip.
    """
    if entity_class not in KNOWN_CLASSES:
        raise InvalidSwidError(
            f"entity_class must be one of {sorted(KNOWN_CLASSES)} (got {entity_class!r})"
        )
    ns = _require_segment(slugify(namespace), "namespace")
    if not natural_key or not natural_key.strip():
        raise InvalidSwidError("natural_key must be a non-empty string")

    fp = fingerprint(ns, natural_key, fingerprint_len)
    name_slug = slugify(name) if name else ""
    local_id = f"{name_slug}-{fp}" if name_slug else fp
    return Swid(entity_class, ns, _require_segment(local_id, "local_id"))


def device_swid(namespace: str, device_id: str, *, name: str | None = None) -> Swid:
    """Room-independent device SWID: ``swid:device:<ns>:<name>-<fp>``.

    The fingerprint is over ``device_id`` only, so the SWID is invariant to the
    device's room/area -- location is tracked as a graph relationship, not part
    of the identifier. This is the forward replacement for
    :func:`legacy_home_swid`, which encoded the room into the id.
    """
    return generate_swid("device", namespace, device_id, name=name)


def space_swid(namespace: str, area_id: str, *, name: str | None = None) -> Swid:
    """SWID for a Space (room/zone/area): ``swid:space:<ns>:<name>-<fp>``."""
    return generate_swid("space", namespace, area_id, name=name)


def agent_swid(namespace: str, actor_id: str, *, name: str | None = None) -> Swid:
    """SWID for a software Agent: ``swid:agent:<ns>:<name>-<fp>``."""
    return generate_swid("agent", namespace, actor_id, name=name)


def user_swid(namespace: str, user_key: str, *, name: str | None = None) -> Swid:
    """SWID for a human User: ``swid:user:<ns>:<name>-<fp>``."""
    return generate_swid("user", namespace, user_key, name=name)


def legacy_home_swid(
    device_id: str, *, name: str | None = None, area: str | None = None
) -> str:
    """Reproduce the incumbent Home Assistant SWID string byte-for-byte.

    Format: ``did:swid:home:<area>:<name>-<sha256(device_id)[:6]>`` with
    ``unassigned`` / ``device`` fallbacks. Kept on the grandfathered
    :data:`LEGACY_PREFIX` so ids already emitted keep matching; it is neither the
    pilot's ``swid:`` scheme nor an official Spatial Web DID, and it bakes the
    room into the id -- prefer :func:`device_swid` for new callers, which keeps
    location out of the id so a device survives being moved between rooms.
    """
    area_segment = normalize_segment(area) if area else ""
    if not area_segment:
        area_segment = "unassigned"

    name_segment = normalize_segment(name) if name else ""
    if not name_segment:
        name_segment = "device"

    stable_hash = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:6]
    return f"{LEGACY_PREFIX}home:{area_segment}:{name_segment}-{stable_hash}"


def parse_swid(value: Any) -> Swid:  # pyright: ignore[reportAny, reportExplicitAny]
    """Parse ``swid:<class>:<namespace>:<local-id>`` (lenient on class).

    Accepts the ``swid:`` scheme and grandfathered ``did:swid:``
    incumbents; structural validation only (three topic-safe segments), so the
    legacy ``home`` class and dotted segments pass. A real conformant
    ``did:swid:z<scid>`` is rejected (one segment, not three). Raises
    :class:`InvalidSwidError` otherwise.
    """
    if not isinstance(value, str):
        raise InvalidSwidError(f"SWID must be a string (got {type(value).__name__})")  # pyright: ignore[reportAny]
    # LEGACY_PREFIX first: it is more specific ("did:swid:" does not start with
    # "swid:", but check the longer one first to be explicit).
    for prefix in (LEGACY_PREFIX, PREFIX):
        if value.startswith(prefix):
            rest = value[len(prefix) :]
            break
    else:
        raise InvalidSwidError(
            f"SWID must start with {PREFIX!r} or {LEGACY_PREFIX!r} (got {value!r})"
        )
    parts = rest.split(":")
    if len(parts) != 3:
        raise InvalidSwidError(
            f"SWID must be {PREFIX}<class>:<namespace>:<local-id> (got {value!r})"
        )
    entity_class, namespace, local_id = parts
    _require_segment(entity_class, "class")  # pyright: ignore[reportUnusedCallResult]
    _require_segment(namespace, "namespace")  # pyright: ignore[reportUnusedCallResult]
    _require_segment(local_id, "local_id")  # pyright: ignore[reportUnusedCallResult]
    return Swid(entity_class, namespace, local_id)


def is_valid_swid(value: Any) -> bool:  # pyright: ignore[reportAny, reportExplicitAny]
    """Return ``True`` iff ``value`` is a syntactically valid SWID."""
    try:
        parse_swid(value)  # pyright: ignore[reportUnusedCallResult]
    except InvalidSwidError:
        return False
    return True
