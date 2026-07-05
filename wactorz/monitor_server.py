"""Wactorz Monitor — WebSocket dashboard + optional MQTT bridge.

Chat routing modes (set via registry wiring in cli.py):
  direct_ws  — registry is set; chat goes straight to actors over WebSocket.
               No IOAgent, no MQTT round-trip for user messages.
  mqtt       — registry is None; chat goes through IOAgent via MQTT (legacy).

The mode is advertised to the browser on connect via a {"type":"config"} frame
so the frontend knows whether to send chat over /ws or publish to io/chat.
"""

import asyncio
import secrets
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Force UTF-8 on the real Windows console only. Skip when stdio has been
    # replaced (pytest capture, test runners, etc.) since re-wrapping a
    # capture stream breaks the harness on Python 3.13.
    _need_wrap = (
        (getattr(sys.stdout, "encoding", "") or "").lower() != "utf-8"
        and hasattr(sys.stdout, "buffer")
        and hasattr(sys.stderr, "buffer")
    )
    if _need_wrap:
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json
import logging
import socket
import time
from pathlib import Path

from .core.mqtt import mqtt_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_WS_PORT = 9001
WS_PORT = 8888
MQTT_TOPICS = ["agents/#", "system/#", "nodes/#", "io/chat"]

# Injected by cli.py after the actor system is built.
# None  → legacy MQTT/IOAgent mode
# <registry> → direct mode (Option B)
registry = None

# Injected by cli.py — used to query historical cost data for deleted agents.
db = None

IO_GATEWAY_ID = "io-gateway"

state = {
    "agents": {},
    "nodes": {},
    "alerts": [],
    "system_health": {},
    "log_feed": [],
}

ws_clients: set = set()
mqtt_client_ref = None
# In-flight chat-generation tasks (direct_ws + REST paths) so POST /chat/stop can
# cancel a turn mid-stream. The legacy MQTT path is handled separately by the
# IOAgent via the io/chat/control topic.
_inflight_chat_tasks: set = set()


def _track_chat_task(task):
    """Register an in-flight chat-generation task so /chat/stop can cancel it."""
    _inflight_chat_tasks.add(task)
    task.add_done_callback(_inflight_chat_tasks.discard)
    return task


# IDs that have been explicitly deleted — block re-admission from stale heartbeats.
# Bounded so a long-running monitor doesn't leak memory across many deletions.
# Stored as a list of (agent_id, deleted_at_ts) tuples so we can re-admit on
# a NEWER status event (which is what a deliberate respawn produces) while
# still ignoring stale retained messages from the deleted instance.
_deleted_agent_ids: list = []
_DELETED_IDS_MAX = 1024
_hard_resetting = False


def _mark_deleted(agent_id: str) -> None:
    """Add an agent_id to the deleted list with FIFO eviction. If already
    present, refresh its deleted-at timestamp so any in-flight retained
    messages from the previous incarnation stay blocked.
    """
    _undelete(agent_id)  # remove any prior entry so the new timestamp wins
    _deleted_agent_ids.append((agent_id, time.time()))
    if len(_deleted_agent_ids) > _DELETED_IDS_MAX:
        del _deleted_agent_ids[0 : len(_deleted_agent_ids) - _DELETED_IDS_MAX]


def _is_deleted(agent_id: str, newer_than: float = 0.0) -> bool:
    """Was this agent_id deleted? When newer_than is given, return False if
    the caller has evidence (a message timestamp) that's strictly later than
    the deletion — that means the agent was respawned and we should re-admit
    it on the next update_agent() call. The actual un-delete happens there;
    this function stays a pure query.
    """
    for aid, ts in _deleted_agent_ids:
        if aid == agent_id:
            return not newer_than > ts
    return False


def _undelete(agent_id: str) -> bool:
    """Remove agent_id from the deleted list. Returns True if it was there."""
    global _deleted_agent_ids
    before = len(_deleted_agent_ids)
    _deleted_agent_ids = [(a, t) for (a, t) in _deleted_agent_ids if a != agent_id]
    return len(_deleted_agent_ids) < before


async def _purge_agent_retained(agent_id: str) -> None:
    """Clear retained MQTT messages for a deleted agent so the broker stops
    re-delivering them after a monitor reconnect or a fresh subscribe.

    Without this, every reconnect re-fires the agent's retained status /
    heartbeat / metrics, each of which would otherwise crash parse_topic
    with KeyError on an entry that update_agent now refuses to recreate.
    """
    if not mqtt_client_ref:
        return
    for metric in (
        "status",
        "heartbeat",
        "metrics",
        "logs",
        "spawned",
        "manifest",
        "errors",
        "detections",
        "results",
        "completed",
    ):
        topic = f"agents/{agent_id}/{metric}"
        try:
            await mqtt_client_ref.publish(topic, b"", retain=True)
        except Exception as e:
            logger.debug(f"[purge] Failed to clear retained {topic}: {e}")


async def _purge_node_desired_state(node: str) -> None:
    """Clear the retained nodes/{node}/desired_state message.

    The remote runner subscribes to this topic and, on reconnect/reboot,
    reconciles it by spawning any listed agent that isn't already running.
    If a reset doesn't clear it, the runner re-spawns the just-deleted agents.
    """
    if not mqtt_client_ref or not node:
        return
    topic = f"nodes/{node}/desired_state"
    try:
        await mqtt_client_ref.publish(topic, b"", retain=True)
    except Exception as e:
        logger.debug(f"[purge] Failed to clear retained {topic}: {e}")


async def _purge_spawn_reconcile(agent: str | None = None) -> None:
    """Tear down the agent-respawn state behind a spawn-registry clear.

    Shared by the "spawns" and "all" reset scopes so both behave identically:
      1. clear the kv-backed registry through the live main actor — an uncached
         recall("_spawned_agents") then returns empty and a later checkpoint
         can't rewrite the cleared set back to disk; and
      2. fix the retained nodes/{node}/desired_state so a reconnecting runner
         can't reconcile cleared agents back.

    For a global clear (agent is None) every affected node's desired_state is
    blanked. For a single-agent clear it is REPUBLISHED from the reduced registry
    so sibling agents still on that node survive.

    Must run BEFORE reset_spawns()/reset_all() wipe the kv on disk — it reads the
    registry to learn which nodes are affected.
    """
    main_ref = registry.find_by_name("main") if registry is not None else None
    reg = {}
    if main_ref is not None and hasattr(main_ref, "_get_spawn_registry"):
        reg = main_ref._get_spawn_registry() or {}

    # Affected nodes: live heartbeats ∪ registry (covers offline nodes).
    node_names: set[str] = set(state["nodes"].keys())
    for name, cfg in reg.items():
        if agent and name != agent:
            continue
        n = (cfg.get("node") or "").strip()
        if n:
            node_names.add(n)

    # Clear the live registry through the actor's own persistence.
    if (
        main_ref is not None
        and hasattr(main_ref, "recall")
        and main_ref.recall("_spawned_agents", None) is not None
    ):
        kept = {k: v for k, v in reg.items() if k != agent} if agent else {}
        main_ref.persist("_spawned_agents", kept)

    if agent and main_ref is not None and hasattr(main_ref, "_update_node_desired_state"):
        # Republish from the reduced registry so siblings on the node survive.
        await asyncio.gather(
            *[main_ref._update_node_desired_state(n) for n in node_names],
            return_exceptions=True,
        )
    else:
        await asyncio.gather(
            *[_purge_node_desired_state(n) for n in node_names],
            return_exceptions=True,
        )


async def _delete_agent(agent_id: str) -> str:
    """Delete an agent properly regardless of whether it lives locally on this
    process or on a remote node. Returns a short status string for logs.

    Strategy:
      1. Mark the actor_id deleted and pop the dashboard entry.
      2. Try to route through main.delete_spawned_agent(name) — it owns the
         spawn registry, knows the agent's node, updates desired_state, sends
         the right MQTT stop signal, and clears the manifest. This is the
         canonical path; it handles local + remote uniformly.
      3. Fall back to direct MQTT if the registry isn't available (the monitor
         is running in a separate process / MQTT-only mode):
           - Remote → publish to nodes/<node>/stop using the node we captured
             from heartbeats.
           - Local → publish to agents/<id>/commands {"command": "stop"}.
      4. Clear retained messages so old heartbeats don't come back on reconnect.
    """
    record = state["agents"].get(agent_id) or {}
    name = record.get("name") or agent_id
    node = (record.get("node") or "").strip()

    _mark_deleted(agent_id)
    state["agents"].pop(agent_id, None)

    routed = "unknown"

    if registry is not None:
        # In-process: delegate to main, which owns the spawn registry and
        # knows exactly how to clean up both local and remote agents.
        main = registry.find_by_name("main")
        if main is not None and hasattr(main, "delete_spawned_agent"):
            try:
                await main.delete_spawned_agent(name)
                routed = f"via main.delete_spawned_agent({name!r})"
            except Exception as e:
                logger.warning(
                    f"[delete] main.delete_spawned_agent('{name}') failed: {e}; "
                    f"falling back to direct MQTT"
                )
                routed = "main path failed"

        # If main wasn't reachable or the call failed, also try to stop a
        # purely local actor through the registry directly. Useful for agents
        # that exist in the registry but aren't in main's spawn registry yet
        # (race window during startup).
        if routed.startswith("via main") is False:
            actor = registry.get(agent_id) or registry.find_by_name(name)
            if actor is not None and not getattr(actor, "protected", False):
                asyncio.create_task(actor.stop())
                routed = "via local registry"

    if routed in ("unknown", "main path failed") and mqtt_client_ref:
        # MQTT-only mode (or main unavailable). Route by node if we have one.
        if node:
            try:
                await mqtt_client_ref.publish(
                    f"nodes/{node}/stop",
                    json.dumps({"name": name}),
                )
                routed = f"via nodes/{node}/stop"
            except Exception as e:
                logger.warning(f"[delete] nodes/{node}/stop publish failed: {e}")
        else:
            try:
                await mqtt_client_ref.publish(
                    f"agents/{agent_id}/commands",
                    json.dumps({"command": "stop", "sender": "monitor", "timestamp": time.time()}),
                )
                routed = f"via agents/{agent_id}/commands"
            except Exception as e:
                logger.warning(f"[delete] commands publish failed: {e}")

    # Always purge retained — even when main handled the delete, we want the
    # dashboard's view to clear immediately rather than wait for tombstones.
    asyncio.create_task(_purge_agent_retained(agent_id))

    logger.info(f"[delete] '{name}' (id={agent_id[:8]}, node={node or 'local'}) {routed}")
    return routed


# ── helpers ────────────────────────────────────────────────────────────────


def _chat_mode() -> str:
    return "direct_ws" if registry is not None else "mqtt"


def _find_main():
    return registry.find_by_name("main") if registry else None


def _parse_mention(content: str) -> tuple[str, str]:
    if content.startswith("@"):
        parts = content[1:].split(None, 1)
        return parts[0], (parts[1].strip() if len(parts) > 1 else "")
    return "main", content


def update_agent(agent_id: str, key: str, data):
    if _hard_resetting or _is_deleted(agent_id):
        return
    if agent_id not in state["agents"]:
        state["agents"][agent_id] = {
            "agent_id": agent_id,
            "name": agent_id[:8],
            "first_seen": time.time(),
        }
    state["agents"][agent_id][key] = data
    state["agents"][agent_id]["last_update"] = time.time()


