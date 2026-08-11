"""REST API interface — connect any chat platform via webhooks.

`POST /chat` with `{"message": "..."}` returns `{"response": "..."}`; the
remaining routes expose actor listing, lifecycle and metrics.
"""

import asyncio
import hmac
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response

from ...config import CONFIG, MAX_REQUEST_BYTES
from ...monitoring import PrometheusMonitor

if TYPE_CHECKING:
    from ...agents.main_actor import MainActor
    from ...core.actor import Actor

logger = logging.getLogger(__name__)

# Reachable without a key so container and uptime probes keep working.
UNGUARDED_PATHS = frozenset({"/health"})


async def _json_object(request: Request) -> dict[str, Any] | None:
    """The body as a JSON object, or None if it is anything else.

    Handlers read named fields off the body, so a list or a bare string has no
    field to read and is a client mistake — 400 — rather than a server fault.
    """
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


class RESTInterface:
    """Generic REST API interface. Connect any chat platform via webhooks.
    POST /chat with {"message": "..."} → returns {"response": "..."}
    """

    def __init__(
        self, main_actor: "MainActor", port: int = 8000, api_key: str | None = None
    ) -> None:
        self.agent = main_actor
        self.port = port
        self.api_key = api_key
        self._monitor = PrometheusMonitor(lambda: getattr(self.agent, "_registry", None))

    def _authorized(self, request: Request) -> bool:
        """Whether a request may proceed.

        Open when no key is configured. Otherwise the key must arrive as
        `X-API-Key` or `Authorization: Bearer <key>` — the latter so scrapers
        that only speak standard auth headers, Prometheus among them, can
        still reach a guarded endpoint.
        """
        if not self.api_key:
            return True
        presented = request.headers.get("X-API-Key", "")
        if not presented:
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            if scheme.lower() == "bearer":
                presented = token.strip()
        return hmac.compare_digest(presented, self.api_key)

    @staticmethod
    def _normalize_state(state: str) -> str:
        if state == "idle":
            return "initializing"
        return state

    def _actor_payload(self, actor: "Actor") -> dict[str, Any]:
        status = actor.get_status()
        return {
            "id": actor.actor_id,
            "name": actor.name,
            "state": self._normalize_state(status.get("state", "unknown")),
            "protected": bool(getattr(actor, "protected", False)),
        }

    def _metrics_payload(self, actor: "Actor") -> dict[str, Any]:
        # The token and cost fields exist only on LLM-backed actors; anything
        # else genuinely has nothing to spend, so zero is the right answer.
        return {
            "messages_received": actor.metrics.messages_received,
            "messages_processed": actor.metrics.messages_processed,
            "messages_failed": actor.metrics.errors,
            "heartbeats": actor.metrics.heartbeats,
            "last_message_at": int(actor.metrics.last_heartbeat),
            "restart_count": actor.metrics.restart_count,
            "llm_input_tokens": getattr(actor, "total_input_tokens", 0),
            "llm_output_tokens": getattr(actor, "total_output_tokens", 0),
            "llm_cost_usd": getattr(actor, "total_cost_usd", 0.0),
        }

    def _latest_ha_map_payload(self) -> dict[str, Any] | None:
        registry = getattr(self.agent, "_registry", None)
        if registry is None:
            return None
        actor = registry.find_by_name("home-assistant-map-agent")
        if actor is None:
            return None
        if hasattr(actor, "get_latest_map_payload"):
            payload = actor.get_latest_map_payload()
        elif hasattr(actor, "recall"):
            payload = actor.recall("latest_map_payload", None)
        else:
            payload = None
        return payload if isinstance(payload, dict) else None

    def build_app(self) -> web.Application:
        """Assemble the routes and middlewares, without binding a port."""
        registry = self.agent._registry

        def _lookup_actor(actor_id: str) -> "Actor | None":
            if registry is None:
                return None
            return registry.get(actor_id)

        async def chat_endpoint(request) -> Response:
            body = await _json_object(request)
            if body is None:
                return web.json_response({"error": "Expected a JSON object"}, status=400)
            message = body.get("message", "")
            agent_name = body.get("agent_name") or "main"
            if not message:
                return web.json_response({"error": "No message provided"}, status=400)

            response = await self.agent.process_user_input(message)
            return web.json_response(
                {
                    "status": "sent",
                    "agent": agent_name,
                    "response": response,
                }
            )

        async def agents_endpoint(request: Request) -> Response:
            if registry is None:
                return web.json_response([])
            actors = [self._actor_payload(actor) for actor in registry.all_actors()]
            return web.json_response(actors)

        async def command_endpoint(request: Request) -> Response:
            body = await _json_object(request)
            if body is None:
                return web.json_response({"error": "Expected a JSON object"}, status=400)
            target = body.get("target")
            command = body.get("command")
            from ...core.actor import MessageType

            cmd_map = {
                "start": MessageType.START,
                "stop": MessageType.STOP,
                "pause": MessageType.PAUSE,
                "resume": MessageType.RESUME,
            }
            if command in cmd_map and target:
                await self.agent.send_command(target, cmd_map[command])
                return web.json_response({"ok": True})
            return web.json_response({"error": "Invalid command"}, status=400)

        async def actor_endpoint(request: Request) -> Response:
            actor = _lookup_actor(request.match_info["actor_id"])
            if actor is None:
                return web.Response(status=404, text="actor not found")
            return web.json_response(self._actor_payload(actor))

        async def actor_message_endpoint(request: Request) -> Response:
            actor = _lookup_actor(request.match_info["actor_id"])
            if actor is None:
                return web.Response(status=404, text="actor not found")
            body = await _json_object(request)
            if body is None:
                return web.json_response({"error": "Expected a JSON object"}, status=400)
            content = body.get("content", "")
            if not content:
                return web.json_response({"error": "No content provided"}, status=400)
            from ...core.actor import MessageType

            await self.agent.send(
                actor.actor_id, MessageType.TASK, {"text": content, "content": content}
            )
            return web.json_response({"status": "sent"})

        async def _lifecycle_endpoint(request: Request, command: str, status: str) -> Response:
            """Run a lifecycle command through the actor's own implementation.

            These endpoints used to call ``actor.stop()``/``pause()``/``resume()``
            directly. That skipped the supervision release, so an actor stopped
            here looked to the watchdog exactly like one that had crashed and was
            restarted moments later.
            """
            actor = _lookup_actor(request.match_info["actor_id"])
            if actor is None:
                return web.json_response({"error": "actor not found"}, status=404)
            if getattr(actor, "protected", False):
                return web.json_response({"error": "actor is protected"}, status=403)
            if not await actor.apply_command(command):
                return web.json_response({"error": f"{command} was refused"}, status=409)
            return web.json_response({"status": status})

        async def start_actor_endpoint(request: Request) -> Response:
            return await _lifecycle_endpoint(request, "start", "starting")

        async def stop_actor_endpoint(request: Request) -> Response:
            # apply_command("stop") releases from supervision but leaves the
            # actor registered; this endpoint has always removed it as well.
            response = await _lifecycle_endpoint(request, "stop", "stopping")
            if response.status == 200 and registry is not None:
                actor = _lookup_actor(request.match_info["actor_id"])
                if actor is not None:
                    await registry.unregister(actor.actor_id)
            return response

        async def pause_actor_endpoint(request: Request) -> Response:
            return await _lifecycle_endpoint(request, "pause", "pausing")

        async def resume_actor_endpoint(request: Request) -> Response:
            return await _lifecycle_endpoint(request, "resume", "resuming")

        async def metrics_endpoint(request: Request) -> Response:
            actor = _lookup_actor(request.match_info["actor_id"])
            if actor is None:
                return web.Response(status=404, text="actor not found")
            return web.json_response(self._metrics_payload(actor))

        async def health_endpoint(request: Request) -> Response:
            return web.json_response({"status": "ok"})

        async def prometheus_metrics_endpoint(request: Request) -> Response:
            return self._monitor.metrics_response()

        async def ha_map_latest_endpoint(request: Request) -> Response:
            payload = self._latest_ha_map_payload()
            if payload is None:
                return web.json_response(
                    {"error": "Home Assistant map snapshot not available"}, status=404
                )
            return web.json_response(payload)

        @web.middleware
        async def auth_middleware(request: Request, handler: Any) -> Response:
            """Apply the key check to every route but the probe endpoints.

            Guarding here rather than per handler means a route added later is
            covered by default, instead of relying on whoever adds it to
            remember.
            """
            if request.path not in UNGUARDED_PATHS and not self._authorized(request):
                return web.json_response({"error": "Unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(
            middlewares=[self._monitor.middleware, auth_middleware],
            client_max_size=MAX_REQUEST_BYTES,
        )
        app.router.add_get("/health", health_endpoint)
        app.router.add_get("/metrics", prometheus_metrics_endpoint)
        app.router.add_get("/ha-map", ha_map_latest_endpoint)
        app.router.add_get("/actors", agents_endpoint)
        app.router.add_get("/actors/{actor_id}", actor_endpoint)
        app.router.add_post("/actors/{actor_id}/message", actor_message_endpoint)
        app.router.add_delete("/actors/{actor_id}", stop_actor_endpoint)
        app.router.add_post("/actors/{actor_id}/start", start_actor_endpoint)
        app.router.add_post("/actors/{actor_id}/pause", pause_actor_endpoint)
        app.router.add_post("/actors/{actor_id}/resume", resume_actor_endpoint)
        app.router.add_get("/actors/{actor_id}/metrics", metrics_endpoint)
        app.router.add_post("/chat", chat_endpoint)
        app.router.add_get("/agents", agents_endpoint)
        app.router.add_post("/agents/command", command_endpoint)
        return app

    async def run(self) -> None:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, CONFIG.bind_host, self.port)
        await site.start()
        logger.info("[REST] API running at http://%s:%s", CONFIG.bind_host, self.port)
        await asyncio.Event().wait()
