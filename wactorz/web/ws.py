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


# How many frames a client may fall behind before it is resynchronised rather
# than fed a backlog. Large enough that an ordinary hiccup rides through; small
# enough that a stuck socket cannot pin unbounded memory.
_CLIENT_QUEUE_DEPTH = 256


class Channel:
    """One client's outbound queue and the task that drains it.

    Broadcasting used to await ``send_str`` for each client in turn, from inside
    the broker message loop — so a browser on a slow link stalled *ingest* for
    every other client and for MQTT itself. Handing each client its own queue and
    writer means a slow one falls behind alone.
    """

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_CLIENT_QUEUE_DEPTH)
        self._writer = asyncio.create_task(self._drain())
        self.dropped = 0

    def send(self, payload: str) -> None:
        """Queue a frame. Never blocks, never raises.

        On overflow the backlog is discarded and a resync marker queued in its
        place. Dropping frames silently would leave that client's state quietly
        wrong — it would keep applying patches to a base it never received — so
        it is told to start again instead.
        """
        try:
            self._queue.put_nowait(payload)
            return
        except asyncio.QueueFull:
            pass

        self.dropped += 1
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - racing drain
                break
        try:
            self._queue.put_nowait(None)  # resync marker
        except asyncio.QueueFull:  # pragma: no cover - drained above
            pass

    async def _drain(self) -> None:
        while True:
            payload = await self._queue.get()
            if payload is None:
                logger.warning(
                    "[broadcast] client fell behind (%d drop events); resynchronising",
                    self.dropped,
                )
                try:
                    payload = json.dumps({"type": "full_snapshot", "state": events.snapshot()})
                except Exception as exc:
                    # Building the snapshot reads the database, so it can fail.
                    # Skip this resync rather than letting the writer die: a dead
                    # writer is a client that silently stops updating.
                    logger.warning("[broadcast] could not build resync snapshot: %s", exc)
                    continue
            try:
                await self.ws.send_str(payload)
            except Exception as exc:
                logger.warning("[broadcast] WS send failed: %s", exc)
                await self._retire()
                return

    async def _retire(self) -> None:
        """Stop serving this client, and close the socket so it knows.

        Leaving the socket open would give the browser a working command channel
        with no updates arriving — it would look connected while showing stale
        state. Closing ends the handler's receive loop now instead of whenever
        the client happens to notice.
        """
        runtime.ws_clients.discard(self)
        try:
            await self.ws.close()
        except Exception as exc:
            logger.debug("[broadcast] closing a failed socket raised: %s", exc)

    async def close(self) -> None:
        """Stop the writer. Never raises into the caller's cleanup path."""
        self._writer.cancel()
        try:
            await self._writer
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # The writer had already died of something else. Report it rather
            # than re-raising from a caller's ``finally``, where it would mask
            # whatever actually brought the connection down.
            logger.warning("[broadcast] writer task ended in error: %s", exc)


async def broadcast(msg: dict[str, Any]) -> None:
    """Hand a message to every connected client's queue.

    Returns as soon as the frames are queued: no client's link speed can delay
    the caller, which is the broker message loop.
    """
    if not runtime.ws_clients:
        return
    payload = json.dumps(msg)
    for channel in list(runtime.ws_clients):
        channel.send(payload)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle websocket connection."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    channel = Channel(ws)
    runtime.ws_clients.add(channel)
    logger.info("WebSocket client connected. Total: %d", len(runtime.ws_clients))

    # Send initial state
    await ws.send_str(json.dumps({"type": "full_snapshot", "state": events.snapshot()}))

    # Advertise chat mode so the frontend knows where to send messages
    await ws.send_str(json.dumps({"type": "config", "chat.chat_mode": "direct_ws"}))

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
        except Exception as exc:
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
                            await ws_reply("[system] Chat unavailable — no actor registry.")

                except Exception as e:
                    logger.warning("[ws] Bad message: %s", e)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        runtime.ws_clients.discard(channel)
        await channel.close()
        logger.info("WebSocket client disconnected. Total: %d", len(runtime.ws_clients))
    return ws


# ── Browser commands ───────────────────────────────────────────────────────


async def handle_command(cmd: dict[str, Any]) -> None:
    """Handle a command message."""
    command = cmd.get("command")
    agent_id = cmd.get("agent_id")
    if not command or not agent_id:
        return
    if command not in {"start", "pause", "stop", "resume", "delete"}:
        return

    logger.info("[cmd] %s -> %s", command.upper(), agent_id[:8])
    try:
        if command == "delete":
            # delete_agent has its own routing: main's spawn registry, then the
            # local actor, then the broker for agents on other nodes.
            await lifecycle.delete_agent(agent_id)
            events.add_log(
                {
                    "type": "command",
                    "agent_id": agent_id,
                    "command": command,
                    "timestamp": time.time(),
                }
            )
            await broadcast(
                {"type": "lifecycle.delete_agent", "agent_id": agent_id, "state": events.snapshot()}
            )
            return

        if not await lifecycle.dispatch_command(agent_id, command, "monitor-dashboard"):
            # Reflecting the new state here regardless would leave the browser
            # showing an agent as paused that is still running, with nothing to
            # correct it until the next heartbeat.
            return
        events.add_log(
            {"type": "command", "agent_id": agent_id, "command": command, "timestamp": time.time()}
        )
        runtime.state["agents"].get(agent_id, {})["state"] = (
            "stopped" if command == "stop" else "paused" if command == "pause" else "running"
        )  # start and resume both end up running
        await broadcast({"type": "patch", "state": events.snapshot()})
    except Exception as exc:
        logger.error("[cmd] %s failed: %s", command, exc)
