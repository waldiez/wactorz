"""WebSocket transport for the dashboard.

Owns the ``/ws`` endpoint: pushes state snapshots and live events to connected
browsers (``broadcast``) and dispatches the commands they send back
(``handle_command``). The browser never talks to MQTT — everything it sees
arrives over this socket.
"""

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import WSMsgType, web

from . import chat, events, lifecycle, runtime

logger = logging.getLogger(__name__)


async def broadcast(msg: dict[str, Any]) -> None:
    """Broadcast a message to all connected clients."""
    if not runtime.ws_clients:
        return
    payload = json.dumps(msg)
    dead = set()
    for ws in list(runtime.ws_clients):
        try:
            await ws.send_str(payload)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("[broadcast] WS send failed: %s", e)
            dead.add(ws)
    runtime.ws_clients.difference_update(dead)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle websocket connection."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    runtime.ws_clients.add(ws)
    logger.info("WebSocket client connected. Total: %d", len(runtime.ws_clients))

    # Send initial state
    await ws.send_str(json.dumps({"type": "full_snapshot", "state": events.snapshot()}))

    # Advertise chat mode so the frontend knows where to send messages
    await ws.send_str(json.dumps({"type": "config", "chat.chat_mode": chat.chat_mode()}))

    # Current server↔broker state so the "live" badge is right immediately on load.
    await ws.send_str(json.dumps({"type": "mqtt_status", "connected": runtime.mqtt_connected}))

    # Per-connection accumulator for streamed assistant replies.
    # We only persist once at stream_end so chat_log gets one row per turn
    # with the full content, not a row per chunk.
    _stream_buffer: list[str] = []

    # The agent the current turn is addressed to. Reply frames and chat_log are
    # attributed to it instead of the generic "io-gateway" transport id, so the
    # UI (and persisted/reloaded history) shows the agent that actually answered
    # rather than the gateway. Set per turn from the user's @mention before
    # routing; defaults to the gateway id until a chat turn arrives.
    _reply_from = {"name": runtime.IO_GATEWAY_ID}

    def _persist_chat(role: str, content: str, agent_name: str = "main") -> None:
        """Best-effort write to chat_log. Never raises into the WS path."""
        if runtime.db is None or not content:
            return
        try:
            runtime.db.write_chat_log(
                ts=time.time(),
                agent_name=agent_name,
                role=role,
                content=content,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[ws] chat_log write failed: %s", exc)

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
        except Exception:  # pylint: disable=broad-exception-caught
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
        except Exception:  # pylint: disable=broad-exception-caught
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
        except Exception:  # pylint: disable=broad-exception-caught
            # Even if the send_str failed, flush anything we accumulated
            # so the user's session isn't lost on a transient ws hiccup.
            if _stream_buffer:
                full = "".join(_stream_buffer)
                _stream_buffer.clear()
                _persist_chat("assistant", full, _reply_from["name"])

    async def _relay_chat_to_ioagent(content: str) -> None:
        """Standalone monitor (no in-process registry): forward the browser's turn
        to the IOAgent over MQTT `io/chat`. The reply returns on
        `agents/{id}/chat`, which this process relays to the browser over /ws.

        Only used in the legacy `wactorz-monitor` (registry-None) mode; remove it
        when that entry point is retired from pyproject scripts.
        """
        if not runtime.mqtt_client_ref:
            await ws_reply("[system] Chat unavailable — no broker connection.")
            return
        who = "main" if content.startswith("/") else chat.parse_mention(content)[0]
        _persist_chat("user", content, who)
        await runtime.mqtt_client_ref.publish(
            "io/chat",
            json.dumps({"content": content, "from": "user", "timestamp": time.time()}),
            qos=1,
        )

    # pylint: disable=broad-exception-caught
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
                        if content and runtime.registry is not None:
                            # Attribute the whole turn to the agent it addresses
                            # (slash commands and un-mentioned text default to
                            # "main", matching chat.route_chat's own resolution) so the
                            # reply frames and chat_log group under that agent
                            # instead of the io-gateway transport id.
                            _reply_from["name"] = (
                                "main"
                                if content.startswith("/")
                                else chat.parse_mention(content)[0]
                            )
                            # Persist the user's turn first so chat_log has the
                            # request even if the assistant reply errors out.
                            _persist_chat("user", content, _reply_from["name"])

                            async def _safe_route(c=content):
                                try:
                                    await chat.route_chat(
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
                                    logger.error("[ws] chat error: %s", exc, exc_info=True)
                                    try:
                                        await ws_reply(f"[error] {exc}")
                                        await ws_stream_end()
                                    except Exception:
                                        pass

                            chat.track_chat_task(asyncio.create_task(_safe_route()))
                        elif content:
                            await _relay_chat_to_ioagent(content)

                except Exception as e:
                    logger.warning("[ws] Bad message: %s", e)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        runtime.ws_clients.discard(ws)
        logger.info("WebSocket client disconnected. Total: %d", len(runtime.ws_clients))
    return ws


# ── Browser commands ───────────────────────────────────────────────────────


async def handle_command(cmd: dict[str, Any]) -> None:
    """Handle a command message."""
    command = cmd.get("command")
    agent_id = cmd.get("agent_id")
    if not command or not agent_id:
        return
    if command not in {"pause", "stop", "resume", "delete"}:
        return

    msg = f"[cmd] {command.upper()} -> {agent_id[:8]}"
    logger.info(msg)
    if not runtime.mqtt_client_ref:
        logger.warning("[cmd] No MQTT client available")
        return

    payload = json.dumps(
        {"command": command, "sender": "monitor-dashboard", "timestamp": time.time()}
    )
    try:
        await runtime.mqtt_client_ref.publish(f"agents/{agent_id}/commands", payload)
        events.add_log(
            {"type": "command", "agent_id": agent_id, "command": command, "timestamp": time.time()}
        )
        if command in ("stop", "pause", "resume"):
            runtime.state["agents"].get(agent_id, {})["state"] = (
                "stopped" if command == "stop" else "paused" if command == "pause" else "running"
            )
            await broadcast({"type": "patch", "state": events.snapshot()})
        elif command == "delete":
            await lifecycle.delete_agent(agent_id)
            await broadcast(
                {"type": "lifecycle.delete_agent", "agent_id": agent_id, "state": events.snapshot()}
            )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("[cmd] Publish failed: %s", e)
