"""Chat routing for the monitor.

Decides where a user message goes — slash command, @mention, the main actor's
LLM, or a plain ``handle_message`` agent — and exposes the REST chat endpoints
plus the in-flight task tracker that ``POST /chat/stop`` cancels through.
"""

import asyncio
import inspect
import json
import logging
import socket
import time
import uuid
from collections.abc import Callable
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from ..agents.llm.attachments import to_blocks
from ..agents.lookup import MAIN_ACTOR_NAME, find_main_actor
from ..config import deploy_env_prefix, deploy_target, deploy_target_help, deploy_target_names
from ..core.actor import ActorState, Message, MessageType
from ..core.mqtt import mqtt_client
from . import runtime, uploads

logger = logging.getLogger(__name__)

# In-flight chat-generation tasks (WebSocket + REST paths) so POST /chat/stop can
# cancel a turn mid-stream.
inflight_chat_tasks: set = set()


def track_chat_task(task):
    """Register an in-flight chat-generation task so /chat/stop can cancel it."""
    inflight_chat_tasks.add(task)
    task.add_done_callback(inflight_chat_tasks.discard)
    return task


async def no_op_async() -> None:
    """No op. To use instead of lambdas."""


async def discard_reply(_text: str) -> None:
    """Drop a reply chunk, for fire-and-forget callers with nowhere to send it.

    ``route_chat`` does ``await reply_fn(text)``, so this must be a coroutine
    function taking one argument — a bare lambda raises TypeError on the first
    chunk and silently abandons the stream after the tokens are paid for.
    """


def parse_mention(content: str) -> tuple[str, str]:
    """Split a leading ``@agent`` mention off a message.

    Returns ``(target, remaining_text)``; target is ``""`` when unmentioned.
    """
    if content.startswith("@"):
        parts = content[1:].split(None, 1)
        return parts[0], (parts[1].strip() if len(parts) > 1 else "")
    return MAIN_ACTOR_NAME, content


# ── Catalog / experimental-agent presentation ──────────────────────────────


def catalog_agent_line(agent: dict[str, Any]) -> str:
    name = agent.get("name", "unknown")
    description = agent.get("description", "")
    return f"- `{name}` - {description}" if description else f"- `{name}`"


def format_catalog_agents_response(payload: dict[str, Any]) -> str:
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        return str(payload)

    show_experimental = bool(payload.get("show_experimental", False))
    recommended = [a for a in agents if isinstance(a, dict) and not a.get("experimental")]
    experimental = [a for a in agents if isinstance(a, dict) and a.get("experimental")]
    total = len(recommended) + len(experimental)

    lines = [
        "**Catalog agents**",
        f"`{total}` total - `{len(recommended)}` recommended, "
        f"`{len(experimental)}` experimental beta",
    ]

    if recommended:
        lines.extend(
            [
                "",
                "### Recommended",
                *(catalog_agent_line(agent) for agent in recommended),
            ]
        )

    if experimental:
        if show_experimental:
            lines.extend(
                [
                    "",
                    "### Experimental / Beta",
                    *(catalog_agent_line(agent) for agent in experimental),
                ]
            )
        else:
            # Hidden by default — nudge the user toward the opt-in instead of
            # listing beta agents in the normal view.
            lines.extend(
                [
                    "",
                    f"_{len(experimental)} experimental/beta agent(s) hidden — "
                    f"say `list experimental` to show them._",
                ]
            )

    return "\n".join(lines)


# Agents already warned in this process — so the beta banner shows on the first
# user message to an experimental agent, not on every turn.
beta_warned_agents: set = set()