def add_log(entry: dict):
    state["log_feed"].insert(0, entry)
    if len(state["log_feed"]) > 100:
        state["log_feed"].pop()


async def broadcast(msg: dict):
    if not ws_clients:
        return
    payload = json.dumps(msg)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_str(payload)
        except Exception as e:
            logger.warning(f"[broadcast] WS send failed: {e}")
            dead.add(ws)
    ws_clients.difference_update(dead)


# ── slash commands ─────────────────────────────────────────────────────────
# Every handler receives a `reply_fn` coroutine — callers supply either an
# MQTT publisher or a WebSocket sender.  No global state, no monkey-patching.


async def _slash_deploy(node: str, host: str, user: str, pw: str, broker: str, reply_fn):
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

    main = _find_main()
    if main is None or not hasattr(main, "delegate_to_installer"):
        await reply_fn("[error] Installer agent not available.")
        return

    broker = broker or "localhost"
    await reply_fn(f"[deploy] Deploying to {user}@{host} as '{node}'... (20-60s)")
    result = await main.delegate_to_installer(
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
        main = _find_main()
        if main and hasattr(main, "persist"):
            main.persist("_plan_cache", {})
        await reply_fn("[System: Plan cache cleared.]")
        return True

    if cmd == "/agents":
        if registry is None:
            await reply_fn("[agents] Registry not available.")
            return True
        lines = []
        for actor in registry.all_actors():
            status = actor.get_status() if hasattr(actor, "get_status") else {}
            st = status.get("state", "?")
            protected = " [protected]" if getattr(actor, "protected", False) else ""
            node = f" [{status['node']}]" if status.get("node") else ""
            lines.append(f"  [{st:8s}] @{actor.name:<22s} {actor.actor_id[:8]}{protected}{node}")
        await reply_fn("Agents:\n" + "\n".join(lines) if lines else "No agents running.")
        return True

    if cmd == "/nodes":
        main = _find_main()
        remote_nodes = main.list_nodes() if (main and hasattr(main, "list_nodes")) else []
        local = [a.name for a in registry.all_actors()] if registry else []
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
        main = _find_main()
        if main is None or not hasattr(main, "migrate_agent"):
            await reply_fn("[error] migrate_agent not available.")
            return True
        await reply_fn(f"[migrating] @{parts[1]} → {parts[2]}...")
        result = await main.migrate_agent(parts[1], parts[2])
        sym = "OK" if result.get("success") else "FAIL"
        await reply_fn(f"[{sym}] {result.get('message', str(result))}")
        return True

    if cmd == "/deploy":
        if len(parts) < 2:
            await reply_fn("[usage] /deploy <node-name> [host [user [password [broker]]]]")
            return True
        await _slash_deploy(
            node=parts[1],
            host=parts[2] if len(parts) > 2 else "",
            user=parts[3] if len(parts) > 3 else "",
            pw=parts[4] if len(parts) > 4 else "",
            broker=parts[5] if len(parts) > 5 else "",
            reply_fn=reply_fn,
        )
        return True

    return False


async def _route_chat(content: str, reply_fn, stream_fn=None, stream_end_fn=None):
    """Core chat routing — slash commands, @mentions, or main-actor stream.

    reply_fn(text)        — send a complete message (slash commands, errors)
    stream_fn(chunk)      — send one streaming chunk (optional; falls back to reply_fn)
    stream_end_fn()       — signal that streaming is done (optional)
    """
    _chunk_fn = stream_fn or reply_fn
    _end_fn = stream_end_fn or (lambda: None)

    if content.startswith("/"):
        handled = await handle_slash(content, reply_fn)
        if not handled:
            # Forward unrecognized slash commands to main actor.
            # main_actor.process_user_input handles the full command set
            # (/help, /plans, /delete, /stop, /memory, /rules, /topics, etc.)
            main = _find_main()
            if main and hasattr(main, "process_user_input_stream"):
                _chunk_fn = stream_fn or reply_fn
                async for chunk in main.process_user_input_stream(content):
                    if isinstance(chunk, dict):
                        continue
                    await _chunk_fn(str(chunk))
                if stream_end_fn:
                    await stream_end_fn()
            elif main and hasattr(main, "process_user_input"):
                result = await main.process_user_input(content)
                await reply_fn(str(result))
                if stream_end_fn:
                    await stream_end_fn()
            else:
                await reply_fn("Unknown command. Type /help for available commands.")
        return

    target_name, text = _parse_mention(content)

    target = registry.find_by_name(target_name) if registry else None

    if target is None:
        # ── Remote agent fallback ─────────────────────────────────────────────
        # Agent not in local registry — check if it's running on a remote node.
        # If so, route the message via MQTT and stream the reply back.
        main = registry.find_by_name("main") if registry else None
        if main and hasattr(main, "_known_nodes"):
            import time as _rt

            remote_node = None
            for node_name, nd in main._known_nodes.items():
                if _rt.time() - nd.get("last_seen", 0) < 30 and target_name in nd.get("agents", []):
                    remote_node = node_name
                    break

            if remote_node:
                import json as _json
                import uuid as _uuid

                reply_topic = f"main/reply/io-gateway/{_uuid.uuid4().hex[:8]}"
                payload = {
                    "text": text,
                    "payload": text,
                    "_reply_topic": reply_topic,
                    "_remote_task": True,
                }
                try:
                    async with mqtt_client(
                        getattr(main, "_mqtt_broker", "localhost"),
                        getattr(main, "_mqtt_port", 1883),
                    ) as client:
                        # Subscribe first, then publish — avoids race condition
                        await client.subscribe(reply_topic)
                        await main._mqtt_publish(
                            f"agents/by-name/{target_name}/task",
                            payload,
                        )
                        logger.info(f"[io-gateway] Routed @{target_name} → {remote_node} via MQTT")
                        try:

                            async def _get_reply():
                                async for msg in client.messages:
                                    try:
                                        data = _json.loads(msg.payload.decode())
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
                    logger.error(
                        f"[io-gateway] Remote @{target_name} routing failed: {exc}", exc_info=True
                    )
                    await reply_fn(
                        f"[error] Could not reach @{target_name} on {remote_node}: {exc}"
                    )
                    await _end_fn()
                    return

        await reply_fn(f"Agent @{target_name} not found.")
        return

    logger.info(f"[io-gateway] → {target.name}: {text[:60]!r}")

    gen_fn = getattr(target, "process_user_input_stream", None) or getattr(
        target, "chat_stream", None
    )
    if gen_fn:
        try:
            async for chunk in gen_fn(text):
                if isinstance(chunk, dict):
                    continue
                await _chunk_fn(str(chunk))
        finally:
            await _end_fn()
    elif hasattr(target, "process_user_input"):
        result = await target.process_user_input(text)
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
        from wactorz.core.actor import Message, MessageType

        # manual-agent: prefer its native chat() — it handles plain text well
        if hasattr(target, "chat") and not hasattr(target, "_fn_handle_task"):
            try:
                result = await target.chat(text)
                await reply_fn(str(result))
            except Exception as exc:
                logger.error(f"[io-gateway] chat() on {target.name} failed: {exc}", exc_info=True)
                await reply_fn(f"[error] {target.name}: {exc}")
            await _end_fn()
            return

        # All other message-passing agents: intercept send() to capture RESULT
        reply_queue = asyncio.Queue()
        original_send = target.send  # save so we can restore

        async def _capture_send(recipient_id, msg_type, payload=None, **kw):
            if msg_type == MessageType.RESULT:
                await reply_queue.put(payload)
            else:
                await original_send(recipient_id, msg_type, payload, **kw)

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
                    lines = [payload.get("message", "Available agents:")]
                    for a in payload["agents"]:
                        lines.append(f"  • {a['name']}: {a.get('description', '')}")
                    text_out = "\n".join(lines)
            else:
                text_out = str(payload)

            await reply_fn(text_out)

        except asyncio.TimeoutError:
            await reply_fn(f"[error] @{target_name} did not reply within 150s.")
        except Exception as exc:
            logger.error(
                f"[io-gateway] task dispatch to {target.name} failed: {exc}", exc_info=True
            )
            await reply_fn(f"[error] {target.name}: {exc}")
        finally:
            target.send = original_send  # always restore
            await _end_fn()


# ── MQTT chat handler (legacy / IOAgent-less fallback) ─────────────────────


async def handle_chat_mqtt(data: dict):
    """Called when io/chat arrives via MQTT and registry is wired in."""
    if registry is None:
        return  # IOAgent handles it
    content = (data.get("content") or "").strip()
    if not content:
        return

    async def mqtt_reply(text: str):
        global mqtt_client_ref
        if mqtt_client_ref:
            await mqtt_client_ref.publish(
                f"agents/{IO_GATEWAY_ID}/chat",
                json.dumps(
                    {
                        "from": IO_GATEWAY_ID,
                        "to": "user",
                        "content": text,
                        "timestamp": time.time(),
                    }
                ),
            )

    await _route_chat(content, mqtt_reply)  # MQTT path: no streaming, reply_fn used for all output


# ── WebSocket handler ──────────────────────────────────────────────────────


async def ws_handler(request):
    from aiohttp import WSMsgType, web

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    logger.info(f"WebSocket client connected. Total: {len(ws_clients)}")

    # Send initial state
    await ws.send_str(json.dumps({"type": "full_snapshot", "state": _snapshot()}))

    # Advertise chat mode so the frontend knows where to send messages
    await ws.send_str(json.dumps({"type": "config", "chat_mode": _chat_mode()}))

    # Per-connection accumulator for streamed assistant replies.
    # We only persist once at stream_end so chat_log gets one row per turn
    # with the full content, not a row per chunk.
    _stream_buffer: list[str] = []

    # The agent the current turn is addressed to. Reply frames and chat_log are
    # attributed to it instead of the generic "io-gateway" transport id, so the
    # UI (and persisted/reloaded history) shows the agent that actually answered
    # rather than the gateway. Set per turn from the user's @mention before
    # routing; defaults to the gateway id until a chat turn arrives.
    _reply_from = {"name": IO_GATEWAY_ID}

    def _persist_chat(role: str, content: str, agent_name: str = "main") -> None:
        """Best-effort write to chat_log. Never raises into the WS path."""
        if db is None or not content:
            return
        try:
            db.write_chat_log(
                ts=time.time(),
                agent_name=agent_name,
                role=role,
                content=content,
            )
        except Exception as exc:
            logger.warning(f"[ws] chat_log write failed: {exc}")

    async def ws_reply(text: str):
        try:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "chat",
                        "from": _reply_from["name"],
                        "content": text,
                        "timestamp": time.time(),
                    }
                )
            )
            # Non-streamed replies (slash command output, errors, system
            # messages) — persist immediately.
            _persist_chat("assistant", text, _reply_from["name"])
        except Exception:
            pass

    async def ws_stream_chunk(chunk: str):
        try:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "stream_chunk",
                        "from": _reply_from["name"],
                        "content": chunk,
                        "timestamp": time.time(),
                    }
                )
            )
            # Buffer for end-of-stream persistence; do NOT write per chunk.
            if chunk:
                _stream_buffer.append(chunk)
        except Exception:
            pass

    async def ws_stream_end():
        try:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "stream_end",
                        "from": _reply_from["name"],
                        "timestamp": time.time(),
                    }
                )
            )
            # Now persist the full assembled assistant turn — once.
            if _stream_buffer:
                full = "".join(_stream_buffer)
                _stream_buffer.clear()
                _persist_chat("assistant", full, _reply_from["name"])
        except Exception:
            # Even if the send_str failed, flush anything we accumulated
            # so the user's session isn't lost on a transient ws hiccup.
            if _stream_buffer:
                full = "".join(_stream_buffer)
                _stream_buffer.clear()
                _persist_chat("assistant", full, _reply_from["name"])

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "command":
                        await handle_command(data)

                    elif msg_type == "chat":
                        content = (data.get("content") or "").strip()
                        if content and registry is not None:
                            # Attribute the whole turn to the agent it addresses
                            # (slash commands and un-mentioned text default to
                            # "main", matching _route_chat's own resolution) so the
                            # reply frames and chat_log group under that agent
                            # instead of the io-gateway transport id.
                            _reply_from["name"] = (
                                "main" if content.startswith("/") else _parse_mention(content)[0]
                            )
                            # Persist the user's turn first so chat_log has the
                            # request even if the assistant reply errors out.
                            _persist_chat("user", content, _reply_from["name"])

                            async def _safe_route(c=content):
                                try:
                                    await _route_chat(
                                        c,
                                        ws_reply,
                                        stream_fn=ws_stream_chunk,
                                        stream_end_fn=ws_stream_end,
                                    )
                                except asyncio.CancelledError:
                                    # Stop button: finalize the partial stream so
                                    # the UI re-enables, then post a confirmation
                                    # matching the IOAgent's wording.
                                    try:
                                        await ws_stream_end()
                                        await ws_reply("⏹ Stopped.")
                                    except Exception:
                                        pass
                                    raise
                                except Exception as exc:
                                    logger.error(f"[ws] chat error: {exc}", exc_info=True)
                                    try:
                                        await ws_reply(f"[error] {exc}")
                                        await ws_stream_end()
                                    except Exception:
                                        pass

                            _track_chat_task(asyncio.create_task(_safe_route()))
                        elif content:
                            # No registry — tell the browser to use MQTT
                            await ws_reply(
                                "[system] Chat not available over WebSocket in this mode."
                            )

                except Exception as e:
                    logger.warning(f"[ws] Bad message: {e}")
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        ws_clients.discard(ws)
        logger.info(f"WebSocket client disconnected. Total: {len(ws_clients)}")
    return ws


