"""
Wactorz Monitor — WebSocket dashboard + optional MQTT bridge.

Chat routing modes (set via registry wiring in cli.py):
  direct_ws  — registry is set; chat goes straight to actors over WebSocket.
               No IOAgent, no MQTT round-trip for user messages.
  mqtt       — registry is None; chat goes through IOAgent via MQTT (legacy).

The mode is advertised to the browser on connect via a {"type":"config"} frame
so the frontend knows whether to send chat over /ws or publish to io/chat.
"""
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json
import logging
import socket
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

MQTT_BROKER  = "localhost"
MQTT_PORT    = 1883
MQTT_WS_PORT = 9001
WS_PORT      = 8888
MQTT_TOPICS  = ["agents/#", "system/#", "nodes/#", "io/chat"]

# Injected by cli.py after the actor system is built.
# None  → legacy MQTT/IOAgent mode
# <registry> → direct mode (Option B)
registry = None

IO_GATEWAY_ID = "io-gateway"

state = {
    "agents":        {},
    "nodes":         {},
    "alerts":        [],
    "system_health": {},
    "log_feed":      [],
}

ws_clients: set = set()
mqtt_client_ref = None


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
    if agent_id not in state["agents"]:
        state["agents"][agent_id] = {
            "agent_id":   agent_id,
            "name":       agent_id[:8],
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

async def _slash_deploy(node: str, host: str, user: str, pw: str, broker: str,
                        reply_fn):
    if not host:
        await reply_fn(f"[discover] Searching for '{node}' on the network...")
        discovered = None
        for candidate in [f"{node}.local", "raspberrypi.local",
                          f"{node.replace('-', '')}.local"]:
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
            f"[deploy] Need SSH credentials:\n"
            f"  /deploy {node} {host} <user> <password> [broker]"
        )
        return

    main = _find_main()
    if main is None or not hasattr(main, "delegate_to_installer"):
        await reply_fn("[error] Installer agent not available.")
        return

    broker = broker or "localhost"
    await reply_fn(f"[deploy] Deploying to {user}@{host} as '{node}'... (20-60s)")
    result = await main.delegate_to_installer({
        "action": "node_deploy", "host": host, "user": user,
        "password": pw, "node_name": node, "broker": broker,
    }, timeout=120.0)

    if result.get("success"):
        await reply_fn(
            f"[OK] Node '{node}' is live!\n"
            f"  \"spawn a CPU monitor agent on {node}\""
        )
    else:
        await reply_fn(f"[FAIL] {result.get('error', result)}")


async def _scan_subnet_ssh(subnet: str) -> list:
    found = []
    sem   = asyncio.Semaphore(60)

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
    """
    Dispatch a slash command. Returns True if recognised.
    `reply_fn` is an async callable that sends a string back to the user.
    """
    parts = text.split()
    cmd   = parts[0].lower()

    if cmd in ("/help", "/h"):
        await reply_fn(
            "Commands:\n"
            "  /agents                        list all active agents\n"
            "  /nodes                         list remote nodes\n"
            "  /migrate <agent> <node>        move an agent to a different node\n"
            "  /deploy <node> [host [user [pw [broker]]]]\n"
            "                                 deploy a remote Wactorz node\n"
            "  /clear-plans                   clear the plan cache\n\n"
            "Everything else goes to the main orchestrator."
        )
        return True

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
            status    = actor.get_status() if hasattr(actor, "get_status") else {}
            st        = status.get("state", "?")
            protected = " [protected]" if getattr(actor, "protected", False) else ""
            node      = f" [{status['node']}]" if status.get("node") else ""
            lines.append(f"  [{st:8s}] @{actor.name:<22s} {actor.actor_id[:8]}{protected}{node}")
        await reply_fn("Agents:\n" + "\n".join(lines) if lines else "No agents running.")
        return True

    if cmd == "/nodes":
        main         = _find_main()
        remote_nodes = main.list_nodes() if (main and hasattr(main, "list_nodes")) else []
        local        = [a.name for a in registry.all_actors()] if registry else []
        lines = [f"  {'local':20s} online   {', '.join('@'+n for n in local) or '(none)'}"]
        for nd in sorted(remote_nodes, key=lambda x: x["node"]):
            st    = "online" if nd["online"] else "OFFLINE"
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
            node   = parts[1],
            host   = parts[2] if len(parts) > 2 else "",
            user   = parts[3] if len(parts) > 3 else "",
            pw     = parts[4] if len(parts) > 4 else "",
            broker = parts[5] if len(parts) > 5 else "",
            reply_fn = reply_fn,
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
    _end_fn   = stream_end_fn or (lambda: None)

    if content.startswith("/"):
        handled = await handle_slash(content, reply_fn)
        if not handled:
            await reply_fn("Unknown command. Type /help for available commands.")
        return

    target_name, text = _parse_mention(content)
    target = registry.find_by_name(target_name) if registry else None
    if target is None:
        await reply_fn(f"Agent @{target_name} not found.")
        return

    logger.info(f"[io-gateway] → {target.name}: {text[:60]!r}")

    gen_fn = (
        getattr(target, "process_user_input_stream", None)
        or getattr(target, "chat_stream", None)
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
                json.dumps({
                    "from":      IO_GATEWAY_ID,
                    "to":        "user",
                    "content":   text,
                    "timestamp": time.time(),
                }),
            )

    await _route_chat(content, mqtt_reply)  # MQTT path: no streaming, reply_fn used for all output


