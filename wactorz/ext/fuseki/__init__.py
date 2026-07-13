"""apache fuseki extension."""

import asyncio
import logging
from typing import cast

from aiohttp import web

from wactorz.core import contract
from wactorz.core.registry import ActorRegistry

from .bridge import TTL_PREFIXES, HAFusekiBridge, HAWebSocketClient, area_body, device_body
from .proxy import fuseki_proxy_handler

_ha_bridge_task: asyncio.Task | None = None
_agent_bridge_tasks: list[asyncio.Task] = []


logger = logging.getLogger(__name__)


async def _start_ha_bridge(app=None) -> None:
    """Launch HAFusekiBridge as a background task if HA_TOKEN is configured.

    The did:swid minter (if the swid extension is loaded) is read from app-state
    so device/space graph nodes get linked; absent ⇒ handles-only, no triples.
    """
    # pylint: disable=global-statement,import-outside-toplevel
    global _ha_bridge_task
    from wactorz.config import CONFIG

    swid_minter = app.get(contract.IDENTITY_MINTER) if app is not None else None

    if not CONFIG.ha_token or not CONFIG.fuseki_url:
        return
    try:
        from .bridge import _run_with_retry, fuseki_reachable
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[ha-bridge] Could not import HAFusekiBridge: %s", exc)
        return

    # Don't start the bridge if Fuseki isn't actually running — otherwise it
    # connects to HA and then fails to write every state change, flooding the
    # log. If you're not using Fuseki, the bridge simply stays off.
    if not await fuseki_reachable(CONFIG.fuseki_url):
        logger.info(
            "[ha-bridge] Fuseki not reachable at %s — HA→Fuseki bridge "
            "disabled. (Start Fuseki and POST /api/ha/sync to enable, "
            "or ignore if you don't use Fuseki.)",
            CONFIG.fuseki_url,
        )
        return

    bridge = HAFusekiBridge(
        ha_url=CONFIG.ha_url,
        ha_token=CONFIG.ha_token,
        fuseki_url=CONFIG.fuseki_url,
        fuseki_dataset=CONFIG.fuseki_dataset,
        fuseki_user=CONFIG.fuseki_user,
        fuseki_password=CONFIG.fuseki_password,
        # Mint a did:swid per space/device during seed and link it on the graph
        # node (no-op when minting is disabled — handles only, no triples).
        swid_minter=swid_minter,
        swid_namespace=CONFIG.swid_namespace,
    )
    _ha_bridge_task = asyncio.create_task(
        _run_with_retry(bridge.run, "HAFusekiBridge"),
        name="ha-fuseki-bridge",
    )
    logger.info(
        "[ha-bridge] HAFusekiBridge started (ha=%s → fuseki=%s/%s)",
        CONFIG.ha_url,
        CONFIG.fuseki_url,
        CONFIG.fuseki_dataset,
    )


