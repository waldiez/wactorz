"""apache fuseki extension."""

from aiohttp import web

from .bridge import TTL_PREFIXES, HAFusekiBridge, HAWebSocketClient, area_body, device_body
from .manager import BridgeManager
from .proxy import fuseki_proxy_handler


def setup(app: web.Application) -> None:
    """Register the extension."""
    mgr = BridgeManager()

    app.router.add_post("/api/fuseki/{dataset}/sparql", fuseki_proxy_handler)
    app.router.add_post("/api/fuseki/{dataset}/update", fuseki_proxy_handler)
    app.router.add_post("/api/ha/sync", mgr.ha_sync_handler)
    app.on_startup.append(mgr.start_ha_bridge)
    app.on_startup.append(mgr.start_agent_bridges)
    app.on_shutdown.append(mgr.cleanup)


def public_config(_app: web.Application) -> dict:
    """Non-secret Fuseki config for the browser."""
    import os

    return {
        "url": os.getenv("FUSEKI_URL", ""),
        "dataset": os.getenv("FUSEKI_DATASET", "wactorz"),
    }


__all__ = [
    "TTL_PREFIXES",
    "HAFusekiBridge",
    "HAWebSocketClient",
    "area_body",
    "device_body",
]
