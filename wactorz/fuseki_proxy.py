"""SPARQL proxy — forwards /api/fuseki/{dataset}/{sparql|update} to Fuseki."""

from __future__ import annotations

import base64


async def fuseki_proxy_handler(request):
    import aiohttp
    from aiohttp import web
    from .config import CONFIG

    dataset = request.match_info["dataset"]
    operation = request.path.rsplit("/", 1)[-1]  # "sparql" or "update"

    base = (CONFIG.fuseki_url or "").strip().rstrip("/")
    if not base:
        return web.json_response({"error": "Fuseki is not configured"}, status=503)

    target = f"{base}/{dataset.lstrip('/')}/{operation}"
    body = await request.read()

    forward_headers: dict[str, str] = {}
    for h in ("Content-Type", "Accept", "Authorization"):
        if h in request.headers:
            forward_headers[h] = request.headers[h]

    if "Authorization" not in forward_headers and CONFIG.fuseki_user:
        creds = base64.b64encode(
            f"{CONFIG.fuseki_user}:{CONFIG.fuseki_password}".encode()
        ).decode()
        forward_headers["Authorization"] = f"Basic {creds}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(target, data=body, headers=forward_headers) as resp:
                resp_body = await resp.read()
                resp_headers: dict[str, str] = {}
                if "Content-Type" in resp.headers:
                    resp_headers["Content-Type"] = resp.headers["Content-Type"]
                return web.Response(status=resp.status, body=resp_body, headers=resp_headers)
    except Exception as exc:
        return web.json_response({"error": f"Fuseki proxy failed: {exc}"}, status=502)
