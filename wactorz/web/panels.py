"""The dashboard tabs extensions add: the list the page reads, and each one's script.

The script is served under ``/api/``, which the service worker never caches: it
keeps any other ``.js`` it has seen, on the assumption that a name carries a
content hash, and a panel's name does not change when its package is upgraded.
"""

from aiohttp import web
from aiohttp.web import Response, StreamResponse

from ..ext import PANELS


def listed(app: web.Application) -> list[dict[str, str]]:
    """What the page needs to show each panel's tab."""
    return [
        {"name": name, "title": panel.title, "icon": panel.icon}
        for name, panel in app.get(PANELS, {}).items()
    ]


async def script_handler(request: web.Request) -> StreamResponse | Response:
    """Serve a panel's script, for a panel of an extension that loaded and nothing else."""
    panel = request.app.get(PANELS, {}).get(request.match_info["name"])
    if panel is None or not panel.script.is_file():
        return web.json_response({"error": "No such panel"}, status=404)
    return web.FileResponse(
        panel.script,
        headers={
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