# ── WebSocket handler ──────────────────────────────────────────────────────

async def ws_handler(request):
    from aiohttp import web, WSMsgType
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    logger.info(f"WebSocket client connected. Total: {len(ws_clients)}")

    # Send initial state
    await ws.send_str(json.dumps({"type": "full_snapshot", "state": _snapshot()}))

    # Advertise chat mode so the frontend knows where to send messages
    await ws.send_str(json.dumps({"type": "config", "chat_mode": _chat_mode()}))

    async def ws_reply(text: str):
        try:
            await ws.send_str(json.dumps({
                "type":      "chat",
                "from":      IO_GATEWAY_ID,
                "content":   text,
                "timestamp": time.time(),
            }))
        except Exception:
            pass

    async def ws_stream_chunk(chunk: str):
        try:
            await ws.send_str(json.dumps({
                "type":      "stream_chunk",
                "from":      IO_GATEWAY_ID,
                "content":   chunk,
                "timestamp": time.time(),
            }))
        except Exception:
            pass

    async def ws_stream_end():
        try:
            await ws.send_str(json.dumps({
                "type":      "stream_end",
                "from":      IO_GATEWAY_ID,
                "timestamp": time.time(),
            }))
        except Exception:
            pass

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data     = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "command":
                        await handle_command(data)

                    elif msg_type == "chat":
                        content = (data.get("content") or "").strip()
                        if content and registry is not None:
                            async def _safe_route(c=content):
                                try:
                                    await _route_chat(c, ws_reply,
                                                      stream_fn=ws_stream_chunk,
                                                      stream_end_fn=ws_stream_end)
                                except Exception as exc:
                                    logger.error(f"[ws] chat error: {exc}", exc_info=True)
                                    try:
                                        await ws_reply(f"[error] {exc}")
                                        await ws_stream_end()
                                    except Exception:
                                        pass
                            asyncio.create_task(_safe_route())
                        elif content:
                            # No registry — tell the browser to use MQTT
                            await ws_reply("[system] Chat not available over WebSocket in this mode.")

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
    command  = cmd.get("command")
    agent_id = cmd.get("agent_id")
    if not command or not agent_id:
        return
    if command not in {"pause", "stop", "resume", "delete"}:
        return

    logger.info(f"[cmd] {command.upper()} -> {agent_id[:8]}")
    if not mqtt_client_ref:
        logger.warning("[cmd] No MQTT client available")
        return

    payload = json.dumps({"command": command, "sender": "monitor-dashboard", "timestamp": time.time()})
    try:
        await mqtt_client_ref.publish(f"agents/{agent_id}/commands", payload)
        add_log({"type": "command", "agent_id": agent_id, "command": command, "timestamp": time.time()})
        if command in ("stop", "pause", "resume"):
            state["agents"].get(agent_id, {})["state"] = (
                "stopped" if command == "stop" else
                "paused"  if command == "pause" else "running"
            )
            await broadcast({"type": "patch", "state": _snapshot()})
        elif command == "delete":
            state["agents"].pop(agent_id, None)
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
        metric   = parts[2]

        if metric == "status":
            update_agent(agent_id, "status", data)
            if isinstance(data, dict):
                if "name"      in data: state["agents"][agent_id]["name"]      = data["name"]
                if "state"     in data: state["agents"][agent_id]["state"]     = data["state"]
                if "protected" in data: state["agents"][agent_id]["protected"] = data["protected"]
            add_log({"type": "status", "agent_id": agent_id, "status": data, "timestamp": time.time()})

        elif metric == "heartbeat":
            update_agent(agent_id, "heartbeat", data)
            if isinstance(data, dict):
                ag = state["agents"][agent_id]
                ag["name"]  = data.get("name",      agent_id[:8])
                ag["cpu"]   = data.get("cpu",        0)
                ag["mem"]   = data.get("memory_mb",  0)
                ag["task"]  = data.get("task",       "idle")
                ag["state"] = data.get("state",      "unknown")
            logger.info(f"[MQTT] Heartbeat: {state['agents'][agent_id].get('name', agent_id[:8])}")

        elif metric == "metrics":
            update_agent(agent_id, "metrics", data)
            if isinstance(data, dict):
                state["agents"][agent_id]["messages_processed"] = data.get("messages_processed", 0)
                if "cost_usd" in data:
                    state["agents"][agent_id]["cost_usd"]      = data.get("cost_usd", 0.0)
                    state["agents"][agent_id]["input_tokens"]  = data.get("input_tokens", 0)
                    state["agents"][agent_id]["output_tokens"] = data.get("output_tokens", 0)

        elif metric == "logs":
            add_log({"type": "log", "agent_id": agent_id, "timestamp": time.time(),
                     **(data if isinstance(data, dict) else {})})
        elif metric == "spawned":
            add_log({"type": "spawned", "agent_id": agent_id, "timestamp": time.time(),
                     **(data if isinstance(data, dict) else {})})
        elif metric == "completed":
            update_agent(agent_id, "last_completed", data)
            add_log({"type": "completed", "agent_id": agent_id, "timestamp": time.time()})
        elif metric == "alert":
            if isinstance(data, dict):
                data["agent_id"] = agent_id
                data.setdefault("name", state["agents"].get(agent_id, {}).get("name", agent_id[:8]))
            state["alerts"].insert(0, data if isinstance(data, dict) else {"agent_id": agent_id})
            if len(state["alerts"]) > 50:
                state["alerts"].pop()
            name     = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            severity = data.get("severity", "warning") if isinstance(data, dict) else "warning"
            add_log({"type": "alert", "agent_id": agent_id, "name": name,
                     "message": f"{name} unresponsive ({severity})", "timestamp": time.time()})

        return {"type": "agent", "agent_id": agent_id, "metric": metric, "data": data}

    if parts[0] == "nodes" and len(parts) >= 3 and parts[2] == "heartbeat":
        node_name = parts[1]
        if isinstance(data, dict):
            state["nodes"][node_name] = {
                "node":      node_name,
                "agents":    data.get("agents", []),
                "last_seen": time.time(),
                "online":    True,
                "node_id":   data.get("node_id", ""),
            }
            logger.info(f"[MQTT] Node heartbeat: {node_name} | agents: {data.get('agents', [])}")
            return {"type": "node", "node_name": node_name, "data": data}

    return None


