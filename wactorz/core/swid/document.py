"""Build a minimal, W3C-DID-conformant SWID Document (HSML-only profile).

The full SWF SWID method requires an HSTP service endpoint; the SYNAPSE pilot
runs HSML-only, so the document's required service endpoint points at the SWID
resolver / HSML profile instead. The document is deliberately small -- identity,
controller, a service pointer, and creation metadata. Everything else about the
entity (capabilities, location, state) lives in the HSML graph, not here.
"""

from datetime import datetime, timezone
from typing import Any

DID_CONTEXT = "https://www.w3.org/ns/did/v1"
# Placeholder until the SWF/IEEE-2874 HSML context URL is pinned.
HSML_CONTEXT = "https://spatialwebfoundation.org/ns/hsml/v1"


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (second precision)."""
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_did_document(
    swid: str,
    *,
    resolver_base: str = "",
    controller: str | None = None,
    verification_method: list[dict[str, Any]] | None = None,  # pyright: ignore[reportExplicitAny]
    hsml_profile_url: str | None = None,
    also_known_as: list[str] | None = None,
    created: str | None = None,
) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Return a SWID Document (DID document) as a JSON-LD-ready ``dict``.

    Args:
        swid: the entity SWID (``swid:...``), used as ``id``.
        resolver_base: base URL of the resolver; used to synthesise the HSML
            profile service endpoint when ``hsml_profile_url`` is omitted.
        controller: controlling SWID (defaults to self-controlled).
        verification_method: optional W3C verification methods (add a per-entity
            key here when signed HSML Activities are wanted).
        hsml_profile_url: explicit HSML profile URL; overrides ``resolver_base``.
        also_known_as: e.g. the entity's ``ha:<entity_id>`` graph IRI, so the
            DID document links back to the triple-store node.
        created: ISO-8601 UTC timestamp; defaults to now (injectable for tests).
    """
    profile_url = hsml_profile_url or (
        f"{resolver_base.rstrip('/')}/1.0/identifiers/{swid}/profile"
        if resolver_base
        else f"swid:profile:{swid}"
    )
    doc: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "@context": [DID_CONTEXT, HSML_CONTEXT],
        "id": swid,
        "controller": controller or swid,
        "service": [
            {
                "id": f"{swid}#hsml-profile",
                "type": "HSMLProfile",
                "serviceEndpoint": profile_url,
            }
        ],
        "created": created or utc_now_iso(),
    }
    if also_known_as:
        doc["alsoKnownAs"] = also_known_as
    if verification_method:
        doc["verificationMethod"] = verification_method
        doc["authentication"] = [vm["id"] for vm in verification_method]
        doc["assertionMethod"] = [vm["id"] for vm in verification_method]
    return doc
