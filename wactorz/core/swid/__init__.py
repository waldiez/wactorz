"""SWID (Spatial Web ID) subsystem: generation, registry, resolution.

A SWID names a **long-lived entity** (Space, Device, Agent, User) as
``swid:<class>:<namespace>:<local-id>``. The namespace is a stable
deployment/site domain -- a device that moves rooms keeps its SWID, and its
room is modelled as a graph relationship rather than encoded into the id.

This is the SYNAPSE pilot's pragmatic scheme, **not** the official SWF-STD-5
``did:swid`` DID method (that prefix stays reserved for a future conformant
implementation); see :mod:`.identifier` for the rationale and migration seam.

Submodules:

* :mod:`.identifier` -- pure generation / parsing / validation.
* :mod:`.document`   -- the DID (SWID) document builder (HSML-only profile).
* :mod:`.registry`   -- async storage + uniqueness guard (in-memory / Fuseki).
* :mod:`.service`    -- ``SwidService``, the idempotent onboarding facade.
* :mod:`.resolver`   -- W3C DID Resolution (``resolve`` + aiohttp routes).

Import everything public from this package root, e.g.::

    from wactorz.core.swid import generate_swid, SwidService, resolve
"""

from .document import (
    DID_CONTEXT,
    HSML_CONTEXT,
    build_did_document,
    utc_now_iso,
)
from .identifier import (
    DEFAULT_FINGERPRINT_LEN,
    KNOWN_CLASSES,
    LEGACY_PREFIX,
    PREFIX,
    InvalidSwidError,
    Swid,
    agent_swid,
    device_swid,
    fingerprint,
    generate_swid,
    is_valid_swid,
    legacy_home_swid,
    normalize_segment,
    parse_swid,
    slugify,
    space_swid,
    user_swid,
)
from .registry import (
    REGISTRY_GRAPH,
    SWID_NS,
    FusekiSwidRegistry,
    InMemoryRegistry,
    Record,
    Registry,
    SwidCollisionError,
)
from .resolver import (
    DID_LD_JSON,
    DID_RESOLUTION_CONTEXT,
    aiohttp_routes,
    resolve,
    status_for,
)
from .service import SwidService

__all__ = [
    # identifier
    "Swid",
    "InvalidSwidError",
    "PREFIX",
    "LEGACY_PREFIX",
    "KNOWN_CLASSES",
    "DEFAULT_FINGERPRINT_LEN",
    "slugify",
    "normalize_segment",
    "fingerprint",
    "generate_swid",
    "device_swid",
    "space_swid",
    "agent_swid",
    "user_swid",
    "legacy_home_swid",
    "parse_swid",
    "is_valid_swid",
    # document
    "build_did_document",
    "utc_now_iso",
    "DID_CONTEXT",
    "HSML_CONTEXT",
    # registry
    "Record",
    "Registry",
    "InMemoryRegistry",
    "FusekiSwidRegistry",
    "SwidCollisionError",
    "REGISTRY_GRAPH",
    "SWID_NS",
    # service
    "SwidService",
    # resolver
    "resolve",
    "status_for",
    "aiohttp_routes",
    "DID_LD_JSON",
    "DID_RESOLUTION_CONTEXT",
]
