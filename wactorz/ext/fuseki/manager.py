"""Bridge bootstrap helpers — bridge startup, sync handler, and cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import cast

from aiohttp import web

from wactorz.core.registry import ActorRegistry

from .. import contract
from .bridge import HAFusekiBridge

logger = logging.getLogger(__name__)


class BridgeManager:
    """Manages the HA => Fuseki and agent bridges lifecycle.

    A single instance is created in ``setup()`` and its bound methods are
    registered as aiohttp callbacks.
    """

    def __init__(self) -> None:
        self.ha_task: asyncio.Task | None = None
        self.agent_tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # HA => Fuseki bridge
    # ------------------------------------------------------------------

    async def start_ha_bridge(self, app: web.Application) -> None:
        """Launch HAFusekiBridge as a background task if HA_TOKEN is configured.

        The did:swid minter (if the swid extension is loaded) is read from app-state
        so device/space graph nodes get linked; absent ⇒ handles-only, no triples.
        """
        # pylint: disable=import-outside-toplevel
        from wactorz.config import CONFIG

        swid_minter = app.get(contract.IDENTITY_MINTER)
        fuseki_url = os.getenv("FUSEKI_URL", "")
        if not CONFIG.ha_token or not fuseki_url:
            return
        try:
            from .bridge import _run_with_retry, fuseki_reachable
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[ha-bridge] Could not import HAFusekiBridge: %s", exc)
            return

        # Don't start the bridge if Fuseki isn't actually running — otherwise it
        # connects to HA and then fails to write every state change, flooding the
        # log. If you're not using Fuseki, the bridge simply stays off.
        if not await fuseki_reachable(fuseki_url):
            logger.info(
                "[ha-bridge] Fuseki not reachable at %s — HA→Fuseki bridge "
                "disabled. (Start Fuseki and POST /api/ha/sync to enable, "
                "or ignore if you don't use Fuseki.)",
                fuseki_url,
            )
            return
        fuseki_dataset = os.getenv("FUSEKI_DATASET", "wactorz")
        fuseki_user = os.getenv("FUSEKI_USER", "admin")
        fuseki_password = os.getenv("FUSEKI_PASSWORD", "admin")
        bridge = HAFusekiBridge(
            ha_url=CONFIG.ha_url,
            ha_token=CONFIG.ha_token,
            fuseki_url=fuseki_url,
            fuseki_dataset=fuseki_dataset,
            fuseki_user=fuseki_user,
            fuseki_password=fuseki_password,
            # Mint a did:swid per space/device during seed and link it on the graph
            # node (no-op when minting is disabled — handles only, no triples).
            swid_minter=swid_minter,
            swid_namespace=CONFIG.ha_namespace,
        )
        self.ha_task = asyncio.create_task(
            _run_with_retry(bridge.run, "HAFusekiBridge"),
            name="ha-fuseki-bridge",
        )
        logger.info(
            "[ha-bridge] HAFusekiBridge started (ha=%s → fuseki=%s/%s)",
            CONFIG.ha_url,
            fuseki_url,
            fuseki_dataset,
        )

    # ------------------------------------------------------------------
    # Agent bridges
    # ------------------------------------------------------------------

    # pylint: disable=import-outside-toplevel,too-many-locals
    async def start_agent_bridges(self, app: web.Application) -> None:
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
        from wactorz.config import CONFIG

        fuseki_url = os.getenv("FUSEKI_URL", "")

        if not fuseki_url:
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
        if not await fuseki_reachable(fuseki_url):
            logger.info(
                "[agent-bridge] Fuseki not reachable at %s — agent manifest bridge "
                "disabled. (Start Fuseki to populate the agents graph.)",
                fuseki_url,
            )
            return
        fuseki_dataset = os.getenv("FUSEKI_DATASET", "wactorz")
        fuseki_user = os.getenv("FUSEKI_USER", "admin")
        fuseki_password = os.getenv("FUSEKI_PASSWORD", "admin")
        # Seed registry-owned fields (state/protected) plus a fallback node for every
        # running actor, so agents without a manifest still show. Best-effort.
        app_registry = app.get(contract.ACTOR_REGISTRY)
        if app_registry is not None:
            registry = cast(ActorRegistry, app_registry)
            try:
                await seed_agent_registry(
                    registry.all_actors(),
                    fuseki_url=fuseki_url,
                    fuseki_dataset=fuseki_dataset,
                    fuseki_user=fuseki_user,
                    fuseki_password=fuseki_password,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("[agent-bridge] Agent registry seed failed: %s", exc)

        manifest_bridge = AgentManifestBridge(
            mqtt_broker=CONFIG.mqtt_host,
            mqtt_port=CONFIG.mqtt_port,
            fuseki_url=fuseki_url,
            fuseki_dataset=fuseki_dataset,
            fuseki_user=fuseki_user,
            fuseki_password=fuseki_password,
        )
        metrics_bridge = MetricsBridge(
            mqtt_broker=CONFIG.mqtt_host,
            mqtt_port=CONFIG.mqtt_port,
            fuseki_url=fuseki_url,
            fuseki_dataset=fuseki_dataset,
            fuseki_user=fuseki_user,
            fuseki_password=fuseki_password,
        )
        self.agent_tasks = [
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
            "[agent-bridge] AgentManifestBridge + MetricsBridge started "
            "(mqtt=%s:%d → fuseki=%s/%s)",
            CONFIG.mqtt_host,
            CONFIG.mqtt_port,
            fuseki_url,
            fuseki_dataset,
        )

    # ------------------------------------------------------------------
    # Sync handler
    # ------------------------------------------------------------------

    async def ha_sync_handler(self, request: web.Request) -> web.Response:
        """POST /api/ha/sync — cancel and restart the HA→Fuseki bridge immediately."""
        from wactorz.config import CONFIG  # pylint: disable=import-outside-toplevel

        if not CONFIG.ha_token:
            return web.json_response({"error": "HA_TOKEN not configured"}, status=400)

        if self.ha_task and not self.ha_task.done():
            self.ha_task.cancel()
            try:
                await self.ha_task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-exception-caught
                pass
        await self.start_ha_bridge(request.app)
        return web.json_response({"status": "restarted"})

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self, _app: web.Application) -> None:
        """Cancel all bridge tasks on shutdown."""
        for task in self.agent_tasks:
            task.cancel()
        self.agent_tasks.clear()
        if self.ha_task:
            self.ha_task.cancel()
            self.ha_task = None