def _node_online(last_seen: float) -> bool:
    return (time.time() - last_seen) < 45


def _snapshot() -> dict:
    for nd in state["nodes"].values():
        nd["online"] = _node_online(nd.get("last_seen", 0))
    total_cost = sum(a.get("cost_usd", 0.0) for a in state["agents"].values())
    return {
        "agents":         list(state["agents"].values()),
        "nodes":          list(state["nodes"].values()),
        "alerts":         state["alerts"][:10],
        "log_feed":       state["log_feed"][:20],
        "system_health":  state["system_health"],
        "total_cost_usd": round(total_cost, 6),
    }


async def mqtt_listener():
    global mqtt_client_ref
    try:
        import aiomqtt
    except ImportError:
        logger.error("aiomqtt not installed: pip install aiomqtt")
        return

    logger.info(f"Connecting to MQTT {MQTT_BROKER}:{MQTT_PORT}...")
    while True:
        try:
            async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as client:
                mqtt_client_ref = client
                logger.info("MQTT connected.")

                if registry is not None:
                    await client.publish(
                        f"agents/{IO_GATEWAY_ID}/spawn",
                        json.dumps({
                            "agentId":   IO_GATEWAY_ID,
                            "agentName": IO_GATEWAY_ID,
                            "agentType": "gateway",
                            "timestamp": time.time(),
                        }),
                    )

                for topic in MQTT_TOPICS:
                    await client.subscribe(topic)

                async for message in client.messages:
                    topic   = str(message.topic)
                    payload = message.payload.decode(errors="replace")

                    if topic == "io/chat":
                        if registry is not None:
                            try:
                                asyncio.create_task(handle_chat_mqtt(json.loads(payload)))
                            except Exception as exc:
                                logger.error(f"[io/chat] error: {exc}")
                        continue

                    event = parse_topic(topic, payload)
                    if event:
                        metric    = event.get("metric", "")
                        log_event = None if metric == "heartbeat" else event
                        await broadcast({"type": "patch", "event": log_event, "state": _snapshot()})

        except Exception as e:
            mqtt_client_ref = None
            logger.warning(f"MQTT error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


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

_pkg  = Path(__file__).parent
_root = _pkg.parent

def _find_dir(*rel: str) -> Path:
    for base in (_pkg, _root):
        p = base.joinpath(*rel)
        if p.is_dir():
            return p
    return _pkg.joinpath(*rel)

FRONTEND_DIST   = _find_dir("static", "app")
FRONTEND_PUBLIC = _find_dir("frontend", "public")
DOCS_SITE       = _find_dir("static", "docs")


def _with_no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


async def index_handler(request):
    from aiohttp import web
    # Ingress support: HA sets X-Hassio-Ingress-Path
    ingress_path = request.headers.get("X-Hassio-Ingress-Path", "").rstrip("/")
    
    # Also handle favicon.svg if it's requested at root
    if request.path.endswith("favicon.svg"):
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    for candidate in [
        FRONTEND_DIST / "index.html",
        _find_dir("frontend") / "index.html",
        _pkg / "monitor.html",
        _root / "monitor.html",
    ]:
        if candidate.exists():
            if not ingress_path:
                return _with_no_cache(web.FileResponse(candidate))
            
            # Behind Ingress: We need to inject the base path so the JS can find the API
            content = candidate.read_text(encoding="utf-8")
            
            # 1. Inject a script to tell the JS what the ingress path is
            script = f"<script>window.WACTORZ_INGRESS_PATH = '{ingress_path}';</script>"
            content = content.replace("<head>", f"<head>{script}")
            
            # 2. Fix relative assets by adding a <base> tag
            # Note: This might break some SPAs if not handled carefully, 
            # but usually it helps with static assets.
            base_tag = f'<base href="{ingress_path}/">'
            content = content.replace("<head>", f"<head>{base_tag}")
            
            return _with_no_cache(web.Response(text=content, content_type="text/html"))
    raise web.HTTPNotFound()


async def static_handler(request):
    from aiohttp import web
    rel = request.match_info["path"]
    
    # Special case for favicon if it's requested at root
    if rel == "favicon.svg":
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    ingress_path = request.headers.get("X-Hassio-Ingress-Path", "").rstrip("/")

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
                    # FORCE the WebSocket to use port 8888 instead of HA's 8123
                    content = content.replace('"ws://localhost:9001"', f'"ws://{request.host.split(":")[0]}:8888/mqtt"')
                    content = content.replace('`ws://${location.host}/ws`', f'`ws://${{location.hostname}}:8888/ws`')
                    content = content.replace('`ws://${location.host}/mqtt`', f'`ws://${{location.hostname}}:8888/mqtt`')
                    
                    return _with_no_cache(web.Response(text=content, content_type="application/javascript"))
                
                return _with_no_cache(web.FileResponse(candidate))
        except Exception:
            pass
    raise web.HTTPNotFound()


async def docs_handler(request):
    from aiohttp import web
    if not DOCS_SITE.is_dir():
        raise web.HTTPNotFound(reason="Docs not built — run: python3 scripts/build_docs.py  (or: make docs-build)")
    rel = request.match_info.get("path", "") or "index.html"
    if not rel or rel.endswith("/"):
        rel += "index.html"
    root      = DOCS_SITE.resolve()
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


async def _bridge_mqtt_tcp(client_ws, broker: str, port: int) -> None:
    from aiohttp import WSMsgType
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(broker, port), timeout=3)
    except Exception as exc:
        logger.warning("MQTT TCP bridge: cannot connect to %s:%s — %s", broker, port, exc)
        return

    async def ws_to_tcp():
        try:
            async for msg in client_ws:
                if msg.type == WSMsgType.BINARY:
                    writer.write(msg.data)
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
    from aiohttp import web, WSMsgType

    raw_proto = request.headers.get("Sec-WebSocket-Protocol", "")
    protocols = [p.strip() for p in raw_proto.split(",") if p.strip()]
    client_ws = web.WebSocketResponse(protocols=protocols)
    await client_ws.prepare(request)

    upstream_url = f"ws://{MQTT_BROKER}:{MQTT_WS_PORT}/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                upstream_url,
                protocols=protocols,
                headers={"Sec-WebSocket-Protocol": ",".join(protocols)} if protocols else {},
                timeout=aiohttp.ClientTimeout(connect=2),
            ) as upstream_ws:
                async def forward(src, dst):
                    async for msg in src:
                        if msg.type == WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                        elif msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break
                await asyncio.gather(forward(client_ws, upstream_ws), forward(upstream_ws, client_ws))
        return client_ws
    except Exception as exc:
        logger.info("MQTT WS proxy unavailable (%s), falling back to TCP bridge", exc)

    await _bridge_mqtt_tcp(client_ws, MQTT_BROKER, MQTT_PORT)
    return client_ws


