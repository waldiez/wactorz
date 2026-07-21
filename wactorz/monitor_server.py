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

import asyncio
import logging
import os
import sys

from aiohttp import web
from aiohttp.typedefs import Handler

from ._bootstrap import WACTORZ_BOOTSTRAP  # noqa: F401 # pylint: disable=unused-import
from .monitor import (
    api_actors,
    api_reset,
    api_system,
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


# ── helpers ────────────────────────────────────────────────────────────────
async def _check_ws_port() -> bool:
    """Return True if WS_PORT is free to bind."""
    try:
        server = await asyncio.start_server(lambda r, w: None, "0.0.0.0", runtime.WS_PORT)
        server.close()
        await server.wait_closed()
        return True
    except OSError as exc:
        logger.error(f"[startup] Port {runtime.WS_PORT} already in use — {exc}")
        return False


# ── Entry point ────────────────────────────────────────────────────────────


async def main(exit_on_failure: bool = False):
    # ... (startup checks remain same) ...
    mqtt_ok = await mqtt.check_mqtt()
    port_ok = await _check_ws_port()

    if not mqtt_ok or not port_ok:
        msg = []
        if not mqtt_ok:
            msg.append(f"MQTT broker unreachable ({runtime.MQTT_BROKER}:{runtime.MQTT_PORT})")
        if not port_ok:
            msg.append(f"Port {runtime.WS_PORT} already in use")
        logger.error(f"[startup] Cannot start: {'; '.join(msg)}")
        if exit_on_failure:
            raise SystemExit(1)
        return

    @web.middleware
    async def cors_middleware(request: web.Request, handler: Handler) -> Response:
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                }
            )
        response = await handler(request)
        try:
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        except Exception:
            pass
        return response

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", static_site.index_handler)
    app.router.add_get("/health", api_system.health_handler)
    app.router.add_get("/api/cost", api_system.cost_handler)
    app.router.add_get("/cost", api_system.cost_handler)
    app.router.add_post("/api/cost/limit", api_system.cost_limit_handler)
    app.router.add_post("/cost/limit", api_system.cost_limit_handler)
    app.router.add_post("/api/cost/reset", api_system.cost_reset_handler)
    app.router.add_post("/cost/reset", api_system.cost_reset_handler)
    app.router.add_get("/ws", ws.ws_handler)

    # Actor collection
    app.router.add_get("/api/actors", api_actors.actors_handler)
    app.router.add_get("/actors", api_actors.actors_handler)

    # Actor control — sub-routes must be registered before /{actor_id} catch-all
    app.router.add_post("/api/actors/{actor_id}/message", api_actors.send_message_handler)
    app.router.add_post("/actors/{actor_id}/message", api_actors.send_message_handler)
    app.router.add_post("/api/actors/{actor_id}/pause", api_actors.pause_actor_handler)
    app.router.add_post("/actors/{actor_id}/pause", api_actors.pause_actor_handler)
    app.router.add_post("/api/actors/{actor_id}/resume", api_actors.resume_actor_handler)
    app.router.add_post("/actors/{actor_id}/resume", api_actors.resume_actor_handler)
    app.router.add_get("/api/actors/{actor_id}/metrics", api_actors.actor_metrics_handler)
    app.router.add_get("/actors/{actor_id}/metrics", api_actors.actor_metrics_handler)
    app.router.add_get("/api/actors/{actor_id}/history", api_actors.actor_history_handler)
    app.router.add_get("/actors/{actor_id}/history", api_actors.actor_history_handler)

    # Actor CRUD
    app.router.add_get("/api/actors/{actor_id}", api_actors.actor_handler)
    app.router.add_get("/actors/{actor_id}", api_actors.actor_handler)
    app.router.add_delete("/api/actors/{actor_id}", api_actors.delete_actor_handler)
    app.router.add_delete("/actors/{actor_id}", api_actors.delete_actor_handler)

    # Chat (REST fire-and-forget)
    app.router.add_post("/api/chat", chat.rest_chat_handler)
    app.router.add_post("/chat", chat.rest_chat_handler)
    app.router.add_post("/api/chat/stop", chat.rest_chat_stop_handler)
    app.router.add_post("/chat/stop", chat.rest_chat_stop_handler)

    app.router.add_get("/api/chats", api_system.chat_log_handler)
    app.router.add_get("/chats", api_system.chat_log_handler)

    app.router.add_get("/api/config", api_system.config_handler)
    app.router.add_get("/config", api_system.config_handler)
    app.router.add_get("/api/feed", api_system.feed_handler)
    app.router.add_get("/feed", api_system.feed_handler)
    app.router.add_post("/api/reset", api_reset.reset_handler)
    app.router.add_get("/favicon.svg", static_site.index_handler)

    # Extensions (wactorz/ext/): additive features register their own routes
    # and startup/teardown hooks here. Must run BEFORE the docs/static
    # catch-alls below, or the /{path:.+} route shadows extension routes.
    from .ext import setup_all

    setup_all(app)

    app.router.add_get("/docs", static_site.docs_redirect)
    app.router.add_get("/docs/", static_site.docs_handler)
    app.router.add_get("/docs/{path:.+}", static_site.docs_handler)

    app.router.add_get("/{path:.+}", static_site.static_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", runtime.WS_PORT)
    await site.start()
    logger.info(f"Monitor  → http://localhost:{runtime.WS_PORT}/  [chat: {chat.chat_mode()}]")
    if static_site.DOCS_SITE.is_dir():
        logger.info(f"Docs     → http://localhost:{runtime.WS_PORT}/docs/")

    await mqtt.mqtt_listener()


def cli_main() -> None:
    if sys.platform == "win32":
        # On Windows we manage the loop manually so paho-mqtt's __del__ doesn't
        # race against a closed loop during interpreter shutdown, which would
        # produce spurious "RuntimeError: Event loop is closed" noise from
        # aiomqtt's _on_socket_close callback.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(main(exit_on_failure=True))
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            # Cancel all pending tasks so paho gets a chance to close its
            # sockets while the loop is still alive.
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            # Brief sleep lets paho's internal socket-close callback fire
            # before we seal the loop for good.
            try:
                loop.run_until_complete(asyncio.sleep(0.25))
            except Exception:
                pass
            loop.close()
    else:
        asyncio.run(main(exit_on_failure=True))


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

    cli_main()


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
