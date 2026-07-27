"""Chat routing for the monitor.

Decides where a user message goes — slash command, @mention, the main actor's
LLM, or a plain ``handle_message`` agent — and exposes the REST chat endpoints
plus the in-flight task tracker that ``POST /chat/stop`` cancels through.
"""

import asyncio
import json
import logging
import socket
import time
import uuid
from typing import Any, cast

from aiohttp import web
from aiohttp.web import Response

from ..agents.main_actor import MainActor
from ..core.actor import Message, MessageType
from ..core.mqtt import mqtt_client
from . import runtime

logger = logging.getLogger(__name__)

# In-flight chat-generation tasks (direct_ws + REST paths) so POST /chat/stop can
# cancel a turn mid-stream. The legacy MQTT path is handled separately by the
# IOAgent via the io/chat/control topic.
inflight_chat_tasks: set = set()

MAIN_ACTOR_NAME = "main"


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


def chat_mode() -> str:
    """Which chat path is active: ``direct_ws`` when a registry is wired, else ``mqtt``."""
    return "direct_ws" if runtime.registry is not None else "mqtt"


def find_main() -> MainActor | None:
    """Return the main actor from the registry, or ``None`` in legacy MQTT mode."""
    actor = runtime.registry.find_by_name(MAIN_ACTOR_NAME) if runtime.registry else None
    if actor:
        return cast(MainActor, actor)
    return None


def parse_mention(content: str) -> tuple[str, str]:
    """Split a leading ``@agent`` mention off a message.

    Returns ``(target, remaining_text)``; target is ``""`` when unmentioned.
    """
    if content.startswith("@"):
        parts = content[1:].split(None, 1)
        return parts[0], (parts[1].strip() if len(parts) > 1 else "")
    return MAIN_ACTOR_NAME, content


# ── Catalog / experimental-agent presentation ──────────────────────────────


def catalog_agent_line(agent: dict) -> str:
    name = agent.get("name", "unknown")
    description = agent.get("description", "")
    return f"- `{name}` - {description}" if description else f"- `{name}`"


def format_catalog_agents_response(payload: dict) -> str:
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
    main = find_main()
    manifest = (getattr(main, "_agent_manifests", {}) or {}).get(agent_name) if main else None
    if not manifest or not manifest.get("experimental"):
        return None
    beta_warned_agents.add(agent_name)
    from ..agents.catalog_agent import BETA_WARNING

    warning = manifest.get("warning") or BETA_WARNING
    return f"⚠️ **{agent_name}** is an experimental/beta agent. {warning}\n\n"


# ── Slash commands ─────────────────────────────────────────────────────────
# Every handler receives a `reply_fn` coroutine — callers supply either an
# MQTT publisher or a WebSocket sender.  No global state, no monkey-patching.


