"""Turning up the detail on one logger, at runtime.

Diagnosing something usually means wanting more from *one* library — the MQTT
client, the LLM SDK — for a few minutes, on a machine you may only reach through
a browser. Restarting with a different level loses the state that produced the
problem, which is often the only copy of it.

Three rules shape this, and each exists because the obvious version is worse:

* **Never global.** Raising the root logger raises every library at once, and
  at DEBUG that is a firehose no ring buffer survives — the records you wanted
  are evicted by the ones you did not. A named logger only.
* **DEBUG needs the host's permission.** It is the level that prints request
  bodies, headers and payloads, so a library at DEBUG can write credentials
  into a buffer the dashboard serves. Raising to INFO or WARNING needs no flag;
  DEBUG needs ``WACTORZ_LOG_DEBUG_CAPTURE=1`` set on the process, which nobody
  can do through the API they are already talking to.
* **Authenticated like everything else.** It is a write: it changes what the
  process records about itself. It sits under ``/api/`` so the key check covers
  it without a second rule here.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from ..config import _env_truthy
from .api_logs import LEVELS

logger = logging.getLogger(__name__)

#: Lets the host allow DEBUG capture. Deliberately an environment variable and
#: not an option in the API: the point is that turning on the level that prints
#: payloads requires reaching the machine, not just reaching the endpoint.
DEBUG_CAPTURE_ENV = "WACTORZ_LOG_DEBUG_CAPTURE"

#: Names that mean "everything". Refused, whatever they are spelled as — the
#: empty string is what `logging.getLogger("")` treats as root.
_ROOT_NAMES = frozenset({"", "root", "."})

#: Restores a logger to inheriting from its parent, which is how it started.
NOTSET = "NOTSET"


def _debug_allowed() -> bool:
    return _env_truthy(DEBUG_CAPTURE_ENV)


async def capture_handler(request: web.Request) -> Response:
    """POST /api/logs/capture — set one logger's level.

    Body: ``{"logger": "aiomqtt", "level": "DEBUG"}``. ``NOTSET`` restores the
    logger to inheriting, which is the only way back without a restart.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"error": "Body must be JSON"}, status=400)

    name = str(body.get("logger", "")).strip()
    level = str(body.get("level", "")).strip().upper()

    if name.lower() in _ROOT_NAMES:
        return web.json_response(
            {
                "error": (
                    "Name a single logger. Raising the root logger raises every "
                    "library at once, and the records you want are then evicted by "
                    "the ones you do not."
                )
            },
            status=400,
        )
    if level != NOTSET and level not in LEVELS:
        return web.json_response(
            {"error": f"level must be one of {', '.join((*LEVELS, NOTSET))}"}, status=400
        )
    if level == "DEBUG" and not _debug_allowed():
        return web.json_response(
            {
                "error": (
                    "DEBUG capture is off. It prints request bodies and headers, so a "
                    f"library at DEBUG can write credentials into the log this server "
                    f"serves — set {DEBUG_CAPTURE_ENV}=1 on the host to allow it."
                )
            },
            status=403,
        )

    logging.getLogger(name).setLevel(logging.NOTSET if level == NOTSET else level)
    # Logged so the record of who turned what up lives in the same buffer the
    # captured records land in.
    logger.warning("[logs] capture level for %r set to %s", name, level)
    return web.json_response({"logger": name, "level": level})


async def capture_state_handler(_request: web.Request) -> Response:
    """GET /api/logs/capture — which loggers have been given an explicit level.

    Only loggers that exist and were set explicitly: `logging` creates a
    placeholder for every name ever referenced, and listing those would bury the
    handful somebody actually changed.
    """
    manager: Any = logging.Logger.manager
    overrides = {
        name: logging.getLevelName(item.level)
        for name, item in sorted(manager.loggerDict.items())
        if isinstance(item, logging.Logger) and item.level != logging.NOTSET
    }
    return web.json_response({"loggers": overrides, "debug_allowed": _debug_allowed()})