# ── MQTT infrastructure ────────────────────────────────────────────────────


async def handle_command(cmd: dict):
    global mqtt_client_ref
    command = cmd.get("command")
    agent_id = cmd.get("agent_id")
    if not command or not agent_id:
        return
    if command not in {"pause", "stop", "resume", "delete"}:
        return

    logger.info(f"[cmd] {command.upper()} -> {agent_id[:8]}")
    if not mqtt_client_ref:
        logger.warning("[cmd] No MQTT client available")
        return

    payload = json.dumps(
        {"command": command, "sender": "monitor-dashboard", "timestamp": time.time()}
    )
    try:
        await mqtt_client_ref.publish(f"agents/{agent_id}/commands", payload)
        add_log(
            {"type": "command", "agent_id": agent_id, "command": command, "timestamp": time.time()}
        )
        if command in ("stop", "pause", "resume"):
            state["agents"].get(agent_id, {})["state"] = (
                "stopped" if command == "stop" else "paused" if command == "pause" else "running"
            )
            await broadcast({"type": "patch", "state": _snapshot()})
        elif command == "delete":
            await _delete_agent(agent_id)
            await broadcast({"type": "delete_agent", "agent_id": agent_id, "state": _snapshot()})
    except Exception as e:
        logger.error(f"[cmd] Publish failed: {e}")


def parse_topic(topic: str, payload_str: str):
    try:
        data = json.loads(payload_str)
    except Exception:
        data = payload_str

    parts = topic.split("/")

    if parts[0] == "system" and len(parts) >= 2:
        if parts[1] == "health":
            state["system_health"] = data
        elif parts[1] == "alerts":
            state["alerts"].insert(0, data)
            if len(state["alerts"]) > 50:
                state["alerts"].pop()
        return {"type": "system", "subtype": parts[1], "data": data}

    if parts[0] == "agents" and len(parts) >= 3:
        agent_id = parts[1]
        metric = parts[2]

        # Re-admit a deleted agent on a FRESH status event. Every actor
        # publishes its first status from on_start(), with uptime ≈ 0; that's
        # the unambiguous "I just started" signal. A stale retained status
        # from the previous (deleted) incarnation would carry the uptime it
        # had at the moment of deletion (typically large), so we don't
        # confuse it with a respawn.
        #
        # Without this, deleting an agent and respawning it under the same
        # name produces the same deterministic actor_id (uuid5 of the name),
        # the deleted guard fires, and the new instance is invisible in the
        # dashboard even though it's actually running.
        if metric == "status" and isinstance(data, dict) and _is_deleted(agent_id):
            uptime = data.get("uptime", 0)
            try:
                uptime = float(uptime)
            except (TypeError, ValueError):
                uptime = 0.0
            agent_state = data.get("state", "")
            if uptime < 10.0 and agent_state not in ("stopped", "failed"):
                _undelete(agent_id)
                logger.info(
                    f"[MQTT] Re-admitting respawned agent {agent_id[:8]} "
                    f"(uptime={uptime:.1f}s, state={agent_state}, previously deleted)"
                )

        # If the agent was just deleted, update_agent() refuses to recreate
        # the entry — so any direct state["agents"][agent_id] access below
        # would KeyError. Skip the whole branch; the agent is gone.
        if _is_deleted(agent_id):
            return {"type": "agent", "subtype": metric, "agent_id": agent_id, "data": data}

        if metric == "status":
            update_agent(agent_id, "status", data)
            if isinstance(data, dict) and agent_id in state["agents"]:
                if "name" in data:
                    state["agents"][agent_id]["name"] = data["name"]
                if "state" in data:
                    state["agents"][agent_id]["state"] = data["state"]
                if "protected" in data:
                    state["agents"][agent_id]["protected"] = data["protected"]
            name = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {
                    "type": "status",
                    "agent_id": agent_id,
                    "name": name,
                    "status": data,
                    "timestamp": time.time(),
                }
            )

        elif metric == "heartbeat":
            update_agent(agent_id, "heartbeat", data)
            if isinstance(data, dict) and agent_id in state["agents"]:
                ag = state["agents"][agent_id]
                ag["name"] = data.get("name", agent_id[:8])
                ag["cpu"] = data.get("cpu", 0)
                ag["mem"] = data.get("memory_mb", 0)
                ag["task"] = data.get("task", "idle")
                ag["state"] = data.get("state", "unknown")
                # Remote agents' heartbeats include "node" — capture it so the
                # dashboard delete path can route the stop to the right runner.
                # Local agents don't set this field; absence means "local".
                if data.get("node"):
                    ag["node"] = data["node"]
            if agent_id in state["agents"]:
                logger.info(
                    f"[MQTT] Heartbeat: {state['agents'][agent_id].get('name', agent_id[:8])}"
                )

        elif metric == "metrics":
            update_agent(agent_id, "metrics", data)
            if isinstance(data, dict) and agent_id in state["agents"]:
                state["agents"][agent_id]["messages_processed"] = data.get("messages_processed", 0)
                if "cost_usd" in data:
                    state["agents"][agent_id]["cost_usd"] = data.get("cost_usd", 0.0)
                    state["agents"][agent_id]["input_tokens"] = data.get("input_tokens", 0)
                    state["agents"][agent_id]["output_tokens"] = data.get("output_tokens", 0)
                    # Bank the spend durably so it survives the agent being
                    # deleted or hard-killed before its on_stop() can persist.
                    _record_lifetime_cost(agent_id, data.get("cost_usd"))

        elif metric == "logs":
            # Log frames carry only the agent id; resolve the friendly name the
            # same way alert/completed do so the feed never shows a bare id.
            # `**data` last lets a payload that already includes a name win.
            name = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {
                    "type": "log",
                    "agent_id": agent_id,
                    "name": name,
                    "timestamp": time.time(),
                    **(data if isinstance(data, dict) else {}),
                }
            )
        elif metric == "spawned":
            # Payload carries child_name/child_id, not name — resolve the (parent)
            # agent's name from state so the feed row isn't attributed to a bare id.
            name = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {
                    "type": "spawned",
                    "agent_id": agent_id,
                    "name": name,
                    "timestamp": time.time(),
                    **(data if isinstance(data, dict) else {}),
                }
            )
        elif metric == "chat":
            # User-facing message pushed by an agent via Actor.notify_user().
            # Forward it to the chat panel as a live chat frame (in addition to
            # the dashboard feed). The frame is carried under "_push_chat";
            # mqtt_listener does the broadcast since parse_topic is synchronous.
            sender = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            content = ""
            if isinstance(data, dict):
                content = (data.get("content") or data.get("text") or "").strip()
                sender = data.get("from") or sender
            elif isinstance(data, str):
                content = data.strip()
            if content:
                add_log(
                    {
                        "type": "chat",
                        "agent_id": agent_id,
                        "from": sender,
                        "content": content,
                        "timestamp": time.time(),
                    }
                )
                return {
                    "type": "agent",
                    "agent_id": agent_id,
                    "metric": "chat",
                    "data": data,
                    "_push_chat": {
                        "type": "chat",
                        "from": sender,
                        "content": content,
                        "timestamp": time.time(),
                    },
                }
            return {"type": "agent", "agent_id": agent_id, "metric": "chat", "data": data}
        elif metric == "completed":
            update_agent(agent_id, "last_completed", data)
            name = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {"type": "completed", "agent_id": agent_id, "name": name, "timestamp": time.time()}
            )
        elif metric == "alert":
            if isinstance(data, dict):
                data["agent_id"] = agent_id
                data.setdefault("name", state["agents"].get(agent_id, {}).get("name", agent_id[:8]))
            state["alerts"].insert(0, data if isinstance(data, dict) else {"agent_id": agent_id})
            if len(state["alerts"]) > 50:
                state["alerts"].pop()
            name = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            severity = data.get("severity", "warning") if isinstance(data, dict) else "warning"
            add_log(
                {
                    "type": "alert",
                    "agent_id": agent_id,
                    "name": name,
                    "message": f"{name} unresponsive ({severity})",
                    "timestamp": time.time(),
                }
            )

        return {"type": "agent", "agent_id": agent_id, "metric": metric, "data": data}

    if parts[0] == "nodes" and len(parts) >= 3 and parts[2] == "heartbeat":
        node_name = parts[1]
        if isinstance(data, dict):
            state["nodes"][node_name] = {
                "node": node_name,
                "agents": data.get("agents", []),
                "last_seen": time.time(),
                "online": True,
                "node_id": data.get("node_id", ""),
            }
            logger.info(f"[MQTT] Node heartbeat: {node_name} | agents: {data.get('agents', [])}")
            return {"type": "node", "node_name": node_name, "data": data}

    return None


def _node_online(last_seen: float) -> bool:
    return (time.time() - last_seen) < 45


# ── Durable lifetime cost ledger ────────────────────────────────────────────
# The headline total cost must never drop when an agent goes away. The per-agent
# _final_cost rows that feed _historical_cost_usd() are purged on permanent
# delete (kv_purge_agent) and are never written on a hard kill — an addon update
# restarts the container, so an in-flight agent's on_stop() never runs. A total
# computed purely from live actors + surviving _final_cost rows therefore leaks
# spend whenever an agent is deleted or killed mid-life.
#
# Every actor publishes its cumulative cost_usd over MQTT on each heartbeat
# (~10s), so the monitor sees the spend long before the agent disappears. We bank
# each actor's highest-reported cost into a monotonic ledger keyed by the stable
# actor_id and persist it under the _system agent in SQLite. Deletion and hard
# kills cannot erase what was already banked; keying by actor_id stops a
# respawned agent from double-counting.
_LIFETIME_LEDGER_KEY = "_lifetime_cost_ledger"
_lifetime_cost: dict = {}
_lifetime_loaded = False