async def slash_deploy(node: str, host: str, user: str, pw: str, broker: str, reply_fn) -> None:
    """Install and start a remote runner on ``host`` over SSH, streaming progress."""
    if not host:
        await reply_fn(f"[discover] Searching for '{node}' on the network...")
        discovered = None
        for candidate in [f"{node}.local", "raspberrypi.local", f"{node.replace('-', '')}.local"]:
            try:
                ip = await asyncio.get_event_loop().run_in_executor(
                    None, socket.gethostbyname, candidate
                )
                discovered = ip
                await reply_fn(f"[discover] Found via mDNS: {candidate} → {ip}")
                break
            except socket.gaierror:
                pass

        if not discovered:
            try:
                local_ip = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: socket.gethostbyname(socket.gethostname())
                )
                subnet = ".".join(local_ip.split(".")[:3])
            except Exception:
                subnet = "192.168.1"
            await reply_fn(f"[discover] mDNS not found. Scanning {subnet}.1-254 for SSH...")
            found = await _scan_subnet_ssh(subnet)
            if found:
                hosts = "\n".join(f"  {ip}" for ip in found)
                await reply_fn(
                    f"[discover] Found {len(found)} host(s):\n{hosts}\n\n"
                    f"Re-run with:\n  /deploy {node} <host> <user> <password> [broker]"
                )
            else:
                await reply_fn(
                    f"[discover] No SSH hosts found.\n"
                    f"  /deploy {node} <host> <user> <password> [broker]"
                )
        else:
            await reply_fn(
                f"[discover] Host: {discovered}\n"
                f"Re-run with credentials:\n"
                f"  /deploy {node} {discovered} <user> <password> [broker]"
            )
        return

    if not user or not pw:
        await reply_fn(
            f"[deploy] Need SSH credentials:\n  /deploy {node} {host} <user> <password> [broker]"
        )
        return

    main_actor = find_main()
    if main_actor is None or not hasattr(main_actor, "delegate_to_installer"):
        await reply_fn("[error] Installer agent not available.")
        return

    broker = broker or "localhost"
    await reply_fn(f"[deploy] Deploying to {user}@{host} as '{node}'... (20-60s)")
    result = await main_actor.delegate_to_installer(
        {
            "action": "node_deploy",
            "host": host,
            "user": user,
            "password": pw,
            "node_name": node,
            "broker": broker,
        },
        timeout=120.0,
    )

    if result.get("success"):
        await reply_fn(f"[OK] Node '{node}' is live!\n  \"spawn a CPU monitor agent on {node}\"")
    else:
        await reply_fn(f"[FAIL] {result.get('error', result)}")


async def _scan_subnet_ssh(subnet: str) -> list:
    found = []
    sem = asyncio.Semaphore(60)

    async def probe(ip):
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, 22), timeout=0.4)
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                found.append(ip)
            except Exception:
                pass

    await asyncio.gather(*[probe(f"{subnet}.{i}") for i in range(1, 255)])
    return sorted(found, key=lambda x: int(x.split(".")[-1]))


async def handle_slash(text: str, reply_fn) -> bool:
    """Dispatch a slash command. Returns True if recognised.
    `reply_fn` is an async callable that sends a string back to the user.
    """
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/clear-plans":
        main_actor = find_main()
        if main_actor and hasattr(main_actor, "persist"):
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
        main_actor = find_main()
        remote_nodes = (
            main_actor.list_nodes() if (main_actor and hasattr(main_actor, "list_nodes")) else []
        )
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
        main_actor = find_main()
        if main_actor is None or not hasattr(main_actor, "migrate_agent"):
            await reply_fn("[error] migrate_agent not available.")
            return True
        await reply_fn(f"[migrating] @{parts[1]} → {parts[2]}...")
        result = await main_actor.migrate_agent(parts[1], parts[2])
        sym = "OK" if result.get("success") else "FAIL"
        await reply_fn(f"[{sym}] {result.get('message', str(result))}")
        return True

    if cmd == "/deploy":
        if len(parts) < 2:
            await reply_fn("[usage] /deploy <node-name> [host [user [password [broker]]]]")
            return True
        await slash_deploy(
            node=parts[1],
            host=parts[2] if len(parts) > 2 else "",
            user=parts[3] if len(parts) > 3 else "",
            pw=parts[4] if len(parts) > 4 else "",
            broker=parts[5] if len(parts) > 5 else "",
            reply_fn=reply_fn,
        )
        return True

    return False


