"""aiohttp resolver routes: DIF DID Resolution over the file-backed registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

import swid as swid_lib

from .registry import FileSWIDRegistry

if TYPE_CHECKING:
    from aiohttp import web as _web


def _resolution_status(error: str | None) -> int:
    """Map ``didResolutionMetadata.error`` to HTTP per the DIF binding."""
    if error is None:
        return 200
    if error == "invalidDid":
        return 400
    if error == "notFound":
        return 404
    return 500


def swid_routes(registry: FileSWIDRegistry) -> _web.RouteTableDef:
    """Build the ``GET /1.0/identifiers/{did}`` route over ``registry``."""
    # Call-time import: parts of the test suite install a partial aiohttp stub
    # in sys.modules; binding `web` at module import would freeze that stub in.
    from aiohttp import web

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

    return routes