def _ensure_lifetime_loaded() -> None:
    """Lazily hydrate the in-memory ledger from SQLite once db is injected."""
    global _lifetime_loaded
    if _lifetime_loaded or db is None:
        return
    try:
        stored = db.kv_get("_system", _LIFETIME_LEDGER_KEY)
        if isinstance(stored, dict):
            for k, v in stored.items():
                try:
                    _lifetime_cost[k] = float(v)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    _lifetime_loaded = True


def _record_lifetime_cost(agent_id: str, cost_usd) -> None:
    """Bank an agent's reported lifetime cost as a monotonic high-water mark.
    Persisted durably so deletion / hard kills never drop it from the total.

    The high-water mark is deliberately robust to out-of-order / transient-low
    heartbeats (a momentary lower reading never lowers the banked value). The
    trade-off: if a same-named agent is *deleted* (purging its _final_cost) and
    then respawned with the same actor_id, the new life's spend is absorbed into
    the existing high-water rather than added on top — i.e. it can undercount in
    that narrow case. That is preferred over inferring "a new life started" from
    a cost regression, which a stale frame would misread and double-count.
    """
    if not agent_id or cost_usd is None:
        return
    try:
        cost = float(cost_usd)
    except (TypeError, ValueError):
        return
    if cost <= 0:
        return
    _ensure_lifetime_loaded()
    if cost <= _lifetime_cost.get(agent_id, 0.0):
        return
    _lifetime_cost[agent_id] = cost
    if db is not None:
        try:
            db.kv_set("_system", _LIFETIME_LEDGER_KEY, _lifetime_cost)
        except Exception:
            pass


def _lifetime_cost_total() -> float:
    _ensure_lifetime_loaded()
    return sum(_lifetime_cost.values())


def _reset_actor_cost(actor) -> None:
    """Zero a live actor's cost/token counters AND its per-call accrual baseline.

    The baseline must move with total_cost_usd: _persist_cost / _accrue_usage
    accumulate (total_cost_usd - baseline) into the global period and all-time
    counters. Zeroing total_cost_usd on a reset without also zeroing the baseline
    leaves the baseline at the pre-reset total, so every subsequent call yields a
    negative delta and the period/all-time counters stop advancing until spend
    climbs back past the old total — the "limit count stopped counting after a
    wipe" bug.
    """
    if not hasattr(actor, "total_cost_usd"):
        return
    actor.total_cost_usd = 0.0
    actor.total_input_tokens = 0
    actor.total_output_tokens = 0
    if hasattr(actor, "_last_persisted_usd"):
        actor._last_persisted_usd = 0.0
    if hasattr(actor, "_last_period_cost_usd"):
        actor._last_period_cost_usd = 0.0


def _historical_cost_usd(live_names: set) -> float:
    """Sum _final_cost for agents not in live_names."""
    if db is None:
        return 0.0
    try:
        import json as _json

        rows = db.conn.execute("SELECT value FROM kv_store WHERE key = '_final_cost'").fetchall()
        total = 0.0
        for row in rows:
            try:
                entry = _json.loads(row[0])
                if entry.get("name") not in live_names:
                    total += entry.get("cost_usd", 0.0)
            except Exception:
                pass
        return total
    except Exception:
        return 0.0


def _historical_messages(live_names: set) -> int:
    """Sum _messages_processed for agents not in live_names."""
    if db is None:
        return 0
    try:
        import json as _json

        rows = db.conn.execute(
            "SELECT agent, value FROM kv_store WHERE key = '_messages_processed'"
        ).fetchall()
        total = 0
        for agent_name, value in rows:
            if agent_name not in live_names:
                try:
                    entry = _json.loads(value)
                    total += entry.get("count", 0)
                except Exception:
                    pass
        return total
    except Exception:
        return 0


def _snapshot() -> dict:
    if _hard_resetting:
        return {
            "agents": [],
            "nodes": [],
            "alerts": [],
            "log_feed": [],
            "total_cost_usd": 0,
            "total_messages": 0,
        }
    for nd in state["nodes"].values():
        nd["online"] = _node_online(nd.get("last_seen", 0))

    # The headline totals must match what the dashboard actually shows on the
    # cards. Each card resolves its cost via _actor_cost() — MQTT state, then the
    # live actor object, then the persisted _final_cost row — so the header has to
    # coalesce the same three sources per agent. Summing only state["cost_usd"]
    # (or only iterating the local registry) dropped any on-screen agent whose
    # cost lives on the actor object / SQLite rather than in an MQTT metrics frame.
    actors_by_id: dict = {}
    actors_by_name: dict = {}
    if registry is not None:
        for a in registry.all_actors():
            actors_by_id[a.actor_id] = a
            actors_by_name[a.name] = a

    live_names: set = set()
    live_cost = 0.0
    live_msgs = 0
    seen_ids: set = set()
    for aid, ag in state["agents"].items():
        seen_ids.add(aid)
        name = ag.get("name", "")
        live_names.add(name)
        actor = actors_by_id.get(aid) or actors_by_name.get(name)
        live_cost += _best_cost(ag, actor, name)
        live_msgs += _best_msgs(ag, actor)
    # Fold in live actors not yet in state (the post-restart window before the
    # first heartbeat). Keyed by actor_id so agents already counted above are
    # skipped — no double-count.
    for a in actors_by_id.values():
        if a.actor_id in seen_ids:
            continue
        live_names.add(a.name)
        live_cost += _best_cost(None, a, a.name)
        live_msgs += _best_msgs(None, a)

    # The live + _final_cost sum can dip when an agent is deleted (its _final_cost
    # row is purged) or hard-killed (its on_stop never ran). The durable ledger is
    # monotonic, so clamp the headline total up to whichever is larger — spend is
    # never lost, and the live path still covers the fresh-boot window before the
    # first heartbeat repopulates the ledger.
    # The all-time call-time counter is delete-proof (a deleted agent's _final_cost
    # row is purged and its lifetime-ledger high-water can be missed/popped, but
    # the counter accrued its spend at call time). Use it as a third floor so the
    # headline never drops below money already spent — and so it can never read
    # lower than the "this period" spend shown beside it.
    try:
        from .agents.llm_agent import get_global_alltime_cost

        alltime_cost = get_global_alltime_cost()
    except Exception:
        alltime_cost = 0.0
    total_cost = max(
        live_cost + _historical_cost_usd(live_names),
        _lifetime_cost_total(),
        alltime_cost,
    )
    total_msgs = live_msgs + _historical_messages(live_names)
    return {
        "agents": list(state["agents"].values()),
        "nodes": list(state["nodes"].values()),
        "alerts": state["alerts"][:10],
        "log_feed": state["log_feed"][:20],
        "system_health": state["system_health"],
        "total_cost_usd": round(total_cost, 6),
        "total_messages": total_msgs,
    }


