"""apache fuseki extension."""

import logging
import os

from aiohttp import web

from .. import contract
from .bridge import (
    TTL_PREFIXES,
    HAFusekiBridge,
    HAWebSocketClient,
    area_body,
    device_body,
    link_agent_dids,
)
from .manager import BridgeManager
from .proxy import fuseki_proxy_handler

# link_agent_ids (id -> swid)
# needs the swid extension to be setup first
__deps__ = ["swid"]

logger = logging.getLogger(__name__)


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
    return {
        "url": os.getenv("FUSEKI_URL", ""),
        "dataset": os.getenv("FUSEKI_DATASET", "wactorz"),
    }


async def on_ready(app: web.Application) -> None:
    """Link each agent's minted DID/handle onto its urn:wactorz:agents node.

    Runs after swid's setup() has populated AGENT_IDENTITY and our own
    start_agent_bridges have seeded agent nodes.  Best-effort — a missing
    Fuseki or non minted DIDs are silently skipped.
    """
    fuseki_url = os.getenv("FUSEKI_URL", "")
    if not fuseki_url:
        return

    id_map = app.get(contract.AGENT_IDENTITY) or {}
    links = {
        actor_id: (rec.did, rec.handle) for actor_id, rec in id_map.items() if rec.did is not None
    }
    if not links:
        return

    try:
        await link_agent_dids(
            links,
            fuseki_url,
            os.getenv("FUSEKI_DATASET", "wactorz"),
            os.getenv("FUSEKI_USER", "admin"),
            os.getenv("FUSEKI_PASSWORD", "admin"),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[fuseki] agent DID → graph linkage failed: %s", exc)


__all__ = [
    "TTL_PREFIXES",
    "HAFusekiBridge",
    "HAWebSocketClient",
    "area_body",
    "device_body",
]
