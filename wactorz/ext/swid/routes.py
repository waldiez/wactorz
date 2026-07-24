"""aiohttp resolver routes: DIF DID Resolution over the file-backed registry."""

from __future__ import annotations

import logging
import os

import swid as swid_lib
from aiohttp import web

from .. import contract
from .registry import FileSWIDRegistry
from .service import SwidMinter

logger = logging.getLogger(__name__)


def swid_routes(registry: FileSWIDRegistry, minter: SwidMinter | None = None) -> web.RouteTableDef:
    """Build the did:swid routes: DIF resolution, plus the identity index.

    ``GET /1.0/identifiers/{did}`` resolves over ``registry``. When ``minter``
    is given, ``GET /api/swid/identities`` lists every minted identity
    (handle → did, with the entity class parsed from the handle) for the
    dashboard's Identity view.
    """
    routes = web.RouteTableDef()

    @routes.get("/1.0/identifiers/{did}")
    async def resolve_handler(request: web.Request) -> web.Response:
        did = request.match_info["did"]
        # resolve_async never raises for bad input — errors come back in the
        # resolution metadata (registry KeyError is mapped to notFound inside).
        result = await swid_lib.resolve_async(did, registry)
        error = result.did_resolution_metadata.get("error")
        return web.json_response(
            {
                "didDocument": result.did_document,
                "didResolutionMetadata": result.did_resolution_metadata,
                "didDocumentMetadata": result.did_document_metadata,
            },
            status=_resolution_status(error),
        )

    if minter is not None:

        @routes.get("/api/swid/identities")
        async def identities_handler(request: web.Request) -> web.Response:
            # Reconcile before listing: mint a DID for any live actor not yet
            # minted, so agents spawned *after* startup show up on refresh
            # (the boot-time mint hook only covers actors present at startup).
            # Idempotent — already-minted actors skip via the id-map / index.
            app_registry = request.app.get(contract.ACTOR_REGISTRY)
            id_map = request.app.get(contract.AGENT_IDENTITY)
            if app_registry is not None and id_map is not None:
                namespace = os.getenv("HA_NAMESPACE", "home")
                for actor in app_registry.all_actors():
                    if actor.actor_id in id_map:
                        continue
                    try:
                        id_map[actor.actor_id] = await minter.ensure_did(
                            "agent", namespace, actor.actor_id, name=actor.name
                        )
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        logger.warning(
                            "[swid] minting DID for agent '%s' failed: %s",
                            actor.actor_id,
                            exc,
                        )
            index = await minter.identities()
            identities = [
                {
                    "handle": handle,
                    "did": did,
                    # Handle shape: swid:<class>:<ns>:<local> — class is segment 1.
                    "entityClass": handle.split(":")[1] if handle.count(":") >= 3 else "",
                }
                for handle, did in sorted(index.items())
            ]
            return web.json_response({"enabled": minter.enabled, "identities": identities})

    return routes


def _resolution_status(error: str | None) -> int:
    """Map ``didResolutionMetadata.error`` to HTTP per the DIF binding."""
    if error is None:
        return 200
    if error == "invalidDid":
        return 400
    if error == "notFound":
        return 404
    return 500