# pylint: disable=import-outside-toplevel
async def _start_agent_bridges(app: web.Application) -> None:
    """Launch the agent-manifest and metrics Fuseki bridges in-process.

    Agents publish their capability manifests as *retained* MQTT messages on
    ``agents/{id}/manifest``, but something has to consume them and upsert them
    into the ``urn:wactorz:agents`` named graph that the dashboard's "Agents"
    panel queries. In the standalone ``wactorz-fuseki`` process that is the job
    of AgentManifestBridge/MetricsBridge — but the single-process app (``wactorz``,
    the HA add-on, ``make run``) only ran HAFusekiBridge, so ``urn:ha:devices`` /
    ``urn:ha:areas`` were rebuilt on startup and showed up while the agents graph
    stayed empty. Starting the bridges here consumes the retained manifests
    without needing a separate bridge container.

    Also seeds the registry once so agents that never publish a manifest (main,
    planner, monitor, io) still appear as typed, labelled nodes.
    """
    global _agent_bridge_tasks  # pylint: disable=global-statement
    from wactorz.config import CONFIG

    if not CONFIG.fuseki_url:
        return
    try:
        from .bridge import (
            AgentManifestBridge,
            MetricsBridge,
            _run_with_retry,
            fuseki_reachable,
            seed_agent_registry,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[agent-bridge] Could not import Fuseki agent bridges: %s", exc)
        return

    # Skip if Fuseki isn't up — otherwise the bridges retry-loop forever writing
    # nowhere. The agents graph simply stays empty until Fuseki is reachable.
    if not await fuseki_reachable(CONFIG.fuseki_url):
        logger.info(
            "[agent-bridge] Fuseki not reachable at %s — agent manifest bridge "
            "disabled. (Start Fuseki to populate the agents graph.)",
            CONFIG.fuseki_url,
        )
        return

    # Seed registry-owned fields (state/protected) plus a fallback node for every
    # running actor, so agents without a manifest still show. Best-effort.
    app_registry = app.get(contract.ACTOR_REGISTRY)
    if app_registry is not None:
        registry = cast(ActorRegistry, app_registry)
        try:
            await seed_agent_registry(
                registry.all_actors(),
                CONFIG.fuseki_url,
                CONFIG.fuseki_dataset,
                CONFIG.fuseki_user,
                CONFIG.fuseki_password,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[agent-bridge] Agent registry seed failed: %s", exc)

    manifest_bridge = AgentManifestBridge(
        mqtt_broker=CONFIG.mqtt_host,
        mqtt_port=CONFIG.mqtt_port,
        fuseki_url=CONFIG.fuseki_url,
        fuseki_dataset=CONFIG.fuseki_dataset,
        fuseki_user=CONFIG.fuseki_user,
        fuseki_password=CONFIG.fuseki_password,
    )
    metrics_bridge = MetricsBridge(
        mqtt_broker=CONFIG.mqtt_host,
        mqtt_port=CONFIG.mqtt_port,
        fuseki_url=CONFIG.fuseki_url,
        fuseki_dataset=CONFIG.fuseki_dataset,
        fuseki_user=CONFIG.fuseki_user,
        fuseki_password=CONFIG.fuseki_password,
    )
    _agent_bridge_tasks = [
        asyncio.create_task(
            _run_with_retry(manifest_bridge.run, "AgentManifestBridge"),
            name="agent-manifest-bridge",
        ),
        asyncio.create_task(
            _run_with_retry(metrics_bridge.run, "MetricsBridge"),
            name="agent-metrics-bridge",
        ),
    ]
    logger.info(
        "[agent-bridge] AgentManifestBridge + MetricsBridge started (mqtt=%s:%d → fuseki=%s/%s)",
        CONFIG.mqtt_host,
        CONFIG.mqtt_port,
        CONFIG.fuseki_url,
        CONFIG.fuseki_dataset,
    )


async def ha_sync_handler(request: web.Request) -> web.Response:
    """POST /api/ha/sync — cancel and restart the HA→Fuseki bridge immediately."""
    from wactorz.config import CONFIG  # pylint: disable=import-outside-toplevel

    if not CONFIG.ha_token:
        return web.json_response({"error": "HA_TOKEN not configured"}, status=400)
    if _ha_bridge_task and not _ha_bridge_task.done():
        _ha_bridge_task.cancel()
        try:
            await _ha_bridge_task
        except (asyncio.CancelledError, Exception):  # pylint: disable=broad-exception-caught
            pass
    await _start_ha_bridge(request.app)
    return web.json_response({"status": "restarted"})


async def _cleanup_tasks(_app: web.Application) -> None:
    for task in _agent_bridge_tasks:
        task.cancel()
    global _ha_bridge_task  # pylint: disable=global-statement
    if _ha_bridge_task:
        _ha_bridge_task.cancel()
        _ha_bridge_task = None


def setup(app: web.Application) -> None:
    """Register the extension."""
    app.router.add_post("/api/fuseki/{dataset}/sparql", fuseki_proxy_handler)
    app.router.add_post("/api/fuseki/{dataset}/update", fuseki_proxy_handler)
    app.router.add_post("/api/ha/sync", ha_sync_handler)
    app.on_startup.append(_start_ha_bridge)
    app.on_startup.append(_start_agent_bridges)
    # app.on_cleanup.append()  # or?
    app.on_shutdown.append(_cleanup_tasks)


__all__ = [
    "TTL_PREFIXES",
    "HAFusekiBridge",
    "HAWebSocketClient",
    "area_body",
    "device_body",
]
