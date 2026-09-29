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
from ...core.actor import forbidden
from ...monitoring import PrometheusMonitor
from ...web import origins, probes

if TYPE_CHECKING:
    from ...agents.main import MainActor
    from ...core.actor import Actor
    from ...core.registry import ActorSystem

logger = logging.getLogger(__name__)

# Reachable without a key so container and uptime probes keep working.
UNGUARDED_PATHS = probes.PROBE_PATHS


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
        self,
        main_actor: "MainActor",
        port: int = 8000,
        api_key: str | None = None,
        system: "ActorSystem | None" = None,
    ) -> None:
        self.agent = main_actor
        self.port = port
        self.api_key = api_key
        #: What the readiness probe reports on. Without one, this interface was
        #: built outside a running system and is never ready.
        self.system = system
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

            Through ``apply_command`` rather than the actor's own methods: it is
            the one place the lifecycle rules live, so a stop releases the actor
            from supervision first. Without that the watchdog reads the silence
            as a crash and restarts what was deliberately stopped.
            """
            actor = _lookup_actor(request.match_info["actor_id"])
            if actor is None:
                return web.json_response({"error": "actor not found"}, status=404)
            if forbidden(
                command,
                protected=bool(getattr(actor, "protected", False)),
                essential=bool(getattr(actor, "essential", False)),
            ):
                return web.json_response(
                    {"error": f"{command} is not allowed for this actor"}, status=403
                )
            if not await actor.apply_command(command):
                return web.json_response({"error": f"{command} was refused"}, status=409)
            return web.json_response({"status": status})

        async def start_actor_endpoint(request: Request) -> Response:
            return await _lifecycle_endpoint(request, "start", "starting")

        async def stop_actor_endpoint(request: Request) -> Response:
            """Stop, leaving the actor registered — it can be started again."""
            return await _lifecycle_endpoint(request, "stop", "stopping")

        async def delete_actor_endpoint(request: Request) -> Response:
            """Delete: stop, unregister, and drop the spawn-registry entry.

            The `delete` verb rather than `stop`: they are refused under
            different rules, and asking the policy about a stop would let a
            protected actor be deleted here. It also clears the spawn-registry
            entry, which an unregister alone leaves behind to be restored on the
            next start.
            """
            response = await _lifecycle_endpoint(request, "delete", "deleting")
            # Belt and braces: apply_command unregisters through the actor's own
            # registry reference, which an actor built outside the system may not
            # carry. Unregistering twice is a no-op.
            if response.status == 200 and registry is not None:
                actor = _lookup_actor(request.match_info["actor_id"])
                if actor is not None:
                    await registry.unregister(actor.actor_id)
            return response

        async def metrics_endpoint(request: Request) -> Response:
            actor = _lookup_actor(request.match_info["actor_id"])
            if actor is None:
                return web.Response(status=404, text="actor not found")
            return web.json_response(self._metrics_payload(actor))

        async def readiness_endpoint(request: Request) -> Response:
            if self.system is None:
                return probes.readiness_response({"supervisor": "not started"})
            return probes.readiness_response(await probes.readiness(self.system))

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
        async def origin_middleware(request: Request, handler: Any) -> Response:
            """With no key, refuse a host or origin that is not this machine's own.

            Without a key the API is open, and these checks are all that keeps a
            web page out of it: one that rebinds its own name to this address, or
            posts to it from another site. With a key they add nothing, since
            neither kind of page can present the key, and the host check would
            refuse the name a scraper on the container network uses. The probe
            endpoints are left alone either way: they change nothing and say only
            that the process is up, and a load balancer asks under its own name.
            """
            if not self.api_key and request.path not in UNGUARDED_PATHS:
                refusal = origins.refuse(request)
                if refusal is not None:
                    return refusal
            return await handler(request)

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
            middlewares=[self._monitor.middleware, origin_middleware, auth_middleware],
            client_max_size=MAX_REQUEST_BYTES,
        )
        for path in sorted(probes.LIVENESS_PATHS):
            app.router.add_get(path, probes.liveness_handler)
        for path in sorted(probes.READINESS_PATHS):
            app.router.add_get(path, readiness_endpoint)
        app.router.add_get("/metrics", prometheus_metrics_endpoint)
        app.router.add_get("/ha-map", ha_map_latest_endpoint)
        app.router.add_get("/actors", agents_endpoint)
        app.router.add_get("/actors/{actor_id}", actor_endpoint)
        app.router.add_post("/actors/{actor_id}/message", actor_message_endpoint)
        app.router.add_delete("/actors/{actor_id}", delete_actor_endpoint)
        app.router.add_post("/actors/{actor_id}/start", start_actor_endpoint)
        app.router.add_post("/actors/{actor_id}/stop", stop_actor_endpoint)
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
