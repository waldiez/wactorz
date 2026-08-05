"""IOAgent - UI gateway actor.

Listens on MQTT topic `io/chat` and routes messages to actors by `@agent-name`
prefix. Messages with no `@` prefix are forwarded to `main-actor`. Replies are
published back to `agents/{actor_id}/chat` so the frontend chat panel displays them.

Slash commands are handled here so both the web UI and any other MQTT-connected
interface get the same behaviour as the CLI.
"""

import asyncio
import json
import logging
import time

from ..core.actor import Actor, ActorState, Message, MessageType
from ..core.mqtt import mqtt_client
from .lookup import find_main_actor

logger = logging.getLogger(__name__)

IO_CHAT_TOPIC = "io/chat"
IO_CHAT_REPLY_TOPIC = "io/chat/response"  # stable topic the UI always subscribes to
IO_CHAT_CONTROL_TOPIC = "io/chat/control"  # UI publishes {"action": "stop"} here


class IOAgent(Actor):
    """Gateway between the frontend UI and the actor network.

    Receives raw chat payloads from the browser via MQTT `io/chat`, parses an
    optional `@name` prefix to select a target actor, and delivers the text as
    a TASK message. Replies from target actors are forwarded to the frontend.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "io-agent")
        super().__init__(**kwargs)
        # The io-agent is core infrastructure (the chat/IO gateway) — it must
        # survive a "Wipe everything" reset like the other system actors.
        self.protected = True
        self._pending_replies: dict[str, tuple[str, float]] = {}
        # In-flight chat turns, so a stop command can cancel them mid-generation.
        self._active_generations: set[asyncio.Task] = set()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId": self.actor_id,
                "agentName": self.name,
                "agentType": "gateway",
                "replyTopic": IO_CHAT_REPLY_TOPIC,  # tell UI which topic to subscribe to
                "timestamp": time.time(),
            },
        )
        self._tasks.append(asyncio.create_task(self._io_chat_listener()))
        logger.info(
            f"[{self.name}] started — listening on '{IO_CHAT_TOPIC}', replying on '{IO_CHAT_REPLY_TOPIC}'"
        )

    # ── MQTT subscriber ────────────────────────────────────────────────────

    async def _io_chat_listener(self):
        """Subscribe to `io/chat` and route every incoming message."""
        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            logger.error(f"[{self.name}] aiomqtt not installed — io/chat listener disabled")
            return

        _last_chat_exc: str | None = None
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with mqtt_client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe(IO_CHAT_TOPIC, qos=1)
                    await client.subscribe(IO_CHAT_CONTROL_TOPIC, qos=1)
                    _last_chat_exc = None
                    async for mqtt_msg in client.messages:
                        if self.state in (ActorState.STOPPED, ActorState.FAILED):
                            break
                        try:
                            raw = mqtt_msg.payload
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode()
                            data = json.loads(raw)

                            # Control channel (stop/cancel) is handled inline so it
                            # stays responsive even while a generation is streaming.
                            if str(mqtt_msg.topic) == IO_CHAT_CONTROL_TOPIC:
                                await self._handle_control(data)
                                continue

                            # Run each chat turn as a tracked task so the listener
                            # stays free to receive a stop command mid-generation.
                            task = asyncio.create_task(self._route_chat(data))
                            self._active_generations.add(task)
                            task.add_done_callback(self._active_generations.discard)
                        except Exception as exc:
                            logger.error(f"[{self.name}] io/chat parse error: {exc}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    exc_str = str(exc)
                    if exc_str != _last_chat_exc:
                        logger.warning(f"[{self.name}] io/chat disconnected: {exc}. Retry in 5s")
                        _last_chat_exc = exc_str
                    else:
                        logger.debug(f"[{self.name}] io/chat still unavailable — retrying in 5s…")
                    await asyncio.sleep(5)

    # ── Routing ────────────────────────────────────────────────────────────

    async def _route_chat(self, data: dict):
        content: str = (data.get("content") or "").strip()
        from_id: str = data.get("from", "user")
        if not content:
            return

        # Slash commands are handled locally — never reach the LLM
        if content.startswith("/") and await self._handle_slash(content):
            return

        target_name, text = self._parse_mention(content)

        if self._registry is None:
            await self._reply("System not ready — no actor registry available.")
            return

        target = self._registry.find_by_name(target_name)
        if target is None:
            if target_name != "main-actor":
                await self._reply(f"Agent @{target_name} not found.")
                return
            target = find_main_actor(self._registry)
            if target is None:
                await self._reply("No main-actor is running.")
                return

        logger.info(f"[{self.name}] routing from '{from_id}' → '{target.name}': {text[:60]!r}")

        # Call streaming methods directly (same as CLI) if available — gives
        # chunk-by-chunk responses instead of waiting for the full reply.
        if target_name in ("main-actor", "main") and hasattr(target, "process_user_input_stream"):
            buf = []
            try:
                async for chunk in target.process_user_input_stream(text):
                    if isinstance(chunk, dict):
                        continue  # system metadata, skip
                    buf.append(str(chunk))
                    # flush every ~80 chars so the UI feels live
                    if sum(len(c) for c in buf) >= 80:
                        await self._reply("".join(buf))
                        buf.clear()
            finally:
                # Flush whatever is left even if the stream was cancelled
                # (stop button) so the last sub-80-char fragment isn't lost.
                if buf:
                    await self._reply("".join(buf))
            return

        if hasattr(target, "chat_stream"):
            buf = []
            try:
                async for chunk in target.chat_stream(text):
                    if isinstance(chunk, dict):
                        continue
                    buf.append(str(chunk))
                    if sum(len(c) for c in buf) >= 80:
                        await self._reply("".join(buf))
                        buf.clear()
            finally:
                if buf:
                    await self._reply("".join(buf))
            return

        # Fallback: actor message passing (no streaming)
        msg = Message(
            type=MessageType.TASK,
            sender_id=self.actor_id,
            payload={"text": text, "from": from_id, "reply_to": self.actor_id},
        )
        self._pending_replies[msg.message_id] = (from_id, time.time())
        await target.receive(msg)

    # ── Generation control (stop button) ───────────────────────────────────

    async def _handle_control(self, data: dict):
        """Handle control-channel messages from the UI (e.g. the stop button)."""
        action = (data.get("action") or "").strip().lower()
        if action in ("stop", "cancel", "abort", "interrupt"):
            await self._stop_generations()
        else:
            logger.debug(f"[{self.name}] ignoring unknown control action: {action!r}")

    async def _stop_generations(self):
        """Cancel any in-flight chat generation(s).

        Cancellation propagates down through process_user_input_stream →
        chat_stream → the provider's streaming context manager, which closes
        the LLM HTTP connection. LLMAgent.chat_stream catches the cancellation,
        persists whatever partial text already arrived, and re-raises — so no
        output is lost and the connection is released cleanly.
        """
        tasks = [t for t in self._active_generations if not t.done()]
        if not tasks:
            await self._reply("[Nothing is generating right now.]")
            return

        for t in tasks:
            t.cancel()
        # Wait for them to unwind so partial state is persisted before we reply.
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._reply("⏹ Stopped.")
        logger.info(f"[{self.name}] stopped {len(tasks)} active generation(s)")

    @staticmethod
    def _parse_mention(content: str) -> tuple[str, str]:
        if content.startswith("@"):
            parts = content[1:].split(None, 1)
            name = parts[0]
            text = parts[1].strip() if len(parts) > 1 else ""
            return name, text
        return "main-actor", content

    async def _reply(self, content: str):
        await self._mqtt_publish(
            IO_CHAT_REPLY_TOPIC,
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )
        # Also publish to the actor_id topic for any legacy subscribers
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    # ── Slash commands ─────────────────────────────────────────────────────

    def _main_actor(self):
        """Return the main actor instance, or None."""
        if self._registry is None:
            return None
        return find_main_actor(self._registry)

    async def _handle_slash(self, text: str) -> bool:
        """Slash-command dispatch. The single source of truth lives in main_actor.py
        — every slash command (including /deploy) is implemented there exactly
        once, so CLI, UI, Discord, and any future interface behave identically.

        io_agent's only job for slash commands is to pipe the input into main
        and stream the output back to the chat channel. Returns True for any
        slash-prefixed input, False otherwise.
        """
        if not text.startswith("/"):
            return False
        await self._forward_slash_to_main(text)
        return True

    async def _forward_slash_to_main(self, slash_text: str):
        """Pipe a slash command into main and stream its output back live.

        We flush each text chunk as it arrives — important for /deploy, which
        emits progress messages mid-execution (subnet scan, deploy phases).
        Buffering would defeat that and make the UI feel hung for ~10s.
        """
        main = self._main_actor()
        if main is None:
            await self._reply("[error] main-actor not available.")
            return

        async for chunk in main.process_user_input_stream(slash_text):
            if isinstance(chunk, dict):
                continue  # {"done": True, ...} system marker — skip
            s = str(chunk)
            if s:
                await self._reply(s)

    # ── handle_message ─────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            payload = msg.payload or {}
            if isinstance(payload, str):
                content, from_id = payload, msg.sender_id
            else:
                content = payload.get("content") or payload.get("text") or ""
                from_id = payload.get("from") or msg.sender_id
            await self._route_chat({"from": from_id, "content": content})

        elif msg.type == MessageType.RESULT:
            payload = msg.payload or {}
            if isinstance(payload, dict):
                reply_text = (
                    payload.get("reply")
                    or payload.get("result")
                    or payload.get("text")
                    or payload.get("content")
                    or str(payload)
                )
            else:
                reply_text = str(payload)
            self._pending_replies.pop(next(iter(self._pending_replies), None), None)
            await self._reply(reply_text)

    def _current_task_description(self) -> str:
        return f"routing io/chat (pending={len(self._pending_replies)})"

    def get_status(self) -> dict:
        s = super().get_status()
        s["agent_type"] = "gateway"
        return s
