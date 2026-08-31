"""REST endpoints for actors: listing, inspection, and control.

Read paths render the live registry (plus MQTT-reported state) into the
dashboard's card payloads; write paths start/stop/delete an actor or hand a
message to it.
"""

import asyncio
import json
import logging

from aiohttp import web
from aiohttp.web import Response

from ..core.actor import forbidden
from . import chat, cost, events, lifecycle, runtime, ws

logger = logging.getLogger(__name__)


def _actor_payload(ag: dict) -> dict:
    return {
        "id": ag.get("agent_id", ""),
        "name": ag.get("name", ""),
        "state": ag.get("state", "unknown"),
        "protected": ag.get("protected", False),
        "essential": ag.get("essential", False),
        "cpu": ag.get("cpu"),
        "mem": ag.get("mem"),
        "task": ag.get("task"),
        "messagesProcessed": ag.get("messages_processed"),
        "costUsd": ag.get("cost_usd"),
    }


async def send_message_handler(request: web.Request) -> Response:
    """Deliver a message to one actor (fire-and-forget)."""
    actor_id = request.match_info["actor_id"]
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = data.get("content", "").strip()
    if not content:
        return web.json_response({"error": "content required"}, status=400)
    actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    # This endpoint names an explicit target, but chat.route_chat re-derives the
    # target from the text and defaults to main — so without this the addressed
    # actor is dropped. Prepend the mention to route there, unless the caller
    # already addressed someone (@) or it's a slash command (/).
    routed = content if content.startswith(("@", "/")) else f"@{actor.name} {content}"
    chat.track_chat_task(asyncio.create_task(chat.route_chat(routed, chat.discard_reply)))
    return web.json_response({"status": "sent"})


async def delete_actor_handler(request: web.Request) -> Response:
    """Stop an actor and purge its retained state so it cannot be resurrected."""
    actor_id = request.match_info["actor_id"]
    # Resolve the dashboard's record first so remote agents (which aren't in
    # the local registry) can still be deleted via this endpoint. The earlier
    # 503/404 short-circuit made remote deletes impossible.
    record = runtime.state["agents"].get(actor_id) or {}
    if not record:
        # Fall back to local-registry lookup so a name-based ID still works.
        if runtime.registry is not None:
            actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
            if actor is None:
                return web.json_response({"error": "actor not found"}, status=404)
            if getattr(actor, "protected", False):
                return web.json_response({"error": "actor is protected"}, status=403)
            actor_id = actor.actor_id
        else:
            return web.json_response({"error": "actor not found"}, status=404)
    if record.get("protected"):
        return web.json_response({"error": "actor is protected"}, status=403)
    routed = await lifecycle.delete_agent(actor_id)
    await ws.broadcast(
        {"type": events.DELETE_AGENT_FRAME, "agent_id": actor_id, "state": events.snapshot()}
    )
    return web.Response(status=200, text=f"stopping ({routed})")


def _remote_entry(actor_id: str) -> tuple[str, dict] | tuple[None, None]:
    """The MQTT-reported record for an agent that is not in the local registry.

    Agents living on a runner node never appear in the local registry — the read
    path documents that as a contract — so a lifecycle command aimed at one has
    to be recognised here or it looks exactly like a typo. Matched on id first
    and then on name, because a caller may sensibly use either.

    Returns the *id*, not whatever was asked for: a command to a remote agent is
    published to a topic keyed by id, and a name would address nothing.
    """
    entry = runtime.state["agents"].get(actor_id)
    if entry is not None:
        return actor_id, entry
    for known_id, known in runtime.state["agents"].items():
        if known.get("name") == actor_id:
            return known_id, known
    return None, None


async def _lifecycle_handler(request: web.Request, command: str, status: str) -> Response:
    """Run a lifecycle command and report what happened.

    Local actors are driven in process. This used to publish to the broker so the
    actor could receive its own command back over the network, which meant that
    with the broker down nothing happened at all while the response still said it
    had — the dashboard would show an agent running that the user had just stopped.

    Remote agents are reached the only way they can be, over the broker. They are
    absent from the local registry by design, so without this an agent the
    dashboard can stop was one this API answered 404 for.

    `run_command` carries the part that was missing: the feed entry, the reported
    state and the patch to open dashboards. Without it a command over REST
    executed and then said nothing, and `GET /actors` reported the old state
    until the next heartbeat — or forever, with the broker down.
    """
    actor_id = request.match_info["actor_id"]
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)

    actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is not None:
        target = actor.actor_id
        protected = bool(getattr(actor, "protected", False))
        essential = bool(getattr(actor, "essential", False))
    else:
        target, entry = _remote_entry(actor_id)
        if target is None or entry is None:
            return web.json_response({"error": "actor not found"}, status=404)
        protected = bool(entry.get("protected", False))
        essential = bool(entry.get("essential", False))

    # 403 is for a rule about the agent; a command declined because the state is
    # wrong answers 409 below. Asked of the same predicate the actor uses, so a
    # caller cannot be refused here for something the actor would have allowed.
    if forbidden(command, protected=protected, essential=essential):
        return web.json_response({"error": f"{command} is not allowed for this actor"}, status=403)

    routed = await lifecycle.run_command(target, command, "rest-api")
    if routed == "refused":
        return web.json_response({"error": f"{command} was refused"}, status=409)
    if not routed:
        # Reached only for a remote agent with no broker to reach it through.
        # 503 rather than 200: the command went nowhere, and a caller that was
        # told otherwise would have no way to find that out.
        return web.json_response({"error": f"{command} could not be delivered"}, status=503)
    return web.json_response({"status": status})


