"""Wactorz Monitor — WebSocket dashboard + optional MQTT bridge.

Chat routing modes (set via registry wiring in cli.py):
  direct_ws  — registry is set; chat goes straight to actors over WebSocket.
               No IOAgent, no MQTT round-trip for user messages.
  mqtt       — registry is None; chat goes through IOAgent via MQTT (legacy).

The mode is advertised to the browser on connect via a {"type":"config"} frame
so the frontend knows whether to send chat over /ws or publish to io/chat.
"""

# pylint: disable=global-statement,invalid-name,logging-fstring-interpolation
# pylint: disable=broad-exception-caught,protected-access,line-too-long
# pylint: disable=missing-function-docstring,unused-argument,too-many-lines
# pylint: disable=import-outside-toplevel,wrong-import-position,wrong-import-order

# pyright: reportAttributeAccessIssue=false,reportUnusedParameter=false,reportUnusedImport=false

import logging
import os
import sys

from aiohttp import web

from ._bootstrap import WACTORZ_BOOTSTRAP  # noqa: F401 # pylint: disable=unused-import
from .monitor import (
    api_actors,
    api_reset,
    api_system,
    app,
    chat,
    cost,
    events,
    lifecycle,
    mqtt,
    runtime,
    static_site,
    ws,
)

Response = web.Response | web.FileResponse | web.StreamResponse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── Entry point ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Wactorz Monitor Server")
    parser.add_argument("--broker", default=os.getenv("WACTORZ_BROKER", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-ws-port", type=int, default=int(os.getenv("MQTT_WS_PORT", "9001")))
    parser.add_argument("--ws-port", type=int, default=int(os.getenv("MONITOR_PORT", "8888")))
    args = parser.parse_args()

    this_module = sys.modules[__name__]
    this_module.MQTT_BROKER = args.broker  # pyright: ignore[reportAttributeAccessIssue]
    this_module.MQTT_PORT = args.mqtt_port  # pyright: ignore[reportAttributeAccessIssue]
    this_module.MQTT_WS_PORT = args.mqtt_ws_port  # pyright: ignore[reportAttributeAccessIssue]
    this_module.WS_PORT = args.ws_port  # pyright: ignore[reportAttributeAccessIssue]

    app.cli_main()


# ── Compatibility façade ──────────────────────────────────────────────────────
# The shared mutable state lives in wactorz.monitor.runtime and extracted
# helpers in their monitor.* modules. External code and tests keep using this
# module's attributes (including pre-split underscore names); forward reads AND
# writes to the real home so injection/monkeypatching is seen by every caller.
_FORWARD = {
    # runtime state (clean names)
    **{
        n: (runtime, n)
        for n in (
            "registry",
            "db",
            "state",
            "ws_clients",
            "mqtt_client_ref",
            "mqtt_connected",
            "hard_resetting",
            "deleted_agent_ids",
            "DELETED_IDS_MAX",
            "MQTT_BROKER",
            "MQTT_PORT",
            "MQTT_WS_PORT",
            "WS_PORT",
            "MQTT_TOPICS",
            "IO_GATEWAY_ID",
            "is_deleted",
            "mark_deleted",
            "undelete",
        )
    },
    # static_site module
    **{
        n: (static_site, n)
        for n in (
            "index_handler",
            "static_handler",
            "docs_handler",
            "docs_redirect",
            "csp_policy",
            "FRONTEND_DIST",
            "FRONTEND_PUBLIC",
            "DOCS_SITE",
        )
    },
    # app module (composition root + entry point)
    **{n: (app, n) for n in ("main", "cli_main", "build_app", "check_ws_port")},
    # api modules
    **{
        n: (api_actors, n)
        for n in (
            "send_message_handler",
            "delete_actor_handler",
            "pause_actor_handler",
            "resume_actor_handler",
            "actor_metrics_handler",
            "actors_handler",
            "actor_handler",
            "actor_history_handler",
        )
    },
    **{
        n: (api_system, n)
        for n in (
            "health_handler",
            "cost_handler",
            "cost_limit_handler",
            "cost_reset_handler",
            "chat_log_handler",
            "config_handler",
            "feed_handler",
        )
    },
    **{n: (api_reset, n) for n in ("reset_handler", "survives_factory_reset", "HA_SYSTEM_AGENTS")},
    # mqtt module
    **{
        n: (mqtt, n)
        for n in ("mqtt_listener", "set_mqtt_status", "check_mqtt", "broadcast_mqtt_msg")
    },
    # ws module
    **{n: (ws, n) for n in ("broadcast", "ws_handler", "handle_command")},
    # lifecycle module
    **{
        n: (lifecycle, n)
        for n in (
            "purge_agent_retained",
            "purge_node_desired_state",
            "purge_spawn_reconcile",
            "delete_agent",
        )
    },
    # chat module
    **{
        n: (chat, n)
        for n in (
            "route_chat",
            "handle_slash",
            "handle_chat_mqtt",
            "rest_chat_handler",
            "rest_chat_stop_handler",
            "track_chat_task",
            "chat_mode",
            "find_main",
            "parse_mention",
            "slash_deploy",
            "no_op_async",
            "inflight_chat_tasks",
        )
    },
    # cost module
    **{
        n: (cost, n)
        for n in (
            "ensure_lifetime_loaded",
            "record_lifetime_cost",
            "lifetime_cost_total",
            "reset_actor_cost",
            "historical_cost_usd",
            "historical_messages",
            "actor_cost",
            "best_cost",
            "best_msgs",
            "lifetime_cost",
            "lifetime_loaded",
            "LIFETIME_LEDGER_KEY",
        )
    },
    # events module (clean names)
    **{
        n: (events, n)
        for n in (
            "parse_topic",
            "update_agent",
            "add_log",
            "snapshot",
            "node_online",
        )
    },
}


def __getattr__(name: str):
    target = _FORWARD.get(name)
    if target is not None:
        return getattr(target[0], target[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _FacadeModule(type(sys.modules[__name__])):
    def __setattr__(self, name: str, value) -> None:
        target = _FORWARD.get(name)
        if target is not None:
            setattr(target[0], target[1], value)
        else:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