async def mqtt_listener():
    global mqtt_client_ref
    logger.info(f"Connecting to MQTT {MQTT_BROKER}:{MQTT_PORT}...")
    try:
        while True:
            try:
                async with mqtt_client(MQTT_BROKER, MQTT_PORT) as client:
                    mqtt_client_ref = client
                    logger.info("MQTT connected.")

                    if registry is not None:
                        await client.publish(
                            f"agents/{IO_GATEWAY_ID}/spawn",
                            json.dumps(
                                {
                                    "agentId": IO_GATEWAY_ID,
                                    "agentName": IO_GATEWAY_ID,
                                    "agentType": "gateway",
                                    "timestamp": time.time(),
                                }
                            ),
                        )

                    for topic in MQTT_TOPICS:
                        await client.subscribe(topic)

                    async for message in client.messages:
                        topic = str(message.topic)
                        payload = message.payload.decode(errors="replace")

                        if topic == "io/chat":
                            if registry is not None:
                                try:
                                    asyncio.create_task(handle_chat_mqtt(json.loads(payload)))
                                except Exception as exc:
                                    logger.error(f"[io/chat] error: {exc}")
                            continue

                        event = parse_topic(topic, payload)
                        if event and not _hard_resetting:
                            metric = event.get("metric", "")
                            log_event = None if metric == "heartbeat" else event
                            await broadcast(
                                {"type": "patch", "event": log_event, "state": _snapshot()}
                            )
                            # Agent-originated user-facing message → push to the
                            # chat panel as a live chat frame, and persist it so
                            # it survives a browser reload like any other turn.
                            push = event.get("_push_chat")
                            if push:
                                await broadcast(push)
                                try:
                                    if db is not None and push.get("content"):
                                        db.write_chat_log(
                                            ts=push.get("timestamp", time.time()),
                                            agent_name=push.get("from", "agent"),
                                            role="assistant",
                                            content=push["content"],
                                        )
                                except Exception as _exc:
                                    logger.debug(f"[chat-bridge] persist failed: {_exc}")

            except Exception as e:
                mqtt_client_ref = None
                logger.warning(f"MQTT error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
    finally:
        # Drop ref and force GC while loop is still open so paho's __del__
        # doesn't fire after the event loop closes (avoids RuntimeError noise).
        import gc

        mqtt_client_ref = None
        gc.collect()


# ── Startup checks ─────────────────────────────────────────────────────────


async def _check_mqtt() -> bool:
    """Return True if MQTT broker is reachable."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(MQTT_BROKER, MQTT_PORT), timeout=3
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.error(f"[startup] MQTT broker {MQTT_BROKER}:{MQTT_PORT} unreachable — {exc}")
        return False


async def _check_ws_port() -> bool:
    """Return True if WS_PORT is free to bind."""
    try:
        server = await asyncio.start_server(lambda r, w: None, "0.0.0.0", WS_PORT)
        server.close()
        await server.wait_closed()
        return True
    except OSError as exc:
        logger.error(f"[startup] Port {WS_PORT} already in use — {exc}")
        return False


# ── Static file serving ────────────────────────────────────────────────────

_pkg = Path(__file__).parent
_root = _pkg.parent


def _find_dir(*rel: str) -> Path:
    for base in (_pkg, _root):
        p = base.joinpath(*rel)
        if p.is_dir():
            return p
    return _pkg.joinpath(*rel)


FRONTEND_DIST = _find_dir("static", "app")
FRONTEND_PUBLIC = _find_dir("frontend", "public")
DOCS_SITE = _find_dir("static", "docs")


def _with_no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _csp_policy(nonce: str) -> str:
    """Build the dashboard's Content-Security-Policy.

    Nonce-based (not hash-based) because the bootstrap script is injected per
    request and its content varies with the ingress path, so a static hash would
    not match under Home Assistant ingress. ``frame-ancestors 'self'`` allows the
    same-origin HA ingress / Nabu Casa remote iframe while blocking foreign framing.
    Verified compliant on both standalone and HA ingress (via a report-only pass)
    before being enforced.
    """
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            # mqtt.js runs its client in a Web Worker created from a blob: URL.
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            # HA ingress (and Nabu Casa remote) frame the add-on from the same
            # origin, so 'self' allows the iframe while blocking foreign framing.
            "frame-ancestors 'self'",
        )
    )


async def index_handler(request):
    from aiohttp import web

    if request.path.endswith("favicon.svg"):
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    for candidate in [
        FRONTEND_DIST / "index.html",
        _find_dir("frontend") / "index.html",
    ]:
        if candidate.exists():
            ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
            # Per-request nonce for the injected bootstrap script below so the CSP
            # can allow it without 'unsafe-inline'.
            nonce = secrets.token_urlsafe(16)
            # Inject the ingress path so the frontend can prefix all fetch/WS URLs.
            # When not behind ingress, ingress_path is "" and all URLs stay relative.
            inject = (
                f"<script nonce='{nonce}'>window.__WACTORZ_INGRESS_PATH='{ingress_path}';</script>"
            )
            if ingress_path:
                inject = f'<base href="{ingress_path}/">{inject}'

            content = candidate.read_text(encoding="utf-8")
            # Stamp the same nonce on the page's own inline scripts (e.g. the SW
            # registration) so they pass the CSP too. First-party bare `<script>`
            # tags only — the module bundle carries `type=`/`src=`.
            content = content.replace("<script>", f"<script nonce='{nonce}'>")
            content = content.replace("<head>", f"<head>{inject}", 1)
            response = _with_no_cache(web.Response(text=content, content_type="text/html"))
            response.headers["Content-Security-Policy"] = _csp_policy(nonce)
            return response
    raise web.HTTPNotFound()


async def static_handler(request):
    from aiohttp import web

    rel = request.match_info["path"]

    # Special case for favicon if it's requested at root
    if rel == "favicon.svg":
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")

    for base in [FRONTEND_DIST, FRONTEND_PUBLIC]:
        candidate = base / rel
        try:
            candidate = candidate.resolve()
            if candidate.is_file() and str(candidate).startswith(str(base.resolve())):
                # If it's a JS file and we're behind Ingress, we must rewrite hardcoded absolute paths
                if candidate.suffix == ".js" and ingress_path:
                    content = candidate.read_text(encoding="utf-8")
                    # Rewrite hardcoded paths from "/api/..." to "api/..." or prepending ingress_path
                    # The frontend seems to use "/api/actors", "/api/config", etc.
                    content = content.replace('"/api/', f'"{ingress_path}/api/')
                    content = content.replace('"/config"', f'"{ingress_path}/config"')
                    content = content.replace('"/actors"', f'"{ingress_path}/actors"')
                    # Point the WebSocket at the monitor's actual port (WS_PORT),
                    # not HA's 8123. WS_PORT is where the /ws and /mqtt proxies live.
                    host = request.host.split(":")[0]
                    content = content.replace(
                        '"ws://localhost:9001"', f'"ws://{host}:{WS_PORT}/mqtt"'
                    )
                    content = content.replace(
                        "`ws://${location.host}/ws`", f"`ws://${{location.hostname}}:{WS_PORT}/ws`"
                    )
                    content = content.replace(
                        "`ws://${location.host}/mqtt`",
                        f"`ws://${{location.hostname}}:{WS_PORT}/mqtt`",
                    )

                    return _with_no_cache(
                        web.Response(text=content, content_type="application/javascript")
                    )

                return _with_no_cache(web.FileResponse(candidate))
        except Exception:
            pass
    raise web.HTTPNotFound()


async def docs_handler(request):
    from aiohttp import web

    if not DOCS_SITE.is_dir():
        raise web.HTTPNotFound(
            reason="Docs not built — run: python3 scripts/build_docs.py  (or: make docs-build)"
        )
    rel = request.match_info.get("path", "") or "index.html"
    if not rel or rel.endswith("/"):
        rel += "index.html"
    root = DOCS_SITE.resolve()
    candidate = (DOCS_SITE / rel).resolve()
    try:
        if candidate.is_file() and str(candidate).startswith(str(root)):
            return web.FileResponse(candidate)
        if rel.endswith("index.html") and not candidate.exists():
            parent = candidate.parent
            if parent.is_dir():
                for sub in sorted(parent.iterdir()):
                    if sub.is_dir() and (sub / "index.html").exists():
                        raise web.HTTPFound(request.path.rstrip("/") + f"/{sub.name}/index.html")
    except web.HTTPFound:
        raise
    except Exception:
        pass
    raise web.HTTPNotFound()


def _encode_mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return bytes((len(b) >> 8, len(b) & 0xFF)) + b


def _inject_connect_credentials(pkt: bytes, username: str, password: str) -> bytes:
    """Add username/password to an anonymous MQTT CONNECT packet, in-flight.

    The browser's mqtt.js client connects with no credentials, so an
    authenticated broker rejects it ("Not authorized") and the dashboard falls
    back to demo data. This rewrites the CONNECT as it passes through the proxy
    so the broker accepts it — without ever sending the credentials to the
    browser. Works for MQTT 3.1.1 and 5.0; username/password are the last two
    payload fields in both, so they're appended at the end.

    Any non-CONNECT packet, an already-credentialed CONNECT, or a malformed /
    partial buffer is returned unchanged.
    """
    if len(pkt) < 2 or pkt[0] != 0x10:  # not a CONNECT (packet type 1, flags 0)
        return pkt
    rem_len = 0
    mult = 1
    idx = 1
    while True:  # decode Remaining Length (variable byte integer)
        if idx >= len(pkt):
            return pkt
        b = pkt[idx]
        rem_len += (b & 0x7F) * mult
        idx += 1
        if not (b & 0x80):
            break
        mult *= 128
        if mult > 128**3:
            return pkt
    body = pkt[idx : idx + rem_len]
    if len(body) < rem_len or len(body) < 4:
        return pkt  # split across frames — don't risk corrupting it
    pn_len = (body[0] << 8) | body[1]
    flags_pos = 2 + pn_len + 1  # protocol name + 1-byte protocol level
    if flags_pos >= len(body):
        return pkt
    if body[flags_pos] & 0x80:  # username flag already set — leave it alone
        return pkt
    new_body = bytearray(body)
    new_body[flags_pos] |= 0xC0  # set username + password flags
    new_body += _encode_mqtt_str(username) + _encode_mqtt_str(password)
    rl = bytearray()  # re-encode Remaining Length
    x = len(new_body)
    while True:
        d = x % 128
        x //= 128
        if x:
            d |= 0x80
        rl.append(d)
        if not x:
            break
    return bytes((0x10,)) + bytes(rl) + bytes(new_body)


def _proxy_mqtt_creds():
    """Broker creds for the dashboard's MQTT proxy, or None when anonymous."""
    from .config import CONFIG

    if CONFIG.mqtt_username:
        return (CONFIG.mqtt_username, CONFIG.mqtt_password)
    return None


async def _bridge_mqtt_tcp(client_ws, broker: str, port: int) -> None:
    from aiohttp import WSMsgType

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(broker, port), timeout=3)
    except Exception as exc:
        logger.warning("MQTT TCP bridge: cannot connect to %s:%s — %s", broker, port, exc)
        return

    creds = _proxy_mqtt_creds()

    async def ws_to_tcp():
        first = creds is not None  # inject creds into the browser's CONNECT
        try:
            async for msg in client_ws:
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                    if first:
                        first = False
                        data = _inject_connect_credentials(data, creds[0], creds[1])
                    writer.write(data)
                    await writer.drain()
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            writer.close()

    async def tcp_to_ws():
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                await client_ws.send_bytes(data)
        finally:
            await client_ws.close()

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)


async def mqtt_proxy_handler(request):
    import aiohttp
    from aiohttp import WSMsgType, web

    raw_proto = request.headers.get("Sec-WebSocket-Protocol", "")
    protocols = [p.strip() for p in raw_proto.split(",") if p.strip()]
    client_ws = web.WebSocketResponse(protocols=protocols)
    try:
        await client_ws.prepare(request)
    except Exception as exc:
        logger.error(
            "[MQTT proxy] WebSocket handshake failed — %s | headers: %s", exc, dict(request.headers)
        )
        raise

    logger.debug("[MQTT proxy] WS accepted from %s proto=%s", request.remote, protocols)

    upstream_url = f"ws://{MQTT_BROKER}:{MQTT_WS_PORT}/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                upstream_url,
                protocols=protocols,
                headers={"Sec-WebSocket-Protocol": ",".join(protocols)} if protocols else {},
                timeout=aiohttp.ClientTimeout(connect=2),
            ) as upstream_ws:
                logger.debug("[MQTT proxy] upstream WS connected → %s", upstream_url)
                creds = _proxy_mqtt_creds()

                async def forward(src, dst, inject=False):
                    first = inject  # inject creds into the browser's CONNECT
                    async for msg in src:
                        if msg.type == WSMsgType.BINARY:
                            data = msg.data
                            if first:
                                first = False
                                data = _inject_connect_credentials(data, creds[0], creds[1])
                            await dst.send_bytes(data)
                        elif msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break

                await asyncio.gather(
                    forward(client_ws, upstream_ws, inject=creds is not None),
                    forward(upstream_ws, client_ws),
                )
        return client_ws
    except Exception as exc:
        logger.info(
            "[MQTT proxy] upstream WS unavailable (%s), falling back to TCP bridge %s:%s",
            exc,
            MQTT_BROKER,
            MQTT_PORT,
        )

    await _bridge_mqtt_tcp(client_ws, MQTT_BROKER, MQTT_PORT)
    return client_ws


def _actor_payload(ag: dict) -> dict:
    return {
        "id": ag.get("agent_id", ""),
        "name": ag.get("name", ""),
        "state": ag.get("state", "unknown"),
        "protected": ag.get("protected", False),
        "cpu": ag.get("cpu"),
        "mem": ag.get("mem"),
        "task": ag.get("task"),
        "messagesProcessed": ag.get("messages_processed"),
        "costUsd": ag.get("cost_usd"),
    }


def _final_cost_from_db(name: str):
    """Read the persisted _final_cost cost_usd for an agent name, or None."""
    if db is None or not name:
        return None
    try:
        import json as _json

        row = db.conn.execute(
            "SELECT value FROM kv_store WHERE agent=? AND key='_final_cost'",
            (name,),
        ).fetchone()
        if row:
            return _json.loads(row[0]).get("cost_usd")
    except Exception:
        pass
    return None


def _actor_cost(actor, ag: dict):
    """Return the most accurate cost available: MQTT-derived first, then live object, then SQLite."""
    mqtt_cost = ag.get("cost_usd")
    if mqtt_cost is not None:
        return mqtt_cost
    live_cost = getattr(actor, "total_cost_usd", None)
    if live_cost:
        return round(live_cost, 6)
    return _final_cost_from_db(getattr(actor, "name", None))


def _best_cost(ag, actor, name: str) -> float:
    """Resolve an agent's cost the SAME way the cards do (_actor_cost): MQTT
    state first, then the live actor object, then the persisted _final_cost row.
    Returns 0.0 when nothing is known so it can be summed safely. The headline
    total must use this — summing only state["cost_usd"] dropped agents whose
    cost lives on the actor object / SQLite.
    """
    if ag is not None:
        c = ag.get("cost_usd")
        if c is not None:
            try:
                return float(c)
            except (TypeError, ValueError):
                pass
    if actor is not None:
        c = getattr(actor, "total_cost_usd", None)
        if c:
            try:
                return float(c)
            except (TypeError, ValueError):
                pass
    c = _final_cost_from_db(name)
    try:
        return float(c) if c is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _best_msgs(ag, actor) -> int:
    """Resolve message count: MQTT state first, then the live actor's metrics."""
    if ag is not None:
        m = ag.get("messages_processed")
        if m:
            try:
                return int(m)
            except (TypeError, ValueError):
                pass
    m = getattr(getattr(actor, "metrics", None), "messages_processed", 0)
    try:
        return int(m) if m else 0
    except (TypeError, ValueError):
        return 0


