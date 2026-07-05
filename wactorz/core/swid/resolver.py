"""SWID resolver: W3C DID Resolution over the registry (async).

Provides a framework-agnostic :func:`resolve` plus an aiohttp handler factory
(:func:`aiohttp_routes`) that plugs into the existing monitor server:

    GET /1.0/identifiers/<swid>          -> DID Resolution result
    GET /1.0/identifiers/<swid>/profile  -> the DID document (HSML profile)
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from .identifier import is_valid_swid
from .registry import Registry

# A per-request registry source: called once per request, yielding a Registry as
# an async context manager. This lets callers with a short-lived backend (the
# monitor's per-request Fuseki session) and callers with a long-lived one (tests,
# via ``lambda: nullcontext(registry)``) share one set of routes.
RegistryProvider = Callable[[], AbstractAsyncContextManager[Registry]]

DID_RESOLUTION_CONTEXT = "https://w3id.org/did-resolution/v1"
DID_LD_JSON = "application/did+ld+json"


async def resolve(swid: str, registry: Registry) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Resolve ``swid`` to a W3C DID Resolution result dict.

    Never raises for a bad/absent SWID: returns the standard error shapes
    (``invalidDid`` / ``notFound``) in ``didResolutionMetadata``.
    """
    base: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "@context": DID_RESOLUTION_CONTEXT,
        "didDocument": None,
        "didDocumentMetadata": {},
    }
    if not is_valid_swid(swid):
        return {**base, "didResolutionMetadata": {"error": "invalidDid"}}

    record = await registry.get(swid)
    if record is None:
        return {**base, "didResolutionMetadata": {"error": "notFound"}}

    return {
        "@context": DID_RESOLUTION_CONTEXT,
        "didDocument": record.document,
        "didResolutionMetadata": {"contentType": DID_LD_JSON},
        "didDocumentMetadata": {
            "created": record.document.get("created"),
            **record.metadata,
        },
    }


_STATUS_FOR_ERROR = {None: 200, "invalidDid": 400, "notFound": 404}


def status_for(result: dict[str, Any]) -> int:  # pyright: ignore[reportExplicitAny]
    """HTTP status for a DID Resolution ``result``.

    200 on success, 400 ``invalidDid``, 404 ``notFound`` (400 for anything else).
    Single source of truth for the status mapping so the standalone routes and
    the monitor server's handler cannot drift apart.
    """
    error = result.get("didResolutionMetadata", {}).get("error")  # pyright: ignore[reportAny]
    return _STATUS_FOR_ERROR.get(error, 400)  # pyright: ignore[reportAny]


def aiohttp_routes(provider: RegistryProvider):
    """Return an ``aiohttp.web.RouteTableDef`` for the resolver endpoints.

    ``provider`` yields a :class:`Registry` per request (see
    :data:`RegistryProvider`). Both endpoints are defined here once; callers
    (including the monitor server) mount these routes instead of hand-rolling
    their own handler::

        # long-lived registry (tests, single process):
        from contextlib import nullcontext
        app.add_routes(aiohttp_routes(lambda: nullcontext(registry)))

        # short-lived registry (monitor: a Fuseki session per request):
        app.add_routes(aiohttp_routes(open_registry))
    """
    from aiohttp import web

    routes = web.RouteTableDef()

    @routes.get("/1.0/identifiers/{swid}")
    async def _resolve(request: web.Request) -> web.Response:
        async with provider() as registry:
            result = await resolve(request.match_info["swid"], registry)
        return web.json_response(result, status=status_for(result), content_type=DID_LD_JSON)

    @routes.get("/1.0/identifiers/{swid}/profile")
    async def _profile(request: web.Request) -> web.Response:
        async with provider() as registry:
            record = await registry.get(request.match_info["swid"])
        if record is None:
            return web.json_response({"error": "notFound"}, status=404)
        return web.json_response(record.document, content_type=DID_LD_JSON)

    return routes
