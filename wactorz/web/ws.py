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
from collections.abc import Callable
from typing import Any

from aiohttp import WSMsgType, web

from ..core.persistence import chat_turn_recorded
from ..ext.stt import service_uri, streaming
from ..monitoring.log_redaction import redact
from . import chat, events, lifecycle, origins, runtime, uploads

logger = logging.getLogger(__name__)
#: The conversation, on the console. Its own name rather than this module's:
#: the server quiets `wactorz.web` to warnings, and what was asked and answered
#: is what someone watching the console most wants to follow.
chat_logger = logging.getLogger("wactorz.chat")
#: How much of a turn the console shows. The whole turn is in chat_log.
CHAT_LOG_PREVIEW_CHARS = 500

#: Seconds between server pings on an open socket.
#:
#: Without one, a connection dropped without a close frame — a laptop lid, a
#: NAT timeout, a dead router — stays in `ws_clients` forever: the server keeps
#: queueing broadcasts to a socket nobody is reading, and the browser is not
#: told to reconnect. The ping is what turns that into a detected close.
#: Answered by the browser's own protocol handling, so no client code is
#: involved.
HEARTBEAT_SECONDS = 30.0


# How many frames a client may fall behind before it is resynchronised rather
# than fed a backlog. Large enough that an ordinary hiccup rides through; small
# enough that a stuck socket cannot pin unbounded memory.
_CLIENT_QUEUE_DEPTH = 256

#: How long a closing channel waits for a frame that is already on its way out.
#: Long enough for the socket to finish compressing it, which is the part that
#: must not be abandoned. A write that is waiting for a client that has stopped
#: reading can be cancelled safely, and is, once this has passed.
CLOSE_WAIT_S = 0.5


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
        #: Whether the writer is inside a write to the socket now, and whether
        #: the channel has been asked to stop. See :meth:`close`.
        self._sending = False
        self._closing = False

    def send(self, payload: str) -> None:
        """Queue a frame. Never blocks, never raises.

        On overflow the backlog is discarded and a resync marker queued in its
        place. Dropping frames silently would leave that client's state quietly
        wrong — it would keep applying patches to a base it never received — so
        it is told to start again instead.
        """
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass
        else:
            return

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
            self._sending = True
            try:
                await self.ws.send_str(payload)
            except Exception as exc:
                if self._closing:
                    # The client went while this frame was being written to it,
                    # which is how a write to a closing socket ends.
                    logger.debug("[broadcast] a frame to a closing socket was not sent: %s", exc)
                    return
                logger.warning("[broadcast] WS send failed: %s", exc)
                await self._retire()
                return
            finally:
                self._sending = False
            if self._closing:
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
        """Stop the writer. Never raises into the caller's cleanup path.

        A frame that is being written is given a moment to end before the writer
        is cancelled. The write of a compressed frame runs in a task of the
        socket's own, shielded from cancellation: cancelled from here it goes
        on alone, fails against the closing socket, and has nobody left to take
        its error, which the event loop then logs as one of ours with a
        traceback. Left to end, the write fails into the writer, which expects
        that.
        """
        self._closing = True
        if self._sending and not self._writer.done():
            await asyncio.wait({self._writer}, timeout=CLOSE_WAIT_S)
        self._writer.cancel()
        # gather rather than a bare await: it hands back the writer's own
        # CancelledError as a value, so it can be ignored without also
        # absorbing a cancellation aimed at whoever called close().
        (outcome,) = await asyncio.gather(self._writer, return_exceptions=True)
        # CancelledError is a BaseException, so this catches a real crash only.
        if isinstance(outcome, Exception):
            # The writer had already died of something else. Report it rather
            # than re-raising from a caller's ``finally``, where it would mask
            # whatever actually brought the connection down.
            logger.warning("[broadcast] writer task ended in error: %s", outcome)


#: Backstop interval for the headline totals. Long, because the `chat` frame
#: carries them as soon as an agent replies; this only covers spend that
#: produces no chat — a planner run, intent classification, a background agent.
TOTALS_INTERVAL_S = 15.0


