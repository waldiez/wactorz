"""Composition root: build the aiohttp app, wire routes, run the server.

``build_app`` is the single place every route is registered, which makes the
monitor's HTTP surface readable in one screen. Extensions are wired in here too
(``setup_all``) — before the static catch-all, or ``/{path:.+}`` would shadow
their routes.
"""

import asyncio
import logging
import sys

from aiohttp import web
from aiohttp.typedefs import Handler

from . import (
    api_actors,
    api_reset,
    api_system,
    chat,
    mqtt,
    runtime,
    static_site,
    ws,
)

Response = web.Response | web.FileResponse | web.StreamResponse

logger = logging.getLogger(__name__)


# ── Startup preconditions ──────────────────────────────────────────────────
async def check_ws_port() -> bool:
    """Return True if WS_PORT is free to bind."""
    try:
        server = await asyncio.start_server(lambda r, w: None, "0.0.0.0", runtime.WS_PORT)
        server.close()
        await server.wait_closed()
        return True
    except OSError as exc:
        logger.error("[startup] Port %d already in use — %s", runtime.WS_PORT, exc)
        return False


async def main(exit_on_failure: bool = False) -> None:
    mqtt_ok = await mqtt.check_mqtt()
    port_ok = await check_ws_port()

    if not mqtt_ok or not port_ok:
        msg = []
        if not mqtt_ok:
            msg.append(f"MQTT broker unreachable ({runtime.MQTT_BROKER}:{runtime.MQTT_PORT})")
        if not port_ok:
            msg.append(f"Port {runtime.WS_PORT} already in use")
        err_msg = "; ".join(msg)
        logger.error("[startup] Cannot start: %s", err_msg)
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
        except Exception:  # pylint: disable=broad-exception-caught
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
    from ..ext import setup_all

    setup_all(app)

    app.router.add_get("/docs", static_site.docs_redirect)
    app.router.add_get("/docs/", static_site.docs_handler)
    app.router.add_get("/docs/{path:.+}", static_site.docs_handler)

    app.router.add_get("/{path:.+}", static_site.static_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", runtime.WS_PORT)
    await site.start()
    msg = f"Monitor  → http://localhost:{runtime.WS_PORT}/  [chat: {chat.chat_mode()}]"
    logger.info(msg)
    if static_site.DOCS_SITE.is_dir():
        msg = f"Docs     → http://localhost:{runtime.WS_PORT}/docs/"
        logger.info(msg)

    await mqtt.mqtt_listener()


def cli_main() -> None:
    if sys.platform == "win32":
        # On Windows we manage the loop manually so paho-mqtt's __del__ doesn't
        # race against a closed loop during interpreter shutdown, which would
        # produce spurious "RuntimeError: Event loop is closed" noise from
        # aiomqtt's _on_socket_close callback.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        exit_exc = None
        # pylint: disable=broad-exception-caught
        try:
            loop.run_until_complete(main(exit_on_failure=True))
        except (KeyboardInterrupt, SystemExit) as _exit:
            exit_exc = _exit
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
            if exit_exc is not None:
                raise exit_exc
    else:
        asyncio.run(main(exit_on_failure=True))
