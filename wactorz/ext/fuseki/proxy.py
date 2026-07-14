"""SPARQL proxy — forwards /api/fuseki/{dataset}/{sparql|update} to Fuseki."""

from __future__ import annotations

import base64
import os

from aiohttp import ClientSession, web


async def fuseki_proxy_handler(request: web.Request) -> web.Response:
    """Start fuseki proxy handler."""
    fuseki_url = os.getenv("FUSEKI_URL", "")
    base = fuseki_url.strip().rstrip("/")
    if not base:
        return web.json_response({"error": "Fuseki is not configured"}, status=503)

    body = await request.read()

    forward_headers: dict[str, str] = {}
    for h in ("Content-Type", "Accept", "Authorization"):
        if h in request.headers:
            forward_headers[h] = request.headers[h]

    fuseki_user = os.getenv("FUSEKI_USER", "admin")
    if "Authorization" not in forward_headers and fuseki_user:
        fuseki_password = os.getenv("FUSEKI_PASSWORD", "admin")
        creds = base64.b64encode(f"{fuseki_user}:{fuseki_password}".encode()).decode()
        forward_headers["Authorization"] = f"Basic {creds}"
    target = _build_url(request, base)
    try:
        async with ClientSession() as session:
            async with session.post(target, data=body, headers=forward_headers) as resp:
                resp_body = await resp.read()
                resp_headers: dict[str, str] = {}
                if "Content-Type" in resp.headers:
                    resp_headers["Content-Type"] = resp.headers["Content-Type"]
                return web.Response(status=resp.status, body=resp_body, headers=resp_headers)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return web.json_response({"error": f"Fuseki proxy failed: {exc}"}, status=502)


def _build_url(request: web.Request, base: str) -> str:
    dataset = request.match_info.get("dataset", "")
    operation = request.path.rsplit("/", 1)[-1]  # "sparql" or "update"
    return f"{base}/{dataset.lstrip('/')}/{operation}"
