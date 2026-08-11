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

from ..config import CONFIG, MAX_REQUEST_BYTES
from . import (
    api_actors,
    api_reset,
    api_system,
    chat,
    mqtt,
    origins,
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
        server = await asyncio.start_server(lambda r, w: None, CONFIG.bind_host, runtime.WS_PORT)
        server.close()
        await server.wait_closed()
        return True
    except OSError as exc:
        logger.error("[startup] Port %d already in use — %s", runtime.WS_PORT, exc)
        return False


def build_app() -> web.Application:
    """Create the monitor app with every route registered.

    Kept separate from :func:`main` so the full HTTP surface is readable in
    one place, and so tests can build the real app without binding a port.
    """

    @web.middleware
    async def cors_middleware(request: web.Request, handler: Handler) -> Response:
        """Decide whether a browser somewhere else may act on this server.

        In middleware rather than per route: nearly every path below is
        registered twice, under `/api/x` and a bare `/x`, and a per-route
        decorator would guard whichever alias its author remembered.
        """
        refusal = origins.refuse(request)
        if refusal is not None:
            return refusal

        origin = origins.allowed_origin(request)
        if request.method == "OPTIONS":
            return web.Response(headers=origins.cors_headers(origin))
        response = await handler(request)
        try:
            response.headers.update(origins.cors_headers(origin))
        except Exception:
            pass
        return response

    app = web.Application(middlewares=[cors_middleware], client_max_size=MAX_REQUEST_BYTES)
    # Expose the registry to extensions (via app.get) before setup_all() runs.
    # None in standalone/legacy MQTT mode — consumers handle that.
    from ..core import contract

    app[contract.ACTOR_REGISTRY] = runtime.registry

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
    app.router.add_post("/api/actors/{actor_id}/start", api_actors.start_actor_handler)
    app.router.add_post("/actors/{actor_id}/start", api_actors.start_actor_handler)
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

    # Extensions (wactorz/ext/): additive features register their own routes
    # and startup/teardown hooks here. Must run BEFORE the docs/static
    # catch-alls below, or the /{path:.+} route shadows extension routes.
    from ..ext import setup_all

    setup_all(app)

    app.router.add_get("/docs", static_site.docs_redirect)
    app.router.add_get("/docs/", static_site.docs_handler)
    app.router.add_get("/docs/{path:.+}", static_site.docs_handler)

    app.router.add_get("/favicon.svg", static_site.index_handler)
    app.router.add_get("/{path:.+}", static_site.static_handler)
    return app


async def main(exit_on_failure: bool = False) -> None:
    """Check preconditions, serve the app, then run the broker listener forever.

    With ``exit_on_failure`` a failed precondition raises ``SystemExit`` (the
    console-script path); otherwise it returns so an embedding app can carry on.
    """
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

    origins.log_mode()
    app = build_app()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG.bind_host, runtime.WS_PORT)
    await site.start()
    msg = f"Monitor  → http://localhost:{runtime.WS_PORT}/"
    logger.info(msg)
    if static_site.DOCS_SITE.is_dir():
        msg = f"Docs     → http://localhost:{runtime.WS_PORT}/docs/"
        logger.info(msg)

    # Held in a local so the task is not garbage-collected mid-flight, and
    # cancelled below so shutdown does not leave it running.
    totals_task = asyncio.create_task(ws.totals_broadcaster())
    try:
        await mqtt.mqtt_listener()
    finally:
        # cancel() only requests it; awaiting is what makes shutdown mean the
        # task has actually unwound. No timeout needed here — unlike an actor's
        # tasks, this one is a sleep and a broadcast, so it stops immediately.
        totals_task.cancel()
        await asyncio.gather(totals_task, return_exceptions=True)


def cli_main() -> None:
    """Console-script entry point (``wactorz-monitor``).

    Windows drives the loop manually so paho-mqtt can close its sockets before
    interpreter shutdown; every other platform just uses ``asyncio.run``.
    """
    if sys.platform == "win32":
        # On Windows we manage the loop manually so paho-mqtt's __del__ doesn't
        # race against a closed loop during interpreter shutdown, which would
        # produce spurious "RuntimeError: Event loop is closed" noise from
        # aiomqtt's _on_socket_close callback.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        exit_exc = None
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