async def health_handler(request):
    from aiohttp import web

    return web.json_response({"status": "ok"})


async def cost_handler(request):
    from aiohttp import web

    from .agents.llm_agent import get_global_cost_info

    return web.json_response(get_global_cost_info())


async def cost_limit_handler(request):
    from aiohttp import web

    from .agents.llm_agent import set_cost_limit

    try:
        body = await request.json()
        limit_usd = float(body.get("limit_usd", 0))
        period = body.get("period", "monthly")
        if period not in ("daily", "weekly", "monthly"):
            return web.json_response(
                {"error": "period must be daily, weekly, or monthly"}, status=400
            )
        set_cost_limit(limit_usd, period)
        return web.json_response({"ok": True, "limit_usd": limit_usd, "period": period})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def cost_reset_handler(request):
    from aiohttp import web

    from .agents.llm_agent import reset_global_cost

    try:
        info = reset_global_cost()
        # Clear the in-memory lifetime ledger so max() doesn't pin the display
        # to pre-reset values for the rest of this process lifetime.
        _lifetime_cost.clear()
        if db is not None:
            try:
                db.kv_delete("_system", _LIFETIME_LEDGER_KEY)
            except Exception:
                pass
        return web.json_response({"ok": True, **info})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def send_message_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]
    if registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = data.get("content", "").strip()
    if not content:
        return web.json_response({"error": "content required"}, status=400)
    actor = registry.get(actor_id) or registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    # This endpoint names an explicit target, but _route_chat re-derives the
    # target from the text and defaults to main — so without this the addressed
    # actor is dropped. Prepend the mention to route there, unless the caller
    # already addressed someone (@) or it's a slash command (/).
    routed = content if content.startswith(("@", "/")) else f"@{actor.name} {content}"
    _track_chat_task(asyncio.create_task(_route_chat(routed, lambda t: None)))
    return web.json_response({"status": "sent"})


async def delete_actor_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]
    # Resolve the dashboard's record first so remote agents (which aren't in
    # the local registry) can still be deleted via this endpoint. The earlier
    # 503/404 short-circuit made remote deletes impossible.
    record = state["agents"].get(actor_id) or {}
    if not record:
        # Fall back to local-registry lookup so a name-based ID still works.
        if registry is not None:
            actor = registry.get(actor_id) or registry.find_by_name(actor_id)
            if actor is None:
                return web.json_response({"error": "actor not found"}, status=404)
            if getattr(actor, "protected", False):
                return web.json_response({"error": "actor is protected"}, status=403)
            actor_id = actor.actor_id
        else:
            return web.json_response({"error": "actor not found"}, status=404)
    if record.get("protected"):
        return web.json_response({"error": "actor is protected"}, status=403)
    routed = await _delete_agent(actor_id)
    await broadcast({"type": "delete_agent", "agent_id": actor_id, "state": _snapshot()})
    return web.Response(status=200, text=f"stopping ({routed})")


async def reset_handler(request):
    """POST /api/reset  —  clear stored state and broadcast a reset event.

    Body (JSON):
      scope   : "chat" | "state" | "metrics" | "spawns" | "all"  (required)
      agent   : str  (optional — limit to one agent by name)
    """
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    scope = body.get("scope", "")
    agent = body.get("agent") or None

    valid = {"chat", "state", "metrics", "spawns", "logs", "all"}
    if scope not in valid:
        return web.json_response({"error": f"scope must be one of {sorted(valid)}"}, status=400)

    import wactorz.reset as _reset

    if scope == "all":
        # Block incoming heartbeats, stop all actors, wipe disk, clear memory
        global _hard_resetting
        _hard_resetting = True
        # Wrap the whole teardown so _hard_resetting ALWAYS resets — otherwise a
        # mid-wipe exception leaves it True forever and every incoming heartbeat
        # stays blocked, freezing the dashboard until the process restarts.
        try:
            supervisor = (
                getattr(registry, "_supervisor_ref", None) if registry is not None else None
            )
            all_actors = list(registry.all_actors()) if registry is not None else []
            # Only stop user-spawned (non-protected) actors — system actors keep running
            stoppable = [a for a in all_actors if not getattr(a, "protected", False)]
            # The registry is the AUTHORITATIVE source of the protected flag — the
            # dashboard entry's "protected" is only set when a heartbeat happened to
            # carry it, so trusting it alone wrongly tears down system agents (main /
            # monitor / installer / catalog) and they flicker back on the next beat.
            protected_ids = {a.actor_id for a in all_actors if getattr(a, "protected", False)}
            protected_names = {a.name for a in all_actors if getattr(a, "protected", False)}
            # Tear down every NON-protected agent the dashboard knows about (covers an
            # agent present only via MQTT, or a remote agent absent from this registry),
            # but never one that maps to a protected registry actor.
            dash_ids = [
                aid
                for aid, ag in state["agents"].items()
                if not ag.get("protected", False)
                and aid not in protected_ids
                and ag.get("name") not in protected_names
            ]
            agent_ids = list({a.actor_id for a in stoppable} | set(dash_ids))

            # Release supervised actors first so the Supervisor doesn't race to
            # restart them, then stop + unregister the live local ones.
            if supervisor is not None:
                for actor in stoppable:
                    supervisor.release(actor.name)
            await asyncio.gather(*[actor.stop() for actor in stoppable], return_exceptions=True)
            await asyncio.gather(
                *[registry.unregister(a.actor_id) for a in stoppable],
                return_exceptions=True,
            )

            # Stop agents living on runner nodes (not in this registry) and clear the
            # retained spawn directives that would otherwise replay on reconnect.
            # Harmless when there are no nodes.
            node_names = set(state["nodes"].keys())
            _main = registry.find_by_name("main") if registry is not None else None
            if _main is not None and hasattr(_main, "_get_spawn_registry"):
                for cfg in (_main._get_spawn_registry() or {}).values():
                    n = (cfg.get("node") or "").strip()
                    if n:
                        node_names.add(n)
            if mqtt_client_ref and node_names:
                await asyncio.gather(
                    *[
                        mqtt_client_ref.publish(
                            f"nodes/{n}/stop_all", json.dumps({"reason": "wipe everything"}), qos=1
                        )
                        for n in node_names
                    ],
                    return_exceptions=True,
                )
                await asyncio.gather(
                    *[
                        mqtt_client_ref.publish(f"nodes/{n}/spawn", b"", retain=True)
                        for n in node_names
                    ],
                    return_exceptions=True,
                )

            # Purge retained MQTT for EVERY non-protected agent, tombstone each so a
            # late/in-flight frame can't re-admit it once _hard_resetting clears, and
            # drop it from the dashboard now.
            await asyncio.gather(
                *[_purge_agent_retained(aid) for aid in agent_ids],
                return_exceptions=True,
            )
            for aid in agent_ids:
                _mark_deleted(aid)
                state["agents"].pop(aid, None)

            # Clear the live spawn registry + retained desired_state so neither a
            # restart nor a runner reconnect can resurrect the wiped agents. Runs
            # before reset_all() wipes the kv on disk (it reads the registry first).
            await _purge_spawn_reconcile(None)
            # Reset in-memory metrics + history on protected (system) actors
            protected_actors = [a for a in all_actors if getattr(a, "protected", False)]
            for actor in protected_actors:
                actor.metrics.messages_processed = 0
                actor.metrics.errors = 0
                actor.metrics.tasks_completed = 0
                actor.metrics.tasks_failed = 0
                _reset_actor_cost(actor)
                if hasattr(actor, "_conversation_history"):
                    actor._conversation_history = []
                if hasattr(actor, "_history_summary"):
                    actor._history_summary = ""
                if hasattr(actor, "_user_facts"):
                    actor._user_facts = {}
                if hasattr(actor, "_pipeline_rules"):
                    actor._pipeline_rules = []
                # NOTE: the kv-backed spawn registry is cleared by
                # _purge_spawn_reconcile() above — assigning the _spawned_agents
                # attribute was a no-op (the registry is read via recall()).
            # Clear the Fuseki dataset too, if configured (best-effort).
            try:
                from .config import CONFIG

                if CONFIG.fuseki_url:
                    import aiohttp as _aiohttp

                    fuseki_update = f"{CONFIG.fuseki_url}/{CONFIG.fuseki_dataset}/update"
                    async with _aiohttp.ClientSession() as _sess:
                        await _sess.post(
                            fuseki_update,
                            data={"update": "DELETE WHERE { ?s ?p ?o }"},
                            auth=_aiohttp.BasicAuth(CONFIG.fuseki_user, CONFIG.fuseki_password),
                            timeout=_aiohttp.ClientTimeout(total=5),
                        )
            except Exception as exc:
                logger.debug("[reset] Fuseki clear skipped: %s", exc)
            _reset.reset_all(agent)
            # Clear the in-memory lifetime cost ledger + global accumulator too, or
            # the headline total re-pins to the old high-water once _hard_resetting
            # clears and heartbeats resume (mirrors /api/cost/reset).
            _lifetime_cost.clear()
            try:
                from .agents.llm_agent import reset_global_cost

                reset_global_cost()
            except Exception as exc:
                logger.debug("[reset] reset_global_cost skipped: %s", exc)
            state["agents"].clear()
            state["nodes"].clear()
            state["alerts"].clear()
            state["log_feed"].clear()
            await broadcast(
                {
                    "type": "reset",
                    "scope": "all",
                    "agent": None,
                    "state": {
                        "agents": [],
                        "nodes": [],
                        "alerts": [],
                        "log_feed": [],
                        "total_cost_usd": 0,
                        "total_messages": 0,
                    },
                }
            )
        finally:
            _hard_resetting = False
        return web.json_response({"status": "ok", "scope": "all", "agent": None})
    if scope == "chat":
        _reset.reset_chat(agent)
        # Also clear the LIVE in-memory conversation on running actors. reset_chat
        # only clears the persisted chat_log/kv, so without this the agent still
        # "remembers" the conversation (and re-persists it on the next turn) until
        # a restart — the same live-vs-disk gap the metrics scope guards against.
        live_actors = list(registry.all_actors()) if registry is not None else []
        for actor in live_actors:
            if agent and actor.name != agent:
                continue
            if hasattr(actor, "_conversation_history"):
                actor._conversation_history = []
            if hasattr(actor, "_history_summary"):
                actor._history_summary = ""
    elif scope == "state":
        if agent:
            _reset.reset_agent_state(agent)
        else:
            _reset._reset_all_pickles()
    elif scope == "metrics":
        _reset.reset_metrics(agent)
        # Also zero the LIVE in-memory counters on running actors. reset_metrics
        # only clears the persisted kv snapshot, so without this the next
        # heartbeat re-reports the old totals and the dashboard looks unchanged
        # until a restart. Scoped to metrics/cost fields only (not history).
        live_actors = list(registry.all_actors()) if registry is not None else []
        for actor in live_actors:
            if agent and actor.name != agent:
                continue
            actor.metrics.messages_processed = 0
            _reset_actor_cost(actor)
        # The headline total is max(live + historical, lifetime ledger).
        # reset_metrics cleared the kv ledger, but the in-memory _lifetime_cost
        # high-water survives in THIS process and pins the headline to its old
        # value — so the total only "drops" by the live component and never
        # zeroes. Clear it here, mirroring /api/cost/reset.
        if agent:
            aid = next((getattr(a, "actor_id", None) for a in live_actors if a.name == agent), None)
            if aid:
                _lifetime_cost.pop(aid, None)
        else:
            _lifetime_cost.clear()
            try:
                from .agents.llm_agent import reset_global_cost

                reset_global_cost()
            except Exception as exc:
                logger.debug("[reset] reset_global_cost skipped: %s", exc)
    elif scope == "spawns":
        # Clear live state + retained desired_state FIRST (it needs the registry
        # to learn the affected nodes), then wipe the kv on disk. Without this,
        # clearing the registry left desired_state behind and a runner reconnect
        # reconciled the agents straight back.
        await _purge_spawn_reconcile(agent)
        _reset.reset_spawns(agent)
    elif scope == "logs":
        _reset.reset_logs()
        # The activity feed mirrors the log files we just truncated, so drop the
        # in-memory entries too — otherwise the UI keeps showing stale lines.
        state["log_feed"].clear()

    # Clear in-memory dashboard cost/message state for the affected agents.
    # Scoped to "metrics" only — clearing chat history must not zero cost/
    # message counters or wipe the alerts/activity feed.
    if scope == "metrics":
        if agent:
            aid = next((k for k, v in state["agents"].items() if v.get("name") == agent), None)
            if aid:
                state["agents"][aid].pop("cost_usd", None)
                state["agents"][aid].pop("messages_processed", None)
        else:
            for aid in state["agents"]:
                state["agents"][aid].pop("cost_usd", None)
                state["agents"][aid].pop("messages_processed", None)
            state["alerts"].clear()
            state["log_feed"].clear()

    await broadcast({"type": "reset", "scope": scope, "agent": agent, "state": _snapshot()})
    return web.json_response({"status": "ok", "scope": scope, "agent": agent})