async def start_actor_handler(request: web.Request) -> Response:
    """Bring a stopped actor back up, under supervision again."""
    return await _lifecycle_handler(request, "start", "starting")


async def stop_actor_handler(request: web.Request) -> Response:
    """Stop a running actor."""
    return await _lifecycle_handler(request, "stop", "stopping")


async def actor_metrics_handler(request: web.Request) -> Response:
    """Return one actor's counters (messages, cost, tokens) for its detail view."""
    actor_id = request.match_info["actor_id"]
    ag = runtime.state["agents"].get(actor_id)
    actor = None
    if runtime.registry is not None:
        actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is None and ag is None:
        return web.json_response({"error": "actor not found"}, status=404)
    metrics_obj = getattr(actor, "metrics", None) if actor else None
    return web.json_response(
        {
            "messages_processed": (
                getattr(metrics_obj, "messages_processed", None)
                or (ag.get("messages_processed") if ag else None)
                or 0
            ),
            "cpu": ag.get("cpu") if ag else None,
            "mem": ag.get("mem") if ag else None,
            "task": ag.get("task") if ag else None,
            "cost_usd": (
                getattr(actor, "total_cost_usd", None) or (ag.get("cost_usd") if ag else None)
            ),
        }
    )


async def actors_handler(request: web.Request) -> Response:
    """List every local actor for the dashboard grid.

    Deliberately excludes remote-runner agents — see the CONTRACT note below.
    """
    # Prefer the live registry (injected at boot via runtime.set_registry) —
    # actor objects carry the authoritative protected flag.  Fall back to the
    # MQTT-derived state dict when no registry was injected (legacy MQTT mode).
    #
    # CONTRACT: the registry path intentionally excludes remote-runner agents
    # (they are not in the local Python registry).  The frontend relies on this
    # to distinguish local vs remote agents: any agent absent from this response
    # but present via MQTT heartbeat with a "node" field is a remote agent and
    # must NOT be evicted by the 15-second REST reconcile cycle.
    if runtime.registry is not None:
        # Extensions may enrich each actor payload (e.g. identity did/handle);
        # gathered once, applied per actor. No-op when no extension provides one.
        from ..ext import collect_actor_decorators

        decorators = collect_actor_decorators(request.app)
        result = []
        for actor in runtime.registry.all_actors():
            if runtime.is_deleted(actor.actor_id):
                continue
            ag = runtime.state["agents"].get(actor.actor_id, {})
            payload = {
                "id": actor.actor_id,
                "name": actor.name,
                "state": ag.get("state", "unknown"),
                "protected": bool(getattr(actor, "protected", False)),
                "cpu": ag.get("cpu"),
                "mem": ag.get("mem"),
                "task": ag.get("task"),
                "messagesProcessed": ag.get("messages_processed")
                if ag.get("messages_processed") is not None
                else getattr(getattr(actor, "metrics", None), "messages_processed", None),
                "costUsd": cost.actor_cost(actor, ag),
            }
            for decorate in decorators:
                decorate(actor, payload)
            result.append(payload)
        return web.json_response(result)
    return web.json_response([_actor_payload(ag) for ag in runtime.state["agents"].values()])


async def actor_handler(request: web.Request) -> Response:
    """Return a single actor's card payload."""
    actor_id = request.match_info["actor_id"]
    ag = runtime.state["agents"].get(actor_id)
    if ag is None:
        return web.json_response({"error": "actor not found"}, status=404)
    return web.json_response(_actor_payload(ag))


async def actor_history_handler(request: web.Request) -> Response:
    """Return an actor's persisted conversation history."""
    actor_id = request.match_info["actor_id"]

    # Resolve actor: the frontend sends the agent NAME (not UUID), so try
    # direct UUID lookup first, then fall back to name-based lookup.
    actor = None
    if runtime.registry is not None:
        actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)

    if actor is not None and hasattr(actor, "recall"):
        history = actor.recall("conversation_history", [])
    elif runtime.db is not None:
        # Actor not in registry (deleted or name-only lookup) — read from SQLite.
        # actor_id might be a display name (e.g. "main") — try it directly.
        try:
            row = runtime.db.conn.execute(
                "SELECT value FROM kv_store WHERE agent=? AND key='conversation_history'",
                (actor_id,),
            ).fetchone()
            history = json.loads(row[0]) if row else []
        except Exception:
            history = []
    else:
        history = []

    visible = [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    return web.json_response(visible)