async def route_chat(content: str, reply_fn, stream_fn=None, stream_end_fn=None):
    """Core chat routing — slash commands, @mentions, or main-actor stream.

    reply_fn(text)        — send a complete message (slash commands, errors)
    stream_fn(chunk)      — send one streaming chunk (optional; falls back to reply_fn)
    stream_end_fn()       — signal that streaming is done (optional)
    """
    _chunk_fn = stream_fn or reply_fn
    _end_fn = stream_end_fn or no_op_async

    if content.startswith("/"):
        handled = await handle_slash(content, reply_fn)
        if not handled:
            main_actor = find_main()
            # Forward unrecognized slash commands to main actor.
            # main_actor.process_user_input handles the full command set
            # (/help, /plans, /delete, /stop, /memory, /rules, /topics, etc.)
            if main_actor and hasattr(main_actor, "process_user_input_stream"):
                _chunk_fn = stream_fn or reply_fn
                async for chunk in main_actor.process_user_input_stream(content):
                    if isinstance(chunk, dict):
                        continue
                    await _chunk_fn(str(chunk))
                if stream_end_fn:
                    await stream_end_fn()
            elif main_actor and hasattr(main_actor, "process_user_input"):
                result = await main_actor.process_user_input(content)
                await reply_fn(str(result))
                if stream_end_fn:
                    await stream_end_fn()
            else:
                await reply_fn("Unknown command. Type /help for available commands.")
        return

    target_name, text = parse_mention(content)

    target = runtime.registry.find_by_name(target_name) if runtime.registry else None

    if target is None:
        main_actor = find_main()
        # ── Remote agent fallback ─────────────────────────────────────────────
        # Agent not in local registry — check if it's running on a remote node.
        # If so, route the message via MQTT and stream the reply back.
        if main_actor and hasattr(main_actor, "_known_nodes"):
            remote_node = None
            for node_name, nd in main_actor._known_nodes.items():
                if time.time() - nd.get("last_seen", 0) < 30 and target_name in nd.get(
                    "agents", []
                ):
                    remote_node = node_name
                    break

            if remote_node:
                reply_topic = f"main/reply/io-gateway/{uuid.uuid4().hex[:8]}"
                payload = {
                    "text": text,
                    "payload": text,
                    "_reply_topic": reply_topic,
                    "_remote_task": True,
                }
                try:
                    async with mqtt_client(
                        getattr(main_actor, "_mqtt_broker", "localhost"),
                        getattr(main_actor, "_mqtt_port", 1883),
                    ) as client:
                        # Subscribe first, then publish — avoids race condition
                        await client.subscribe(reply_topic)
                        await main_actor._mqtt_publish(
                            f"agents/by-name/{target_name}/task",
                            payload,
                        )
                        msg = f"[io-gateway] Routed @{target_name} → {remote_node} via MQTT"
                        logger.info(msg)
                        try:

                            async def _get_reply():
                                async for msg in client.messages:
                                    try:
                                        data = json.loads(msg.payload.decode())
                                        text_out = (
                                            data.get("result")
                                            or data.get("reply")
                                            or data.get("text")
                                            or data.get("message")
                                            or data.get("content")
                                            or str(data)
                                        )
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
                    msg = f"[io-gateway] Remote @{target_name} routing failed: {exc}"
                    logger.error(msg, exc_info=True)
                    await reply_fn(
                        f"[error] Could not reach @{target_name} on {remote_node}: {exc}"
                    )
                    await _end_fn()
                    return

        await reply_fn(f"Agent @{target_name} not found.")
        return

    msg = f"[io-gateway] → {target.name}: {text[:60]!r}"
    logger.info(msg)

    # First user message to an experimental/beta agent gets a one-time warning
    # banner, emitted through the same channel the reply will use.
    banner = experimental_first_use_banner(target_name)
    if banner:
        await _chunk_fn(banner)

    gen_fn = getattr(target, "process_user_input_stream", None) or getattr(
        target, "chat_stream", None
    )
    if gen_fn:
        try:
            async for chunk in gen_fn(text):  # pylint: disable=not-callable
                if isinstance(chunk, dict):
                    continue
                await _chunk_fn(str(chunk))
        finally:
            await _end_fn()
    elif hasattr(target, "process_user_input"):
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
            try:
                result = await target.chat(text)  # pyright: ignore[reportAttributeAccessIssue]
                await reply_fn(str(result))
            except Exception as exc:
                msg = f"[io-gateway] chat() on {target.name} failed: {exc}"
                logger.error(msg, exc_info=True)
                await reply_fn(f"[error] {target.name}: {exc}")
            await _end_fn()
            return

        # All other message-passing agents: intercept send() to capture RESULT
        reply_queue = asyncio.Queue()
        original_send = target.send  # save so we can restore

        async def _capture_send(
            target_id: str,
            msg_type: MessageType,
            payload: Any = None,
            **kw: Any,
        ) -> bool:
            if msg_type == MessageType.RESULT:
                await reply_queue.put(payload)
                return True
            return await original_send(target_id, msg_type, payload, **kw)

        target.send = _capture_send
        try:
            msg = Message(
                type=MessageType.TASK,
                sender_id="io-gateway",
                reply_to="io-gateway",
                payload={"text": text},
            )
            await target.handle_message(msg)

            payload = await asyncio.wait_for(reply_queue.get(), timeout=150.0)

            if isinstance(payload, dict):
                text_out = (
                    payload.get("reply")
                    or payload.get("message")
                    or payload.get("text")
                    or payload.get("content")
                    or payload.get("result")
                    or str(payload)
                )
                if "agents" in payload and isinstance(payload["agents"], list):
                    text_out = format_catalog_agents_response(payload)
            else:
                text_out = str(payload)

            await reply_fn(text_out)

        except asyncio.TimeoutError:
            await reply_fn(f"[error] @{target_name} did not reply within 150s.")
        except Exception as exc:
            msg = f"[io-gateway] task dispatch to {target.name} failed: {exc}"
            logger.error(msg, exc_info=True)
            await reply_fn(f"[error] {target.name}: {exc}")
        finally:
            target.send = original_send  # always restore
            await _end_fn()