async def pause_actor_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]
    if registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    actor = registry.get(actor_id) or registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    if getattr(actor, "protected", False):
        return web.json_response({"error": "actor is protected"}, status=403)
    if mqtt_client_ref:
        await mqtt_client_ref.publish(
            f"agents/{actor_id}/commands",
            json.dumps({"command": "pause", "sender": "api", "timestamp": time.time()}),
        )
    return web.json_response({"status": "pausing"})


async def resume_actor_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]
    if registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    actor = registry.get(actor_id) or registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    if getattr(actor, "protected", False):
        return web.json_response({"error": "actor is protected"}, status=403)
    if mqtt_client_ref:
        await mqtt_client_ref.publish(
            f"agents/{actor_id}/commands",
            json.dumps({"command": "resume", "sender": "api", "timestamp": time.time()}),
        )
    return web.json_response({"status": "resuming"})


async def actor_metrics_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]
    ag = state["agents"].get(actor_id)
    actor = None
    if registry is not None:
        actor = registry.get(actor_id) or registry.find_by_name(actor_id)
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


async def rest_chat_handler(request):
    """POST /chat — fire-and-forget a message to a named agent."""
    from aiohttp import web

    if registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    message = data.get("message", "").strip()
    agent_name = data.get("agent_name", "main-actor")
    if not message:
        return web.json_response({"error": "message required"}, status=400)
    target = registry.find_by_name(agent_name)
    if target is None:
        return web.json_response({"error": f"agent '{agent_name}' not found"}, status=404)
    # As above: route to the named agent, since _route_chat would otherwise
    # default to main when the message carries no @mention.
    routed = message if message.startswith(("@", "/")) else f"@{target.name} {message}"
    _track_chat_task(asyncio.create_task(_route_chat(routed, lambda t: None)))
    return web.json_response({"status": "sent", "agent": agent_name})


async def rest_chat_stop_handler(request):
    """POST /chat/stop — cancel any in-flight generation. No request body needed.

    Works in both runtime modes:
      - direct_ws — cancels the in-process generation task(s) running here; the
        cancelled stream finalizes and posts "⏹ Stopped." over the WebSocket.
      - mqtt (legacy) — publishes {"action": "stop"} to io/chat/control so the
        IOAgent cancels the turn it is streaming and replies on io/chat/response.
    The user-facing confirmation rides the usual chat reply path, so the UI
    needs no extra subscription.
    """
    from aiohttp import web

    # direct_ws: cancel the in-process generation task(s).
    tasks = [t for t in _inflight_chat_tasks if not t.done()]
    for t in tasks:
        t.cancel()

    # legacy MQTT: tell the IOAgent to stop whatever it is generating.
    published = False
    if mqtt_client_ref:
        try:
            await mqtt_client_ref.publish(
                "io/chat/control",
                json.dumps({"action": "stop"}),
                qos=1,
            )
            published = True
        except Exception as exc:
            logger.warning(f"[chat/stop] io/chat/control publish failed: {exc}")

    return web.json_response(
        {
            "status": "stopped",
            "cancelled": len(tasks),
            "published": published,
        }
    )


async def actors_handler(request):
    from aiohttp import web

    # Prefer the live registry (injected by cli.py) — actor objects carry the
    # authoritative protected flag.  Fall back to MQTT-derived state dict when
    # the registry is unavailable (standalone monitor_server mode).
    #
    # CONTRACT: the registry path intentionally excludes remote-runner agents
    # (they are not in the local Python registry).  The frontend relies on this
    # to distinguish local vs remote agents: any agent absent from this response
    # but present via MQTT heartbeat with a "node" field is a remote agent and
    # must NOT be evicted by the 15-second REST reconcile cycle.
    if registry is not None:
        result = []
        for actor in registry.all_actors():
            if _is_deleted(actor.actor_id):
                continue
            ag = state["agents"].get(actor.actor_id, {})
            result.append(
                {
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
                    "costUsd": _actor_cost(actor, ag),
                }
            )
        return web.json_response(result)
    return web.json_response([_actor_payload(ag) for ag in state["agents"].values()])


async def actor_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]
    ag = state["agents"].get(actor_id)
    if ag is None:
        return web.json_response({"error": "actor not found"}, status=404)
    return web.json_response(_actor_payload(ag))