def experimental_first_use_banner(agent_name: str) -> str | None:
    """One-time beta banner for the first user message to an experimental agent.

    Returns the banner the first time ``agent_name`` is messaged in this process,
    then None afterwards so the warning isn't repeated every turn. Non-experimental
    or unknown agents always return None. The experimental flag and per-agent
    warning come from main's manifest, populated by the catalog at startup.
    """
    if agent_name in beta_warned_agents:
        return None
    main = find_main_actor(runtime.registry)
    manifest = main._agent_manifests.get(agent_name) if main else None
    if not manifest or not manifest.get("experimental"):
        return None
    beta_warned_agents.add(agent_name)
    from ..agents.catalog_agent import BETA_WARNING

    warning = manifest.get("warning") or BETA_WARNING
    return f"⚠️ **{agent_name}** is an experimental/beta agent. {warning}\n\n"


# ── Slash commands ─────────────────────────────────────────────────────────
# Every handler receives a `reply_fn` coroutine — callers supply either an
# MQTT publisher or a WebSocket sender.  No global state, no monkey-patching.


async def slash_deploy(node: str, reply_fn) -> None:
    """Install and start a remote runner on the configured target for ``node``.

    Everything about the target — host, user, SSH auth, broker — comes from the
    environment (``DEPLOY_TARGETS`` plus a ``DEPLOY_<NODE>_*`` block). This used
    to accept ``host``/``user``/``password`` as chat arguments and, when no host
    was given, port-scan the local /24 for SSH. Both are gone: the scan turned a
    chat message into a LAN sweep, and the password argument put a live
    credential into the reply stream and the persisted conversation history.
    """
    target = deploy_target(node)
    if target is None:
        await reply_fn("[error] " + deploy_target_help(node))
        return

    host = target.host
    if not host:
        # No host configured — resolve <node>.local. A name lookup, not a sweep:
        # it asks about one host and learns nothing about any other.
        await reply_fn(f"[discover] No host configured for '{node}' — trying mDNS...")
        host = await _resolve_mdns(node) or ""
        if not host:
            await reply_fn(
                f"[error] Could not resolve '{node}.local'.\n"
                f"Set {deploy_env_prefix(node)}_HOST in your environment."
            )
            return
        await reply_fn(f"[discover] Found via mDNS: {node}.local → {host}")

    main_actor = find_main_actor(runtime.registry)
    if main_actor is None:
        await reply_fn("[error] Installer agent not available.")
        return

    await reply_fn(f"[deploy] Deploying to {target.user}@{host} as '{node}'... (20-60s)")
    result = await main_actor.delegate_to_installer(
        {
            "action": "node_deploy",
            "host": host,
            "node_name": target.name,
            "broker": target.broker or "localhost",
            "port": target.broker_port,
        },
        timeout=120.0,
    )

    if result.get("success"):
        await reply_fn(f"[OK] Node '{node}' is live!\n  \"spawn a CPU monitor agent on {node}\"")
    else:
        await reply_fn(f"[FAIL] {result.get('error', result)}")


async def _resolve_mdns(node: str) -> str | None:
    """Resolve ``<node>.local``, or None. Off the loop — a miss blocks for the
    resolver's full timeout, which would freeze every actor in the process.
    """
    try:
        return await asyncio.to_thread(socket.gethostbyname, f"{node}.local")
    except OSError:
        return None