async def totals_broadcaster(interval: float = TOTALS_INTERVAL_S) -> None:
    """Re-send the dashboard totals periodically, as a backstop.

    Totals are the one part of a snapshot that queries the database, so they
    are not rebuilt per broker message. The `chat` frame carries them, which
    covers the case someone is actually watching — ask a question, see the
    cost move. Not every expense produces a chat frame though, so this keeps
    the figure honest for spend that happens out of sight.

    One query per interval, independent of message rate and agent count.
    """
    while True:
        await asyncio.sleep(interval)
        # Check before building: the snapshot is the expensive half, and
        # `broadcast` would discard it anyway with nobody listening.
        if not runtime.ws_clients or runtime.hard_resetting:
            continue
        try:
            await broadcast({"type": "patch", "state": events.snapshot()})
        except Exception as exc:
            # A failed tick must not end the loop, or totals stop updating for
            # the life of the process with nothing to say why.
            logger.warning("[totals] periodic broadcast failed: %s", exc)


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


def log_chat_turn(role: str, content: str, agent_name: str) -> None:
    """Put one chat turn on the console, redacted as chat_log is, and cut short."""
    text = redact(content)
    if len(text) > CHAT_LOG_PREVIEW_CHARS:
        text = text[:CHAT_LOG_PREVIEW_CHARS] + "…"
    direction = f"user → @{agent_name}" if role == "user" else f"@{agent_name} → user"
    chat_logger.info("[chat] %s: %s", direction, text)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle websocket connection.

    The handshake is checked before anything is accepted. A WebSocket is exempt
    from CORS entirely, so narrowing the HTTP surface alone leaves a page on any
    origin able to open this socket, read the live feed and send commands.
    """
    refusal = origins.refuse(request, strict_origin=True)
    if refusal is not None:
        raise web.HTTPForbidden(text=refusal.text, content_type="application/json")

    ws = web.WebSocketResponse(heartbeat=HEARTBEAT_SECONDS)
    await ws.prepare(request)
    channel = Channel(ws)
    runtime.ws_clients.add(channel)
    logger.info("WebSocket client connected. Total: %d", len(runtime.ws_clients))

    # Send initial state
    await ws.send_str(json.dumps({"type": "full_snapshot", "state": events.snapshot()}))

    # Current server↔broker state so the "live" badge is right immediately on load.
    await ws.send_str(json.dumps({"type": "mqtt_status", "connected": runtime.mqtt_connected}))

    # This connection's recognition session, if it opens one.
    listening = Listening()

    def _persist_chat(
        role: str,
        content: str,
        agent_name: str = "main",
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Best-effort write to chat_log, and a line on the console. Never raises into the WS path."""
        if content:
            log_chat_turn(role, content, agent_name)
        if runtime.db is None or not (content or attachments):
            return
        try:
            runtime.db.write_chat_log(
                ts=time.time(),
                agent_name=agent_name,
                role=role,
                attachments=attachments,
                # The log outlives the conversation and is readable through the
                # API, so it gets the same treatment as the log file: a user can
                # still type a credential even where no command accepts one.
                content=redact(content),
            )
        except Exception as exc:
            logger.warning("[ws] chat_log write failed: %s", exc)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "ping":
                        # Answered so the browser can tell a live socket from one
                        # whose far end has gone. The protocol-level ping this
                        # connection already sends is handled by the browser
                        # itself and never reaches the page, so it cannot serve.
                        await ws.send_str(json.dumps({"type": "pong"}))

                    elif msg_type == "command":
                        await handle_command(data)

                    elif msg_type == "stt_start":
                        await listening.start(ws)

                    elif msg_type == "stt_stop":
                        await listening.finish()

                    elif msg_type == "chat":
                        content = (data.get("content") or "").strip()
                        # Ids only travel on the wire; the name and type come
                        # from what the server stored, so a caller cannot label
                        # a file as something else in every thread that shows it.
                        files = uploads.resolve(data.get("attachments"))
                        if content and runtime.registry is not None:
                            # Attribute the whole turn — reply frames and
                            # chat_log alike — to the agent it addresses, rather
                            # than to the io-gateway transport id. A mention that
                            # names nothing this process can reach falls back to
                            # the thread the sender says it is in, so the answer
                            # ("not found") arrives where the user is looking and
                            # ends the turn there; attributed to the mention, it
                            # would name an agent no view has a thread for.
                            name = chat.turn_attribution(content, str(data.get("agent_name") or ""))
                            # Persist the user's turn first so chat_log has the
                            # request even if the assistant reply errors out.
                            _persist_chat("user", content, name, files)
                            turn = TurnReplies(ws, name, _persist_chat)

                            async def _safe_route(c=content, files=files, turn=turn):
                                # Both halves of this turn are stored here — the
                                # user's above, the reply once it has finished —
                                # so an agent that also stores its own turns must
                                # not write them again. Set inside this task, the
                                # mark follows the call into the agent and no other
                                # turn sees it.
                                chat_turn_recorded.set(True)
                                try:
                                    await chat.route_chat(
                                        c,
                                        turn.reply,
                                        stream_fn=turn.chunk,
                                        stream_end_fn=turn.end,
                                        attachments=files,
                                    )
                                except asyncio.CancelledError:
                                    # Stop button: finalize the partial stream so
                                    # the UI re-enables its composer, then say so.
                                    # Without the stream-end the send control
                                    # stays disabled with nothing to re-enable it.
                                    try:
                                        await turn.end()
                                        await turn.reply("⏹ Stopped.")
                                    # The socket is already gone; that is why we are here.
                                    except Exception:  # noqa: S110
                                        pass
                                    raise
                                except Exception as exc:
                                    logger.exception("[ws] chat error")
                                    try:
                                        await turn.reply(f"[error] {exc}")
                                        await turn.end()
                                    # The socket is already gone; that is why we are here.
                                    except Exception:  # noqa: S110
                                        pass

                            chat.track_chat_task(asyncio.create_task(_safe_route()))
                        elif content:
                            await TurnReplies(ws, runtime.IO_GATEWAY_ID, _persist_chat).reply(
                                "[system] Chat unavailable — no actor registry."
                            )

                except Exception as e:
                    logger.warning("[ws] Bad message: %s", e)
            elif msg.type == WSMsgType.BINARY:
                # Audio for an open recognition session. Frames only mean
                # something while one is running; before `stt_start` there is
                # nothing to feed and dropping them is the whole handling.
                if listening.session is not None:
                    await listening.session.feed(msg.data)

            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        await listening.stop()
        runtime.ws_clients.discard(channel)
        await channel.close()
        logger.info("WebSocket client disconnected. Total: %d", len(runtime.ws_clients))
    return ws