def _actor_payload(ag: dict) -> dict:
    return {
        "id":                ag.get("agent_id", ""),
        "name":              ag.get("name", ""),
        "state":             ag.get("state", "unknown"),
        "protected":         ag.get("protected", False),
        "cpu":               ag.get("cpu"),
        "mem":               ag.get("mem"),
        "task":              ag.get("task"),
        "messagesProcessed": ag.get("messages_processed"),
        "costUsd":           ag.get("cost_usd"),
    }


async def actors_handler(request):
    from aiohttp import web
    # Prefer the live registry (injected by cli.py) — actor objects carry the
    # authoritative protected flag.  Fall back to MQTT-derived state dict when
    # the registry is unavailable (standalone monitor_server mode).
    if registry is not None:
        result = []
        for actor in registry.all_actors():
            ag = state["agents"].get(actor.actor_id, {})
            result.append({
                "id":                actor.actor_id,
                "name":              actor.name,
                "state":             ag.get("state", "unknown"),
                "protected":         bool(getattr(actor, "protected", False)),
                "cpu":               ag.get("cpu"),
                "mem":               ag.get("mem"),
                "task":              ag.get("task"),
                "messagesProcessed": ag.get("messages_processed"),
                "costUsd":           ag.get("cost_usd"),
            })
        return web.json_response(result)
    return web.json_response([_actor_payload(ag) for ag in state["agents"].values()])