async def handle_slash(text: str, reply_fn) -> bool:
    """Dispatch a slash command. Returns True if recognised.
    `reply_fn` is an async callable that sends a string back to the user.
    """
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/clear-plans":
        main_actor = find_main_actor(runtime.registry)
        if main_actor:
            main_actor.persist("_plan_cache", {})
        await reply_fn("[System: Plan cache cleared.]")
        return True

    if cmd == "/agents":
        if runtime.registry is None:
            await reply_fn("[agents] Registry not available.")
            return True
        lines = []
        for actor in runtime.registry.all_actors():
            status = actor.get_status() if hasattr(actor, "get_status") else {}
            st = status.get("state", "?")
            protected = " [protected]" if getattr(actor, "protected", False) else ""
            node = f" [{status['node']}]" if status.get("node") else ""
            lines.append(f"  [{st:8s}] @{actor.name:<22s} {actor.actor_id[:8]}{protected}{node}")
        await reply_fn("Agents:\n" + "\n".join(lines) if lines else "No agents running.")
        return True

    if cmd == "/nodes":
        main_actor = find_main_actor(runtime.registry)
        remote_nodes = main_actor.list_nodes() if (main_actor) else []
        local = [a.name for a in runtime.registry.all_actors()] if runtime.registry else []
        lines = [f"  {'local':20s} online   {', '.join('@' + n for n in local) or '(none)'}"]
        for nd in sorted(remote_nodes, key=lambda x: x["node"]):
            st = "online" if nd["online"] else "OFFLINE"
            names = ", ".join("@" + n for n in nd["agents"]) or "(no agents)"
            lines.append(f"  {nd['node']:20s} {st:6s}   {names}")
        if not remote_nodes:
            lines.append("  (no remote nodes — /deploy <node-name>)")
        await reply_fn("Nodes:\n" + "\n".join(lines))
        return True

    if cmd == "/migrate":
        if len(parts) < 3:
            await reply_fn("[usage] /migrate <agent-name> <target-node>")
            return True
        main_actor = find_main_actor(runtime.registry)
        if main_actor is None:
            await reply_fn("[error] migrate_agent not available.")
            return True
        await reply_fn(f"[migrating] @{parts[1]} → {parts[2]}...")
        result = await main_actor.migrate_agent(parts[1], parts[2])
        sym = "OK" if result.get("success") else "FAIL"
        await reply_fn(f"[{sym}] {result.get('message', str(result))}")
        return True

    if cmd == "/deploy":
        if len(parts) < 2:
            names = deploy_target_names()
            listing = "\n".join(f"  {n}" for n in names) or "  (none configured)"
            await reply_fn(f"[usage] /deploy <node-name>\nConfigured targets:\n{listing}")
            return True
        if len(parts) > 2:
            # The old form took host/user/password/broker here. Refuse without
            # echoing the extra words back — parts[3:] may be a live password,
            # and the reply is persisted into conversation history.
            await reply_fn(
                "[error] /deploy takes a node name only — host and SSH credentials "
                "now come from the environment, not from chat.\n\n" + deploy_target_help(parts[1])
            )
            return True
        await slash_deploy(node=parts[1], reply_fn=reply_fn)
        return True

    return False


#: Correlation id → the queue waiting for that request's RESULT. One entry per
#: in-flight chat turn; the interceptor below routes by this rather than
#: assuming there is only ever one.
_PENDING_REPLIES: dict[str, asyncio.Queue] = {}


def _install_reply_capture(target: Any) -> None:
    """Teach an agent's ``send`` to hand RESULTs back to the waiting chat turn.

    Agents reply to ``msg.reply_to or msg.sender_id``, and a chat turn is not a
    real actor, so the reply has nowhere to go unless it is intercepted.

    Installed **once per agent and never removed**. It used to be patched in and
    restored around each turn, which broke under two concurrent turns to the
    same agent: the second saved the first's interceptor as "the original", the
    first restored the real method — so the second's replies stopped being
    captured — and the second then restored the first's interceptor, leaving
    the agent permanently sending its results into an abandoned queue.

    Correlating by id also fixes the other half: the old interceptor captured
    *any* RESULT, so a reply meant for one turn could be handed to another.
    """
    if getattr(target, "_io_gateway_capture_installed", False):
        return
    original_send = target.send

    async def _capture_send(
        target_id: str,
        msg_type: MessageType,
        payload: Any = None,
        **kw: Any,
    ) -> bool:
        if msg_type == MessageType.RESULT:
            queue = _PENDING_REPLIES.get(target_id)
            if queue is not None:
                await queue.put(payload)
                return True
            # Not for a chat turn — an ordinary actor-to-actor result.
        return await original_send(target_id, msg_type, payload, **kw)

    target.send = _capture_send
    target._io_gateway_capture_installed = True


