"""Readable entity handles: ``swid:<class>:<namespace>:<slug>-<fingerprint>``.

A handle is a deterministic, human-readable **label** for a long-lived entity
(e.g. ``swid:device:home:kitchen-light-ab12cd34``). It is NOT a DID: the
canonical identifier for entities that act or sign is a real ``did:swid:z…``
minted via the ``waldiez-swid`` library, which carries the handle in the DID
document's ``alsoKnownAs``. Handles are safe as MQTT topic and graph segments
(no ``/``, ``:`` only as the scheme delimiters, no wildcards).

Identical inputs always produce an identical handle, so re-onboarding the same
entity is idempotent without any registry round-trip.
"""

from __future__ import annotations

import base64
import hashlib
import re

KNOWN_CLASSES = frozenset({"space", "device", "agent", "user"})

DEFAULT_FINGERPRINT_LEN = 8
_MIN_FINGERPRINT_LEN = 6
_MAX_FINGERPRINT_LEN = 32

# A valid segment: lowercase alnum start, then alnum/dot/hyphen/underscore. No
# ':' (delimiter) and no '/' or MQTT wildcards, so a handle drops verbatim into
# a topic. Dots are allowed so names like ``name.v2`` validate.
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase and collapse anything outside ``[a-z0-9]`` to ``-`` (no dots).

    Returns ``""`` if nothing survives.
    """
    return _SLUG_STRIP_RE.sub("-", text.strip().lower()).strip("-")


def normalize_segment(text: str) -> str:
    """Segment normaliser that **keeps dots** (``name.v2`` stays ``name.v2``)."""
    segment = text.strip().lower()
    segment = re.sub(r"[\s_]+", "-", segment)
    segment = re.sub(r"[^a-z0-9\-.]", "", segment)
    return re.sub(r"-{2,}", "-", segment).strip("-")


def fingerprint(namespace: str, natural_key: str, length: int = DEFAULT_FINGERPRINT_LEN) -> str:
    """Deterministic base32-lower fingerprint of a stable natural key.

    Scoped by ``namespace`` so the same key in two domains cannot collide.
    ``natural_key`` should be the entity's most stable handle (MAC / serial /
    Home Assistant device id) — never a display name.
    """
    if not (_MIN_FINGERPRINT_LEN <= length <= _MAX_FINGERPRINT_LEN):
        msg = f"fingerprint length must be in [{_MIN_FINGERPRINT_LEN}, {_MAX_FINGERPRINT_LEN}]"
        raise ValueError(msg)
    material = f"{namespace}\x00{natural_key}".encode()
    digest = hashlib.blake2s(material).digest()
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:length]


def _require_segment(value: str, field: str) -> str:
    if not value or not _SEGMENT_RE.match(value):
        raise ValueError(f"{field} must match [a-z0-9][a-z0-9._-]* (got {value!r})")
    return value


def make_handle(
    entity_class: str,
    namespace: str,
    natural_key: str,
    *,
    name: str | None = None,
    fingerprint_len: int = DEFAULT_FINGERPRINT_LEN,
) -> str:
    """Deterministically build ``swid:<class>:<ns>:<slug>-<fp>`` for an entity.

    The fingerprint covers ``natural_key`` only (scoped by namespace), so the
    handle is invariant to display-name and location changes; ``name`` merely
    prettifies the local part.
    """
    if entity_class not in KNOWN_CLASSES:
        raise ValueError(
            f"entity_class must be one of {sorted(KNOWN_CLASSES)} (got {entity_class!r})"
        )
    ns = _require_segment(slugify(namespace), "namespace")
    if not natural_key or not natural_key.strip():
        raise ValueError("natural_key must be a non-empty string")

    fp = fingerprint(ns, natural_key, fingerprint_len)
    name_slug = slugify(name) if name else ""
    local_id = f"{name_slug}-{fp}" if name_slug else fp
    return f"swid:{entity_class}:{ns}:{_require_segment(local_id, 'local_id')}"