class TurnReplies:
    """The callbacks one chat turn answers through, on one socket.

    One per turn rather than one per socket: the turns on a socket run at the
    same time, each in its own task, and a reply ends when its own turn does.
    Shared, the first turn to end closes the stream of whichever one is still
    answering, and files that one's text under its own name.

    Replies are attributed to the agent the turn addressed rather than to the
    transport, so the page and the reloaded history show who answered. A
    streamed reply is stored once, whole, when it ends, rather than a row per
    piece.
    """

    def __init__(
        self,
        ws: web.WebSocketResponse,
        name: str,
        persist: Callable[[str, str, str], None],
    ) -> None:
        self._ws = ws
        self.name = name
        self._persist = persist
        self._streamed: list[str] = []

    async def reply(self, text: str) -> None:
        """Send one whole reply, and store it."""
        try:
            await self._send("chat", content=text)
            self._persist("assistant", text, self.name)
        except Exception:
            logger.debug("[ws] Could not deliver or persist a reply", exc_info=True)

    async def chunk(self, chunk: str) -> None:
        """Send one piece of a reply that is still arriving."""
        try:
            await self._send("stream_chunk", content=chunk)
            if chunk:
                self._streamed.append(chunk)
        except Exception:
            logger.debug("[ws] Could not deliver a stream chunk", exc_info=True)

    async def end(self) -> None:
        """Close the streamed reply, and store it whole.

        Stored even when the socket has gone, so a turn is not lost to a
        hiccup on the way out.
        """
        try:
            await self._send("stream_end")
        except Exception:
            logger.debug("[ws] Could not deliver the end of a stream", exc_info=True)
        if self._streamed:
            whole, self._streamed = "".join(self._streamed), []
            self._persist("assistant", whole, self.name)

    async def _send(self, kind: str, **fields: str) -> None:
        await self._ws.send_str(
            json.dumps({"type": kind, "from": self.name, **fields, "timestamp": time.time()})
        )


