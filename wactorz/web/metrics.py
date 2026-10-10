"""`/metrics` on the dashboard's server.

The REST interface serves `/metrics` too, but it runs only when it is the
chosen interface. The dashboard's server runs however Wactorz is started — the
CLI, a library call, the Home Assistant add-on — so serving the same page here
is what makes the metrics reachable everywhere. Same registry contents, same
key check as the rest of this server: a scraper presents the API key as
`Authorization: Bearer`, as it does on the REST port.

The monitor is built here rather than shared with the REST interface's, since
that one may not exist. Both register the same module-level metrics, so the
two pages agree.
"""

from typing import Any

from aiohttp import web

from ..agents.llm.cost import get_global_cost_info
from ..agents.lookup import find_main_actor
from ..config import CONFIG
from ..monitoring.prometheus import PrometheusMonitor
from . import runtime


def known_nodes() -> list[dict[str, Any]]:
    """The nodes main knows; none before main is up, or without a node manager."""
    nodes = getattr(find_main_actor(runtime.registry), "nodes", None)
    return nodes.list_nodes() if nodes is not None else []


def build_monitor() -> PrometheusMonitor:
    """A monitor that reads this process's actors, broker and nodes when rendered.

    Everything is read through `runtime` at render time, not captured now: the
    server is built before the actor system has finished wiring itself in.
    """
    return PrometheusMonitor(
        lambda: runtime.registry,
        publisher_provider=lambda: getattr(runtime.system, "_mqtt_client", None),
        nodes_provider=known_nodes,
        expected_nodes_provider=lambda: [target.name for target in CONFIG.deploy_targets],
        supervisor_provider=lambda: getattr(runtime.system, "supervisor", None),
        spend_provider=get_global_cost_info,
    )


def handler_for(monitor: PrometheusMonitor) -> Any:
    """The `/metrics` route's handler, rendering ``monitor``."""

    async def metrics_handler(_request: web.Request) -> web.Response:
        await monitor.refresh()
        return monitor.metrics_response()

    return metrics_handler