async def actor_history_handler(request):
    from aiohttp import web

    actor_id = request.match_info["actor_id"]

    # Resolve actor: the frontend sends the agent NAME (not UUID), so try
    # direct UUID lookup first, then fall back to name-based lookup.
    actor = None
    if registry is not None:
        actor = registry.get(actor_id) or registry.find_by_name(actor_id)

    if actor is not None and hasattr(actor, "recall"):
        history = actor.recall("conversation_history", [])
    elif db is not None:
        # Actor not in registry (deleted or name-only lookup) — read from SQLite.
        # actor_id might be a display name (e.g. "main") — try it directly.
        try:
            import json as _json

            row = db.conn.execute(
                "SELECT value FROM kv_store WHERE agent=? AND key='conversation_history'",
                (actor_id,),
            ).fetchone()
            history = _json.loads(row[0]) if row else []
        except Exception:
            history = []
    else:
        history = []

    visible = [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    return web.json_response(visible)


async def chat_log_handler(request):
    """GET /api/chats — query the persistent chat_log table.

    Query params:
      agent   — filter by agent name
      role    — filter by role (user | assistant)
      since   — Unix timestamp float, only return rows newer than this
      limit   — max rows to return (default 200, max 1000)
    """
    from aiohttp import web

    if db is None:
        return web.json_response([], status=200)
    try:
        agent = request.rel_url.query.get("agent")
        role = request.rel_url.query.get("role")
        since = float(request.rel_url.query["since"]) if "since" in request.rel_url.query else None
        limit = min(int(request.rel_url.query.get("limit", 200)), 1000)
        rows = db.query_chat_log(agent_name=agent, role=role, since=since, limit=limit)
        return web.json_response(rows)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


_tts_voices_cache: list | None = None
_ha_bridge_task: asyncio.Task | None = None


async def _start_ha_bridge(_app=None) -> None:
    """Launch HAFusekiBridge as a background task if HA_TOKEN is configured."""
    global _ha_bridge_task
    from .config import CONFIG

    if not CONFIG.ha_token or not CONFIG.fuseki_url:
        return
    try:
        from .fuseki import HAFusekiBridge, _run_with_retry, fuseki_reachable
    except Exception as exc:
        logger.warning("[ha-bridge] Could not import HAFusekiBridge: %s", exc)
        return

    # Don't start the bridge if Fuseki isn't actually running — otherwise it
    # connects to HA and then fails to write every state change, flooding the
    # log. If you're not using Fuseki, the bridge simply stays off.
    if not await fuseki_reachable(CONFIG.fuseki_url):
        logger.info(
            "[ha-bridge] Fuseki not reachable at %s — HA→Fuseki bridge "
            "disabled. (Start Fuseki and POST /api/ha/sync to enable, "
            "or ignore if you don't use Fuseki.)",
            CONFIG.fuseki_url,
        )
        return

    bridge = HAFusekiBridge(
        ha_url=CONFIG.ha_url,
        ha_token=CONFIG.ha_token,
        fuseki_url=CONFIG.fuseki_url,
        fuseki_dataset=CONFIG.fuseki_dataset,
        fuseki_user=CONFIG.fuseki_user,
        fuseki_password=CONFIG.fuseki_password,
    )
    _ha_bridge_task = asyncio.create_task(
        _run_with_retry(bridge.run, "HAFusekiBridge"),
        name="ha-fuseki-bridge",
    )
    logger.info(
        "[ha-bridge] HAFusekiBridge started (ha=%s → fuseki=%s/%s)",
        CONFIG.ha_url,
        CONFIG.fuseki_url,
        CONFIG.fuseki_dataset,
    )


async def ha_sync_handler(request):
    """POST /api/ha/sync — cancel and restart the HA→Fuseki bridge immediately."""
    from aiohttp import web

    from .config import CONFIG

    global _ha_bridge_task
    if not CONFIG.ha_token:
        return web.json_response({"error": "HA_TOKEN not configured"}, status=400)
    if _ha_bridge_task and not _ha_bridge_task.done():
        _ha_bridge_task.cancel()
        try:
            await _ha_bridge_task
        except (asyncio.CancelledError, Exception):
            pass
    await _start_ha_bridge()
    return web.json_response({"status": "restarted"})


# ── SWID resolution (W3C DID-Resolution shape; pilot swid: scheme) ──────────
# The routes themselves live in wactorz.core.swid.resolver.aiohttp_routes; the
# monitor only supplies a per-request registry. Kept as one self-contained block.
from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _open_swid_registry():
    """Yield a SWID registry for one request: Fuseki-backed, or empty when unset.

    A short-lived Fuseki session per request (matching the pattern used
    elsewhere). When no triplestore is configured, an empty in-memory registry
    makes every lookup resolve to ``notFound`` — the desired behaviour.
    """
    import aiohttp as _aiohttp

    from .config import CONFIG
    from .core.swid import FusekiSwidRegistry, InMemoryRegistry
    from .fuseki import FusekiClient

    if not CONFIG.fuseki_url:
        yield InMemoryRegistry()
        return
    auth = _aiohttp.BasicAuth(CONFIG.fuseki_user, CONFIG.fuseki_password)
    async with _aiohttp.ClientSession() as session:
        yield FusekiSwidRegistry(
            FusekiClient(CONFIG.fuseki_url, CONFIG.fuseki_dataset, session, auth)
        )


async def _warm_tts_voices(_app=None) -> None:
    """Load edge-tts voice list once at startup and cache it."""
    global _tts_voices_cache
    try:
        import edge_tts

        voices = await edge_tts.list_voices()
        _tts_voices_cache = [
            {"name": v["ShortName"], "locale": v["Locale"], "gender": v["Gender"]}
            for v in sorted(voices, key=lambda v: v["ShortName"])
        ]
    except Exception:
        _tts_voices_cache = []


async def tts_voices_handler(request):
    """GET /api/tts/voices — list available edge-tts voices."""
    from aiohttp import web

    try:
        import edge_tts as _  # noqa: F401 — check installed
    except ImportError:
        return web.json_response([])
    if _tts_voices_cache is None:
        await _warm_tts_voices()
    return web.json_response(_tts_voices_cache or [])


async def tts_handler(request):
    """GET /api/tts?text=...&voice=... — synthesize speech via edge-tts.

    Returns audio/mpeg. Falls back 503 if edge-tts is not installed so the
    frontend can transparently fall back to the Web Speech API.
    """
    import os

    from aiohttp import web

    try:
        import edge_tts
    except ImportError:
        return web.Response(status=503, text="edge-tts not installed — pip install 'wactorz[tts]'")

    text = request.rel_url.query.get("text", "").strip()
    if not text:
        return web.Response(status=400, text="text param required")

    # Mirror TTSManager: strip code blocks, cap at 300 chars
    import re

    text = re.sub(r"```[\s\S]*?```", "code block", text)[:300]

    default_voice = os.environ.get("TTS_VOICE", "en-US-JennyNeural")
    voice = request.rel_url.query.get("voice", default_voice) or default_voice

    try:
        communicate = edge_tts.Communicate(text, voice)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        audio = b"".join(chunks)
        return web.Response(
            body=audio,
            content_type="audio/mpeg",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return web.Response(status=500, text=str(exc))


async def config_handler(request):
    """Expose non-secret runtime config so the frontend can seed its defaults."""
    from aiohttp import web

    from .config import CONFIG

    # The /mqtt and /ws proxies are served by *this* server, so point the
    # frontend at the monitor's actual port (WS_PORT), not a hardcoded one.
    raw_host = request.host.split(":")[0]
    ws_host = f"{raw_host}:{WS_PORT}"
    protocol = "wss" if request.secure else "ws"

    mqtt_url = f"{protocol}://{ws_host}/mqtt"
    ws_url = f"{protocol}://{ws_host}/ws"

    return web.json_response(
        {
            "ha": {
                # URL only — the dashboard links out to the HA UI and never talks to
                # HA directly, so the long-lived token must NOT reach the browser.
                "url": CONFIG.ha_url,
            },
            "fuseki": {
                # url (display) + dataset (proxy path) only. The browser queries
                # through the /api/fuseki proxy, which injects the credentials
                # server-side — so user/password must NOT reach the browser.
                "url": CONFIG.fuseki_url,
                "dataset": CONFIG.fuseki_dataset,
            },
            "mqtt": {
                "host": MQTT_BROKER,
                "port": MQTT_PORT,
                "url": mqtt_url,
            },
            "llm": {
                "provider": CONFIG.llm_provider,
                "model": CONFIG.llm_model,
            },
            "weather": {
                "defaultLocation": CONFIG.weather_default_location,
            },
            "ws_url": ws_url,
        }
    )


async def feed_handler(request):
    """Return recent chat events for the UI feed, with REAL persisted timestamps.

    Previously this read from kv_store.conversation_history, which is just a
    JSON list with no timestamps — so each entry got `i` (the loop index) as
    its timestamp and the frontend re-dated them to "now - i*delta", causing
    timestamps to reset on every page reload / restart.

    Now we read from the chat_log table, which has a real `ts REAL` column
    written at the moment each turn happens. Falls back to the legacy
    kv_store path only if chat_log is empty (e.g. a freshly upgraded DB
    where nothing has been written yet) so existing users still see their
    pre-upgrade history on first launch.
    """
    from aiohttp import web

    if db is None:
        return web.json_response([])
    try:
        # Primary path — persistent chat_log with real timestamps.
        try:
            rows = db.query_chat_log(limit=50)
        except Exception as exc:
            logger.warning(f"[feed] chat_log query failed: {exc}")
            rows = []

        if rows:
            # query_chat_log returns newest-first; the frontend expects
            # chronological (oldest-first) so the latest message ends up
            # at the bottom of the feed.
            rows = list(reversed(rows))
            items = [
                {
                    "type": "chat",
                    "label": str(r.get("content", "")),
                    "agentName": r.get("agent_name", ""),
                    "role": r.get("role", ""),
                    "timestamp": float(r.get("ts", 0.0)),  # REAL Unix time, not an index
                    "_seq": i,
                    "_agent": r.get("agent_name", ""),
                }
                for i, r in enumerate(rows)
            ]
            return web.json_response(items)

        # Fallback — legacy kv_store path. Keeps old DBs displaying *something*
        # until new chat turns start populating chat_log. Synthesises a
        # timestamp by anchoring the last entry to "now" and walking backwards
        # in 1-second steps, so at least entries are ordered consistently.
        import json as _json

        kv_rows = db.conn.execute(
            "SELECT agent, value FROM kv_store WHERE key='conversation_history'"
        ).fetchall()
        items = []
        now = time.time()
        for agent_name, value in kv_rows:
            try:
                history = _json.loads(value)
                visible = [
                    m
                    for m in history
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                ]
                n = len(visible)
                for i, msg in enumerate(visible):
                    items.append(
                        {
                            "type": "chat",
                            "label": str(msg.get("content", "")),
                            "agentName": agent_name,
                            "role": msg.get("role", ""),
                            # Synthesised but at least monotonic and anchored
                            # to a real wall-clock value, not a bare index.
                            "timestamp": now - (n - 1 - i),
                            "_seq": i,
                            "_agent": agent_name,
                        }
                    )
            except Exception:
                pass
        return web.json_response(items[-50:])
    except Exception as exc:
        logger.warning(f"[feed] handler failed: {exc}")
        return web.json_response([])


# ── Entry point ────────────────────────────────────────────────────────────


async def main(exit_on_failure: bool = False):
    from aiohttp import web

    # ... (startup checks remain same) ...
    mqtt_ok = await _check_mqtt()
    port_ok = await _check_ws_port()

    if not mqtt_ok or not port_ok:
        msg = []
        if not mqtt_ok:
            msg.append(f"MQTT broker unreachable ({MQTT_BROKER}:{MQTT_PORT})")
        if not port_ok:
            msg.append(f"Port {WS_PORT} already in use")
        logger.error(f"[startup] Cannot start: {'; '.join(msg)}")
        if exit_on_failure:
            raise SystemExit(1)
        return

    @web.middleware
    async def _cors_middleware(request, handler):
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

    app = web.Application(middlewares=[_cors_middleware])
    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/cost", cost_handler)
    app.router.add_get("/cost", cost_handler)
    app.router.add_post("/api/cost/limit", cost_limit_handler)
    app.router.add_post("/cost/limit", cost_limit_handler)
    app.router.add_post("/api/cost/reset", cost_reset_handler)
    app.router.add_post("/cost/reset", cost_reset_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/mqtt", mqtt_proxy_handler)

    # Actor collection
    app.router.add_get("/api/actors", actors_handler)
    app.router.add_get("/actors", actors_handler)

    # Actor control — sub-routes must be registered before /{actor_id} catch-all
    app.router.add_post("/api/actors/{actor_id}/message", send_message_handler)
    app.router.add_post("/actors/{actor_id}/message", send_message_handler)
    app.router.add_post("/api/actors/{actor_id}/pause", pause_actor_handler)
    app.router.add_post("/actors/{actor_id}/pause", pause_actor_handler)
    app.router.add_post("/api/actors/{actor_id}/resume", resume_actor_handler)
    app.router.add_post("/actors/{actor_id}/resume", resume_actor_handler)
    app.router.add_get("/api/actors/{actor_id}/metrics", actor_metrics_handler)
    app.router.add_get("/actors/{actor_id}/metrics", actor_metrics_handler)
    app.router.add_get("/api/actors/{actor_id}/history", actor_history_handler)
    app.router.add_get("/actors/{actor_id}/history", actor_history_handler)

    # Actor CRUD
    app.router.add_get("/api/actors/{actor_id}", actor_handler)
    app.router.add_get("/actors/{actor_id}", actor_handler)
    app.router.add_delete("/api/actors/{actor_id}", delete_actor_handler)
    app.router.add_delete("/actors/{actor_id}", delete_actor_handler)

    # Chat (REST fire-and-forget)
    app.router.add_post("/api/chat", rest_chat_handler)
    app.router.add_post("/chat", rest_chat_handler)
    app.router.add_post("/api/chat/stop", rest_chat_stop_handler)
    app.router.add_post("/chat/stop", rest_chat_stop_handler)

    app.router.add_get("/api/chats", chat_log_handler)
    app.router.add_get("/chats", chat_log_handler)
    app.router.add_get("/api/tts/voices", tts_voices_handler)
    app.router.add_get("/api/tts", tts_handler)
    app.on_startup.append(_warm_tts_voices)
    app.on_startup.append(_start_ha_bridge)

    app.router.add_get("/api/config", config_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_get("/api/feed", feed_handler)
    app.router.add_get("/feed", feed_handler)
    app.router.add_post("/api/reset", reset_handler)
    app.router.add_post("/api/ha/sync", ha_sync_handler)
    app.router.add_get("/favicon.svg", index_handler)
    # Fuseki SPARQL proxy (browser holds no creds; auth injected server-side).
    from .fuseki_proxy import fuseki_proxy_handler

    app.router.add_post("/api/fuseki/{dataset}/sparql", fuseki_proxy_handler)
    app.router.add_post("/api/fuseki/{dataset}/update", fuseki_proxy_handler)
    # SWID resolver (W3C DID Resolution): /1.0/identifiers/{swid}[/profile].
    from .core.swid import aiohttp_routes as _swid_routes

    app.add_routes(_swid_routes(_open_swid_registry))
    app.router.add_get("/docs", lambda r: web.HTTPFound("/docs/"))
    app.router.add_get("/docs/", docs_handler)
    app.router.add_get("/docs/{path:.+}", docs_handler)
    app.router.add_get("/{path:.+}", static_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WS_PORT)
    await site.start()
    logger.info(f"Monitor  → http://localhost:{WS_PORT}/  [chat: {_chat_mode()}]")
    if DOCS_SITE.is_dir():
        logger.info(f"Docs     → http://localhost:{WS_PORT}/docs/")

    await mqtt_listener()


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
    import os

    parser = argparse.ArgumentParser(description="Wactorz Monitor Server")
    parser.add_argument("--broker", default=os.getenv("WACTORZ_BROKER", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-ws-port", type=int, default=int(os.getenv("MQTT_WS_PORT", "9001")))
    parser.add_argument("--ws-port", type=int, default=int(os.getenv("MONITOR_PORT", "8888")))
    args = parser.parse_args()

    thismodule = sys.modules[__name__]
    thismodule.MQTT_BROKER = args.broker
    thismodule.MQTT_PORT = args.mqtt_port
    thismodule.MQTT_WS_PORT = args.mqtt_ws_port
    thismodule.WS_PORT = args.ws_port

    cli_main()