class Listening:
    """One browser's recognition session, for as long as it keeps the socket.

    Tied to the connection rather than kept globally: two browsers may listen at
    once, and a session belongs to whoever opened it. A closed tab ends its own
    session and nobody else's.
    """

    def __init__(self) -> None:
        self.session: streaming.LiveTranscription | None = None
        self._pump: asyncio.Task[None] | None = None

    async def start(self, ws: web.WebSocketResponse) -> None:
        """Open a session, or say why there is not going to be one."""
        if self.session is not None:
            return  # already listening; a second start is not an error

        uri = service_uri()
        if not streaming.is_streaming_uri(uri):
            # A batch recogniser answers once, at the end. Saying so is better
            # than opening a session that will never produce a partial.
            await _send(ws, {"type": "stt_error", "message": "this recogniser does not stream"})
            return

        self.session = streaming.LiveTranscription(uri)
        await self.session.__aenter__()
        self._pump = asyncio.create_task(self._forward(ws, self.session))

    async def finish(self) -> None:
        """Tell the recogniser the audio has ended; readings settle on their own."""
        if self.session is not None:
            await self.session.finish()

    async def stop(self) -> None:
        """End the session, whether it finished or the socket simply went away."""
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
        await self._release()

    async def _release(self) -> None:
        """Let go of the session, so the next `stt_start` opens a fresh one.

        Idempotent, because both the forwarding task and `stop` reach it: a turn
        that ends on its own frees the session there, and one cut short frees it
        here. Without that the guard in `start` sees a spent session and quietly
        refuses, leaving a socket good for exactly one utterance.
        """
        self._pump = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _forward(
        self, ws: web.WebSocketResponse, session: streaming.LiveTranscription
    ) -> None:
        """Send every reading to the browser as it arrives.

        Takes the session as an argument rather than reading the attribute: the
        attribute is cleared when the turn ends, and this loop outlives that.
        """
        try:
            async for reading in session.readings():
                await _send(
                    ws,
                    {
                        "type": "stt_final" if reading.final else "stt_partial",
                        "text": reading.text,
                        "segment": reading.segment,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[ws] recognition ended: %s", exc)
            await _send(ws, {"type": "stt_error", "message": "recognition stopped"})
        # Reached when the turn ends on its own or fails and is reported, never
        # when cancelled -- `stop` owns the session in that case.
        await self._release()


async def _send(ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
    """Send one frame, tolerating a socket that has already gone."""
    try:
        await ws.send_str(json.dumps(payload))
    except Exception:  # pylint: disable=broad-exception-caught  # noqa: S110
        pass


# ── Browser commands ───────────────────────────────────────────────────────


async def handle_command(cmd: dict[str, Any]) -> None:
    """Handle a command message."""
    command = cmd.get("command")
    agent_id = cmd.get("agent_id")
    if not command or not agent_id:
        return
    if command not in {"start", "stop", "delete"}:
        return

    logger.info("[cmd] %s -> %s", command.upper(), agent_id[:8])
    try:
        if command == "delete":
            # delete_agent has its own routing: main's spawn registry, then the
            # local actor, then the broker for agents on other nodes.
            if await lifecycle.delete_agent(agent_id) == "refused-protected":
                # Broadcasting anyway would remove the agent from every open
                # dashboard while it is still running, with nothing to correct
                # the view until the next heartbeat.
                return
            events.add_log(
                {
                    "type": "command",
                    "agent_id": agent_id,
                    "command": command,
                    "timestamp": time.time(),
                }
            )
            await broadcast(
                {
                    "type": events.DELETE_AGENT_FRAME,
                    "agent_id": agent_id,
                    "state": events.snapshot(),
                }
            )
            return

        # Dispatch, feed entry, reported state and the patch to every open
        # dashboard all happen in `run_command`, which REST goes through too.
        await lifecycle.run_command(agent_id, command, "monitor-dashboard")
    except Exception:
        logger.exception("[cmd] %s failed", command)
