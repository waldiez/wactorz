# SPDX-License-Identifier: Apache-2.0
"""The dashboard tabs extensions add: the list the page reads, and each one's script.

The script is served under ``/api/``, which the service worker never caches: it
keeps any other ``.js`` it has seen, on the assumption that a name carries a
content hash, and a panel's name does not change when its package is upgraded.
"""

from typing import Any

from aiohttp import web
from aiohttp.web import Response, StreamResponse

from ..ext import PANEL_ICONS, PANELS
from ..ext._panel import DEFAULT_ICON, setting_entry


def listed(app: web.Application) -> list[dict[str, Any]]:
    """What the page needs to show each panel's tab.

    A panel whose icon is a file is sent the file's text as ``icon_svg``, read
    when the extension loaded, with ``icon`` naming the dashboard's default for
    a page that does not read it. One that declares settings is sent them as
    ``settings``, for the page to draw and keep the values of.
    """
    icons = app.get(PANEL_ICONS, {})
    entries: list[dict[str, Any]] = []
    for name, panel in app.get(PANELS, {}).items():
        icon = panel.icon if isinstance(panel.icon, str) else DEFAULT_ICON
        entry: dict[str, Any] = {"name": name, "title": panel.title, "icon": icon}
        if name in icons:
            entry["icon_svg"] = icons[name]
        if panel.settings:
            entry["settings"] = [setting_entry(setting) for setting in panel.settings]
        entries.append(entry)
    return entries


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