#: Where a reply keeps its words, most likely first. `result` leads because that
#: is the field the prompts tell a generated agent to fill -- "for agents that
#: return plain text, use {"result": ...}" -- and what every other reader in the
#: tree looks for before anything else.
_REPLY_FIELDS = ("result", "reply", "text", "message", "content")


def reply_text(payload: Any) -> str:
    """The words in an agent's reply, whatever shape the agent chose.

    One function for both ways a reply arrives, because they had drifted apart:
    an in-process agent was read `reply` first and one answering from a node was
    read `result` first. Nothing carried two of these fields, so nothing was
    visibly wrong -- but the same agent moved onto a node would have started
    rendering differently, with no way to see why.

    A payload with none of them is returned as it is, which reaches a person as
    a repr. That is deliberate: it is ugly enough to get reported, where a
    prettier rendering would hide an agent that never learned to answer.
    """
    if isinstance(payload, dict):
        for field in _REPLY_FIELDS:
            value = payload.get(field)
            if value:
                return str(value)
    return str(payload)


def _takes_attachments(fn: Callable[..., Any]) -> bool:
    """Whether `fn` accepts an `attachments` argument.

    The dispatch below reaches four differently-shaped entry points, and only
    the LLM ones grew a parameter for this: the Gmail, Calendar and Home
    Assistant agents override `chat` with routing of their own, and a dynamic
    agent's `chat` takes `(prompt, system)` entirely. Asking is what keeps a
    file from becoming a TypeError on an agent that never wanted one.
    """
    try:
        return "attachments" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def route_chat(
    content: str,
    reply_fn,
    stream_fn=None,
    stream_end_fn=None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Core chat routing — slash commands, @mentions, or the orchestrator's stream.

    reply_fn(text)        — send a complete message (slash commands, errors)
    stream_fn(chunk)      — send one streaming chunk (optional; falls back to reply_fn)
    stream_end_fn()       — signal that streaming is done (optional)
    attachments           — stored records for this turn, resolved by the caller

    The records are read into content blocks here rather than by the agent: the
    files are the web layer's, and only this side knows how to reach them. Every
    route that cannot carry them says so instead of answering as though the user
    attached nothing.
    """
    _chunk_fn = stream_fn or reply_fn
    _end_fn = stream_end_fn or no_op_async
    blocks = to_blocks(attachments, uploads.read_bytes) if attachments else []

    async def _say_files_not_sent(why: str) -> None:
        names = ", ".join(str(a.get("name") or "attachment") for a in attachments or [])
        await _chunk_fn(f"[note] {names} not sent — {why}.")

    if content.startswith("/"):
        if blocks:
            await _say_files_not_sent("a command does not take attachments")
        handled = await handle_slash(content, reply_fn)
        if not handled:
            main_actor = find_main_actor(runtime.registry)
            # Forward unrecognized slash commands to main actor.
            # main_actor.process_user_input handles the full command set
            # (/help, /plans, /delete, /stop, /memory, /rules, /topics, etc.)
            if main_actor:
                _chunk_fn = stream_fn or reply_fn
                async for chunk in main_actor.process_user_input_stream(content):
                    if isinstance(chunk, dict):
                        continue
                    await _chunk_fn(str(chunk))
                if stream_end_fn:
                    await stream_end_fn()
            else:
                await reply_fn("Unknown command. Type /help for available commands.")
        return

    target_name, text = parse_mention(content)

    target = runtime.registry.find_by_name(target_name) if runtime.registry else None

    if target is None:
        main_actor = find_main_actor(runtime.registry)
        # ── Remote agent fallback ─────────────────────────────────────────────
        # Agent not in local registry — check if it's running on a remote node.
        # If so, route the message via MQTT and stream the reply back.
        if main_actor:
            remote_node = None
            for node_name, nd in main_actor._known_nodes.items():
                if time.time() - nd.get("last_seen", 0) < 30 and target_name in nd.get(
                    "agents", []
                ):
                    remote_node = node_name
                    break

            if remote_node:
                if blocks:
                    # The files are on this disk; a remote node cannot read them,
                    # and shipping them means megabytes of base64 through the
                    # broker and into its outbox. The broker is the wrong pipe.
                    await _say_files_not_sent(f"@{target_name} runs on {remote_node}")
                reply_topic = f"main/reply/io-gateway/{uuid.uuid4().hex[:8]}"
                payload = {
                    "text": text,
                    "payload": text,
                    "_reply_topic": reply_topic,
                    "_remote_task": True,
                }
                try:
                    async with mqtt_client(
                        main_actor._mqtt_broker,
                        main_actor._mqtt_port,
                    ) as client:
                        # Subscribe first, then publish — avoids race condition
                        await client.subscribe(reply_topic)
                        await main_actor._mqtt_publish(
                            f"agents/by-name/{target_name}/task",
                            payload,
                        )
                        logger.info(
                            "[io-gateway] Routed @%s → %s via MQTT", target_name, remote_node
                        )
                        try:

                            async def _get_reply():
                                async for msg in client.messages:
                                    try:
                                        data = json.loads(msg.payload.decode())
                                        text_out = reply_text(data)
                                    except Exception:
                                        text_out = msg.payload.decode()
                                    return str(text_out)
                                return None

                            text_out = await asyncio.wait_for(_get_reply(), timeout=150.0)
                            await reply_fn(text_out)
                            await _end_fn()
                            return
                        except asyncio.TimeoutError:
                            await reply_fn(
                                f"[error] @{target_name} on {remote_node} did not reply within 150s."
                            )
                            await _end_fn()
                            return
                except Exception as exc:
                    logger.exception("[io-gateway] Remote @%s routing failed", target_name)
                    await reply_fn(
                        f"[error] Could not reach @{target_name} on {remote_node}: {exc}"
                    )
                    await _end_fn()
                    return

        await reply_fn(f"Agent @{target_name} not found.")
        return

    # Every path below reaches into the agent directly rather than through its
    # mailbox, so none of the states that suspend the mailbox stop it answering
    # on their own: a paused agent replied as though nothing had happened, and a
    # stopped one kept answering after its message loop had been cancelled.
    #
    # ``==`` not ``is``: ActorState is a str-enum compared by value everywhere
    # else in the codebase, and identity is not safe here — the test suite has
    # wactorz.core.actor loaded under two module identities, so the enum members
    # are distinct objects with equal values.
    _unavailable = {
        ActorState.PAUSED.value: "is paused. Resume it to send messages.",
        ActorState.STOPPED.value: "is stopped. Start it to send messages.",
        ActorState.FAILED.value: "has failed. It should restart shortly.",
    }
    reason = _unavailable.get(getattr(target.state, "value", target.state))
    if reason is not None:
        await reply_fn(f"@{target.name} {reason}")
        await _end_fn()
        return

    logger.info("[io-gateway] → %s: %r", target.name, text[:60])

    # First user message to an experimental/beta agent gets a one-time warning
    # banner, emitted through the same channel the reply will use.
    banner = experimental_first_use_banner(target_name)
    if banner:
        await _chunk_fn(banner)

    gen_fn = getattr(target, "process_user_input_stream", None) or getattr(
        target, "chat_stream", None
    )
    if gen_fn:
        if blocks and not _takes_attachments(gen_fn):
            await _say_files_not_sent(f"@{target.name} cannot read attachments")
        kwargs = {"attachments": blocks} if blocks and _takes_attachments(gen_fn) else {}
        try:
            async for chunk in gen_fn(text, **kwargs):  # pylint: disable=not-callable
                if isinstance(chunk, dict):
                    continue
                await _chunk_fn(str(chunk))
        finally:
            await _end_fn()
    elif hasattr(target, "process_user_input"):
        if blocks:
            await _say_files_not_sent(f"@{target.name} cannot read attachments")
        result = await target.process_user_input(text)  # pyright: ignore[reportAttributeAccessIssue]
        await reply_fn(str(result))
        await _end_fn()
    else:
        # Agents that only speak via handle_task/TASK+RESULT message passing:
        # - catalog-agent (no LLM)
        # - dynamic agents (sinergym-collector, sinergym-optimizer, etc.)
        # - manual-agent (fallback if chat() not present)
        #
        # Strategy: call handle_message() directly and intercept the reply by
        # temporarily monkey-patching target.send() to capture the RESULT
        # payload instead of trying to route it to a non-existent actor ID.

        # manual-agent: prefer its native chat() — it handles plain text well
        if hasattr(target, "chat") and not hasattr(target, "_fn_handle_task"):
            if blocks:
                await _say_files_not_sent(f"@{target.name} cannot read attachments")
            try:
                result = await target.chat(text)  # pyright: ignore[reportAttributeAccessIssue]
                await reply_fn(str(result))
            except Exception as exc:
                logger.exception("[io-gateway] chat() on %s failed", target.name)
                await reply_fn(f"[error] {target.name}: {exc}")
            await _end_fn()
            return

        # All other message-passing agents: intercept send() to capture the
        # RESULT, since it is addressed to a correlation id rather than a real
        # actor. The interceptor is installed once per agent and correlates by
        # that id; it is never swapped back.
        if blocks:
            await _say_files_not_sent(f"@{target.name} cannot read attachments")
        correlation_id = f"io-gateway:{uuid.uuid4().hex[:12]}"
        reply_queue: asyncio.Queue = asyncio.Queue()
        _install_reply_capture(target)
        _PENDING_REPLIES[correlation_id] = reply_queue
        try:
            msg = Message(
                type=MessageType.TASK,
                sender_id=correlation_id,
                reply_to=correlation_id,
                payload={"text": text},
            )
            await target.handle_message(msg)

            payload = await asyncio.wait_for(reply_queue.get(), timeout=150.0)

            text_out = reply_text(payload)
            if (
                isinstance(payload, dict)
                and "agents" in payload
                and isinstance(payload["agents"], list)
            ):
                text_out = format_catalog_agents_response(payload)

            await reply_fn(text_out)

        except asyncio.TimeoutError:
            await reply_fn(f"[error] @{target_name} did not reply within 150s.")
        except Exception as exc:
            logger.exception("[io-gateway] task dispatch to %s failed", target.name)
            await reply_fn(f"[error] {target.name}: {exc}")
        finally:
            _PENDING_REPLIES.pop(correlation_id, None)
            await _end_fn()


# ── REST chat endpoints ────────────────────────────────────────────────────


async def rest_chat_handler(request: web.Request) -> Response:
    """POST /chat — fire-and-forget a message to a named agent."""
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    message = data.get("message", "").strip()
    agent_name = data.get("agent_name", MAIN_ACTOR_NAME)
    if not message:
        return web.json_response({"error": "message required"}, status=400)
    target = runtime.registry.find_by_name(agent_name)
    if target is None:
        return web.json_response({"error": f"agent '{agent_name}' not found"}, status=404)
    # As above: route to the named agent, since route_chat would otherwise
    # default to main when the message carries no @mention.
    routed = message if message.startswith(("@", "/")) else f"@{target.name} {message}"
    track_chat_task(asyncio.create_task(route_chat(routed, discard_reply)))
    return web.json_response({"status": "sent", "agent": agent_name})


async def rest_chat_stop_handler(request: web.Request | None) -> Response:
    """POST /chat/stop — cancel any in-flight generation. No request body needed.

    Cancels the in-process generation task(s); the cancelled stream finalizes and
    posts "⏹ Stopped." over the WebSocket. The user-facing confirmation rides the
    usual chat reply path, so the UI needs no extra subscription.
    """
    tasks = [t for t in inflight_chat_tasks if not t.done()]
    for t in tasks:
        t.cancel()

    return web.json_response(
        {
            "status": "stopped",
            "cancelled": len(tasks),
        }
    )
