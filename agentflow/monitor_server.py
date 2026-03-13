"""
AgentFlow Monitor - MQTT <-> WebSocket Bridge
Supports: receiving agent state, sending pause/stop/resume/delete commands
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

MQTT_BROKER    = "localhost"
MQTT_PORT      = 1883
MQTT_WS_PORT   = 9001   # Mosquitto WebSocket listener (proxied at /mqtt)
WS_PORT        = 8888
MQTT_TOPICS    = ["agents/#", "system/#", "nodes/#"]

state = {
    "agents":        {},
    "nodes":         {},   # node_name -> {node, agents, last_seen, online}
    "alerts":        [],
    "system_health": {},
    "log_feed":      [],
}

ws_clients: set = set()

# Global MQTT client ref for publishing commands
mqtt_client_ref = None


def update_agent(agent_id: str, key: str, data):
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


async def handle_command(cmd: dict):
    """
    Handle a command sent from the browser dashboard.
    Expected format: {"command": "pause"|"stop"|"resume"|"delete", "agent_id": "..."}
    Publishes to agents/{agent_id}/commands topic which the agent subscribes to.
    """
    global mqtt_client_ref
    command  = cmd.get("command")
    agent_id = cmd.get("agent_id")

    if not command or not agent_id:
        logger.warning(f"[cmd] Invalid command: {cmd}")
        return

    valid = {"pause", "stop", "resume", "delete"}
    if command not in valid:
        logger.warning(f"[cmd] Unknown command: {command}")
        return

    logger.info(f"[cmd] {command.upper()} -> {agent_id[:8]}")

    # Publish command to the agent's command topic
    if mqtt_client_ref:
        payload = json.dumps({
            "command":   command,
            "sender":    "monitor-dashboard",
            "timestamp": time.time(),
        })
        try:
            await mqtt_client_ref.publish(f"agents/{agent_id}/commands", payload)
            add_log({
                "type":      "command",
                "agent_id":  agent_id,
                "command":   command,
                "timestamp": time.time(),
            })
            # Optimistically update state for instant UI feedback
            if command == "stop":
                state["agents"].get(agent_id, {})["state"] = "stopped"
            elif command == "pause":
                state["agents"].get(agent_id, {})["state"] = "paused"
            elif command == "resume":
                state["agents"].get(agent_id, {})["state"] = "running"
            elif command == "delete":
                state["agents"].pop(agent_id, None)
                # Tell browser to explicitly remove this agent card
                await broadcast({
                    "type":     "delete_agent",
                    "agent_id": agent_id,
                    "state":    _snapshot(),
                })
                return  # already broadcasted

            await broadcast({"type": "patch", "state": _snapshot()})
        except Exception as e:
            logger.error(f"[cmd] Publish failed: {e}")
    else:
        logger.warning("[cmd] No MQTT client available")


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
            # Just update the alerts list — don't log separately,
            # the agents/{id}/alert topic already creates a log entry
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
                if "name" in data:
                    state["agents"][agent_id]["name"]  = data.get("name", agent_id[:8])
                if "state" in data:
                    state["agents"][agent_id]["state"] = data.get("state", "unknown")
            add_log({"type": "status", "agent_id": agent_id,
                     "status": data, "timestamp": time.time()})

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
                # Accumulate cost and tokens across all agents
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
            # Enrich with known name if not in payload
            if isinstance(data, dict):
                data["agent_id"] = agent_id
                data.setdefault("name", state["agents"].get(agent_id, {}).get("name", agent_id[:8]))
            state["alerts"].insert(0, data if isinstance(data, dict) else {"agent_id": agent_id})
            if len(state["alerts"]) > 50:
                state["alerts"].pop()
            name = state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            severity = data.get("severity", "warning") if isinstance(data, dict) else "warning"
            add_log({"type": "alert", "agent_id": agent_id, "name": name,
                     "message": f"{name} unresponsive ({severity})",
                     "timestamp": time.time()})

        return {"type": "agent", "agent_id": agent_id, "metric": metric, "data": data}

    if parts[0] == "nodes" and len(parts) >= 3:
        node_name = parts[1]
        metric    = parts[2]
        if metric == "heartbeat" and isinstance(data, dict):
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
    return (time.time() - last_seen) < 45   # 3 missed heartbeats @ 15s = offline


def _snapshot() -> dict:
    # Mark nodes offline if heartbeat is too old
    for nd in state["nodes"].values():
        nd["online"] = _node_online(nd.get("last_seen", 0))
    total_cost = sum(a.get("cost_usd", 0.0) for a in state["agents"].values())
    return {
        "agents":        list(state["agents"].values()),
        "nodes":         list(state["nodes"].values()),
        "alerts":        state["alerts"][:10],
        "log_feed":      state["log_feed"][:20],
        "system_health": state["system_health"],
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
                for topic in MQTT_TOPICS:
                    await client.subscribe(topic)
                    logger.info(f"  Subscribed: {topic}")
                async for message in client.messages:
                    topic   = str(message.topic)
                    payload = message.payload.decode(errors="replace")
                    event   = parse_topic(topic, payload)
                    if event:
                        metric = event.get("metric", "")
                        # Heartbeats: broadcast state update (for CPU/mem bars) but
                        # pass event=None so browser doesn't log it
                        # Metrics: always broadcast so message counters update immediately
                        log_event = None if metric == "heartbeat" else event
                        await broadcast({
                            "type":  "patch",
                            "event": log_event,
                            "state": _snapshot(),
                        })
        except Exception as e:
            mqtt_client_ref = None
            logger.warning(f"MQTT error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def ws_handler(request):
    from aiohttp import web, WSMsgType
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    logger.info(f"WebSocket client connected. Total: {len(ws_clients)}")

    snap = _snapshot()
    logger.info(f"Sending snapshot: {len(snap['agents'])} agents")
    # Use "full_snapshot" so browser knows to replace its entire state
    await ws.send_str(json.dumps({"type": "full_snapshot", "state": snap}))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "command":
                        await handle_command(data)
                except Exception as e:
                    logger.warning(f"[ws] Bad message: {e}")
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        ws_clients.discard(ws)
        logger.info(f"WebSocket client disconnected. Total: {len(ws_clients)}")
    return ws


# Resolve frontend paths: when installed the dist lives inside the package;
# when running from a dev/editable checkout fall back to the project root.
_pkg = Path(__file__).parent
_root = _pkg.parent

def _find_dir(*rel: str) -> Path:
    """Return the first existing candidate, or the package-relative path."""
    for base in (_pkg, _root):
        p = base.joinpath(*rel)
        if p.is_dir():
            return p
    return _pkg.joinpath(*rel)   # non-existent but canonical installed path

FRONTEND_DIST   = _find_dir("frontend", "dist")
FRONTEND_PUBLIC = _find_dir("frontend", "public")
DOCS_SITE       = _find_dir("docs_site")


async def index_handler(request):
    from aiohttp import web
    # Priority: Vite-built dist → source index → standalone monitor.html
    for candidate in [
        FRONTEND_DIST / "index.html",
        _find_dir("frontend") / "index.html",
        _pkg / "monitor.html",
        _root / "monitor.html",
    ]:
        if candidate.exists():
            return web.FileResponse(candidate)
    raise web.HTTPNotFound()


async def static_handler(request):
    """Serve files from frontend/dist/ then frontend/public/ as fallback."""
    from aiohttp import web
    rel = request.match_info["path"]
    for base in [FRONTEND_DIST, FRONTEND_PUBLIC]:
        candidate = base / rel
        try:
            candidate = candidate.resolve()
            if candidate.is_file() and str(candidate).startswith(str(base.resolve())):
                return web.FileResponse(candidate)
        except Exception:
            pass
    raise web.HTTPNotFound()


async def docs_handler(request):
    """Serve the built docs site from /docs/."""
    from aiohttp import web
    if not DOCS_SITE.is_dir():
        raise web.HTTPNotFound(reason="Docs not built — run: make docs-build")
    rel = request.match_info.get("path", "") or "index.html"
    if not rel or rel.endswith("/"):
        rel = rel + "index.html"
    root = DOCS_SITE.resolve()
    candidate = (DOCS_SITE / rel).resolve()
    try:
        if candidate.is_file() and str(candidate).startswith(str(root)):
            return web.FileResponse(candidate)
        # If index.html is missing (e.g. rustdoc root), try generating one on-the-fly
        if rel.endswith("index.html") and not candidate.exists():
            parent = candidate.parent
            if parent.is_dir():
                # redirect to first sub-index found
                for sub in sorted(parent.iterdir()):
                    if sub.is_dir() and (sub / "index.html").exists():
                        location = request.path.rstrip("/") + f"/{sub.name}/index.html"
                        raise web.HTTPFound(location)
    except web.HTTPFound:
        raise
    except Exception:
        pass
    raise web.HTTPNotFound()


async def _bridge_mqtt_tcp(client_ws, broker: str, port: int) -> None:
    """Bridge browser MQTT-over-WebSocket ↔ Mosquitto raw TCP (port 1883).

    MQTT-over-WebSocket is just MQTT binary frames sent as WS binary messages.
    Plain TCP MQTT uses the same binary format without any WS framing, so we
    can bridge the two by stripping/adding WS framing on the fly.
    """
    from aiohttp import WSMsgType
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(broker, port), timeout=3
        )
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
    """Proxy /mqtt WebSocket → Mosquitto WS (port 9001), falling back to TCP (port 1883)."""
    import aiohttp
    from aiohttp import web, WSMsgType

    # Preserve MQTT sub-protocol negotiation so mqtt.js works correctly.
    raw_proto = request.headers.get("Sec-WebSocket-Protocol", "")
    protocols = [p.strip() for p in raw_proto.split(",") if p.strip()]

    client_ws = web.WebSocketResponse(protocols=protocols)
    await client_ws.prepare(request)

    # ── Try Mosquitto WebSocket listener first (port 9001) ────────────────────
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

                await asyncio.gather(
                    forward(client_ws, upstream_ws),
                    forward(upstream_ws, client_ws),
                )
        return client_ws
    except Exception as exc:
        logger.info("MQTT WS proxy (%s) unavailable (%s), falling back to TCP bridge on port %s",
                    upstream_url, exc, MQTT_PORT)

    # ── Fall back: bridge WS ↔ raw TCP MQTT (port 1883) ──────────────────────
    await _bridge_mqtt_tcp(client_ws, MQTT_BROKER, MQTT_PORT)
    return client_ws


async def actors_handler(request):
    """REST endpoint: GET /api/actors — returns current agent list.

    Maps the in-memory MQTT-derived state to the AgentInfo shape the
    frontend expects (``id`` instead of ``agent_id``, camelCase fields).
    """
    from aiohttp import web
    agents = []
    for ag in state["agents"].values():
        agents.append({
            "id":       ag.get("agent_id", ""),
            "name":     ag.get("name", ""),
            "state":    ag.get("state", "unknown"),
            "protected": ag.get("protected", False),
            "cpu":      ag.get("cpu"),
            "mem":      ag.get("mem"),
            "task":     ag.get("task"),
            "messagesProcessed": ag.get("messages_processed"),
            "costUsd":  ag.get("cost_usd"),
        })
    return web.json_response(agents)


async def main():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/",             index_handler)
    app.router.add_get("/ws",           ws_handler)
    app.router.add_get("/mqtt",         mqtt_proxy_handler)
    app.router.add_get("/api/actors",   actors_handler)
    app.router.add_get("/docs",         lambda r: __import__("aiohttp").web.HTTPFound("/docs/"))
    app.router.add_get("/docs/",        docs_handler)
    app.router.add_get("/docs/{path:.+}", docs_handler)
    app.router.add_get("/{path:.+}",    static_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WS_PORT)
    await site.start()
    docs_note = f"  docs  → http://localhost:{WS_PORT}/docs/" if DOCS_SITE.is_dir() else ""
    logger.info(f"Monitor  → http://localhost:{WS_PORT}/{docs_note}")
    await mqtt_listener()

def cli_main() -> None:
    """Entry point for the `agentflow-monitor` console script."""
    asyncio.run(main())

if __name__ == "__main__":
    import argparse, os
    parser = argparse.ArgumentParser(description="AgentFlow Monitor Server")
    parser.add_argument("--broker",    default=os.getenv("AGENTFLOW_BROKER", "localhost"),
                        help="MQTT broker host (default: localhost or $AGENTFLOW_BROKER)")
    parser.add_argument("--mqtt-port",    type=int, default=1883)
    parser.add_argument("--mqtt-ws-port", type=int,
                        default=int(os.getenv("MQTT_WS_PORT", "9001")),
                        help="Mosquitto WebSocket port (default: 9001 or $MQTT_WS_PORT)")
    parser.add_argument("--ws-port",      type=int, default=8888)
    args = parser.parse_args()

    # Override module-level config before asyncio.run so mqtt_listener picks them up
    MQTT_BROKER  = args.broker
    MQTT_PORT    = args.mqtt_port
    MQTT_WS_PORT = args.mqtt_ws_port
    WS_PORT      = args.ws_port

    import sys
    # Patch the module globals directly so all functions see the updated values
    thismodule = sys.modules[__name__]
    thismodule.MQTT_BROKER  = args.broker
    thismodule.MQTT_PORT    = args.mqtt_port
    thismodule.MQTT_WS_PORT = args.mqtt_ws_port
    thismodule.WS_PORT      = args.ws_port

    cli_main()