async def actor_handler(request):
    from aiohttp import web
    actor_id = request.match_info["actor_id"]
    ag = state["agents"].get(actor_id)
    if ag is None:
        return web.json_response({"error": "actor not found"}, status=404)
    return web.json_response(_actor_payload(ag))


async def config_handler(request):
    """Expose non-secret runtime config so the frontend can seed its defaults."""
    from aiohttp import web
    from .config import CONFIG

    # Ingress support: HA sets X-Hassio-Ingress-Path
    ingress_path = request.headers.get("X-Hassio-Ingress-Path", "")
    
    host = request.host
    protocol = "wss" if request.secure else "ws"
    
    # We build the URL relative to the current ingress path if present
    base_url = f"{protocol}://{host}{ingress_path}"

    return web.json_response({
        "ha": {
            "url":   CONFIG.ha_url,
            "token": CONFIG.ha_token,
        },
        "fuseki": {
            "url":     CONFIG.fuseki_url,
            "dataset": CONFIG.fuseki_dataset,
        },
        "mqtt": {
            "host": MQTT_BROKER,
            "port": MQTT_PORT,
            "url":  f"{base_url}/mqtt",  # Proxy-aware WebSocket URL
        },
        "llm": {
            "provider": CONFIG.llm_provider,
            "model":    CONFIG.llm_model,
        },
        "weather": {
            "defaultLocation": CONFIG.weather_default_location,
        },
    })