# ── MQTT chat handler (legacy / IOAgent-less fallback) ─────────────────────


async def handle_chat_mqtt(data: dict):
    """Called when io/chat arrives via MQTT and registry is wired in."""
    if runtime.registry is None:
        return  # IOAgent handles it
    content = (data.get("content") or "").strip()
    if not content:
        return

    async def mqtt_reply(text: str):
        if runtime.mqtt_client_ref:
            await runtime.mqtt_client_ref.publish(
                f"agents/{runtime.IO_GATEWAY_ID}/chat",
                json.dumps(
                    {
                        "from": runtime.IO_GATEWAY_ID,
                        "to": "user",
                        "content": text,
                        "timestamp": time.time(),
                    }
                ),
            )

    await route_chat(content, mqtt_reply)  # MQTT path: no streaming, reply_fn used for all output


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


async def rest_chat_stop_handler(request: web.Request) -> Response:
    """POST /chat/stop — cancel any in-flight generation. No request body needed.

    Works in both runtime modes:
      - direct_ws — cancels the in-process generation task(s) running here; the
        cancelled stream finalizes and posts "⏹ Stopped." over the WebSocket.
      - mqtt (legacy) — publishes {"action": "stop"} to io/chat/control so the
        IOAgent cancels the turn it is streaming and replies on io/chat/response.
    The user-facing confirmation rides the usual chat reply path, so the UI
    needs no extra subscription.
    """
    # direct_ws: cancel the in-process generation task(s).
    tasks = [t for t in inflight_chat_tasks if not t.done()]
    for t in tasks:
        t.cancel()

    # legacy MQTT: tell the IOAgent to stop whatever it is generating.
    published = False
    if runtime.registry is None and runtime.mqtt_client_ref:
        try:
            await runtime.mqtt_client_ref.publish(
                "io/chat/control",
                json.dumps({"action": "stop"}),
                qos=1,
            )
            published = True
        except Exception as exc:
            logger.warning("[chat/stop] io/chat/control publish failed: %s", exc)

    return web.json_response(
        {
            "status": "stopped",
            "cancelled": len(tasks),
            "published": published,
        }
    )