# ── Entry point ────────────────────────────────────────────────────────────

async def main(exit_on_failure: bool = False):
    from aiohttp import web

    # ... (startup checks remain same) ...
    mqtt_ok = await _check_mqtt()
    port_ok = await _check_ws_port()

    if not mqtt_ok or not port_ok:
        msg = []
        if not mqtt_ok: msg.append(f"MQTT broker unreachable ({MQTT_BROKER}:{MQTT_PORT})")
        if not port_ok: msg.append(f"Port {WS_PORT} already in use")
        logger.error(f"[startup] Cannot start: {'; '.join(msg)}")
        if exit_on_failure:
            raise SystemExit(1)
        return

    app = web.Application()
    app.router.add_get("/",                      index_handler)
    app.router.add_get("/ws",                    ws_handler)
    app.router.add_get("/mqtt",                  mqtt_proxy_handler)
    
    # Add both /api and non-api versions to satisfy the frontend's different fetch patterns
    app.router.add_get("/api/actors",            actors_handler)
    app.router.add_get("/actors",                actors_handler)
    app.router.add_get("/api/actors/{actor_id}", actor_handler)
    app.router.add_get("/actors/{actor_id}",     actor_handler)
    
    app.router.add_get("/api/config",            config_handler)
    app.router.add_get("/config",                config_handler)
    app.router.add_get("/favicon.svg",           index_handler)
    from .fuseki_proxy import fuseki_proxy_handler
    app.router.add_post("/api/fuseki/{dataset}/sparql",  fuseki_proxy_handler)
    app.router.add_post("/api/fuseki/{dataset}/update",  fuseki_proxy_handler)
    app.router.add_get("/docs",  lambda r: web.HTTPFound("/docs/"))
    app.router.add_get("/docs/",             docs_handler)
    app.router.add_get("/docs/{path:.+}",    docs_handler)
    app.router.add_get("/{path:.+}",         static_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WS_PORT)
    await site.start()
    logger.info(f"Monitor  → http://localhost:{WS_PORT}/  [chat: {_chat_mode()}]")
    if DOCS_SITE.is_dir():
        logger.info(f"Docs     → http://localhost:{WS_PORT}/docs/")

    await mqtt_listener()


def cli_main() -> None:
    asyncio.run(main(exit_on_failure=True))


if __name__ == "__main__":
    import argparse, os
    parser = argparse.ArgumentParser(description="Wactorz Monitor Server")
    parser.add_argument("--broker",       default=os.getenv("WACTORZ_BROKER", "localhost"))
    parser.add_argument("--mqtt-port",    type=int, default=1883)
    parser.add_argument("--mqtt-ws-port", type=int, default=int(os.getenv("MQTT_WS_PORT", "9001")))
    parser.add_argument("--ws-port",      type=int, default=int(os.getenv("MONITOR_PORT", "8888")))
    args = parser.parse_args()

    thismodule = sys.modules[__name__]
    thismodule.MQTT_BROKER  = args.broker
    thismodule.MQTT_PORT    = args.mqtt_port
    thismodule.MQTT_WS_PORT = args.mqtt_ws_port
    thismodule.WS_PORT      = args.ws_port

    cli_main()
