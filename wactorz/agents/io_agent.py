"""
IOAgent - UI gateway actor.

Listens on MQTT topic `io/chat` and routes messages to actors by `@agent-name`
prefix. Messages with no `@` prefix are forwarded to `main-actor`. Replies are
published back to `agents/{actor_id}/chat` so the frontend chat panel displays them.

Slash commands are handled here so both the web UI and any other MQTT-connected
interface get the same behaviour as the CLI.
"""

import asyncio
import json
import logging
import socket
import time

from ..core.actor import Actor, ActorState, Message, MessageType

logger = logging.getLogger(__name__)

IO_CHAT_TOPIC = "io/chat"
IO_CHAT_REPLY_TOPIC = "io/chat/response"  # stable topic the UI always subscribes to


class IOAgent(Actor):
    """
    Gateway between the frontend UI and the actor network.

    Receives raw chat payloads from the browser via MQTT `io/chat`, parses an
    optional `@name` prefix to select a target actor, and delivers the text as
    a TASK message. Replies from target actors are forwarded to the frontend.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "io-agent")
        super().__init__(**kwargs)
        self.protected = False
        self._pending_replies: dict[str, tuple[str, float]] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":        self.actor_id,
                "agentName":      self.name,
                "agentType":      "gateway",
                "replyTopic":     IO_CHAT_REPLY_TOPIC,   # tell UI which topic to subscribe to
                "timestamp":      time.time(),
            },
        )
        self._tasks.append(asyncio.create_task(self._io_chat_listener()))
        logger.info(f"[{self.name}] started — listening on '{IO_CHAT_TOPIC}', replying on '{IO_CHAT_REPLY_TOPIC}'")

    # ── MQTT subscriber ────────────────────────────────────────────────────

    async def _io_chat_listener(self):
        """Subscribe to `io/chat` and route every incoming message."""
        try:
            import aiomqtt
        except ImportError:
            logger.error(f"[{self.name}] aiomqtt not installed — io/chat listener disabled")
            return

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe(IO_CHAT_TOPIC, qos=1)
                    async for mqtt_msg in client.messages:
                        if self.state in (ActorState.STOPPED, ActorState.FAILED):
                            break
                        try:
                            raw = mqtt_msg.payload
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode()
                            data = json.loads(raw)
                            await self._route_chat(data)
                        except Exception as exc:
                            logger.error(f"[{self.name}] io/chat parse error: {exc}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    logger.warning(f"[{self.name}] io/chat disconnected: {exc}. Retry in 5s")
                    await asyncio.sleep(5)

    # ── Routing ────────────────────────────────────────────────────────────

    async def _route_chat(self, data: dict):
        content: str = (data.get("content") or "").strip()
        from_id: str = data.get("from", "user")
        if not content:
            return

        # Slash commands are handled locally — never reach the LLM
        if content.startswith("/"):
            if await self._handle_slash(content):
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
            target = self._registry.find_by_name("main")
            if target is None:
                await self._reply("No main-actor is running.")
                return

        logger.info(f"[{self.name}] routing from '{from_id}' → '{target.name}': {text[:60]!r}")

        # Call streaming methods directly (same as CLI) if available — gives
        # chunk-by-chunk responses instead of waiting for the full reply.
        if target_name in ("main-actor", "main") and hasattr(target, "process_user_input_stream"):
            buf = []
            async for chunk in target.process_user_input_stream(text):
                if isinstance(chunk, dict):
                    continue  # system metadata, skip
                buf.append(str(chunk))
                # flush every ~80 chars so the UI feels live
                if sum(len(c) for c in buf) >= 80:
                    await self._reply("".join(buf))
                    buf.clear()
            if buf:
                await self._reply("".join(buf))
            return

        if hasattr(target, "chat_stream"):
            buf = []
            async for chunk in target.chat_stream(text):
                if isinstance(chunk, dict):
                    continue
                buf.append(str(chunk))
                if sum(len(c) for c in buf) >= 80:
                    await self._reply("".join(buf))
                    buf.clear()
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
        return self._registry.find_by_name("main")

    async def _handle_slash(self, text: str) -> bool:
        """
        Dispatch slash commands. Returns True if the command was recognised
        (regardless of success), so the caller knows not to forward to the LLM.
        """
        parts = text.split()
        cmd   = parts[0].lower()

        if cmd in ("/help", "/h"):
            await self._reply(
                "Commands:\n"
                "  /agents                           list all active agents\n"
                "  /nodes                            list remote nodes\n"
                "  /migrate <agent> <node>           move an agent to a different node\n"
                "  /deploy <node> [host [user [pw [broker]]]]\n"
                "                                    deploy a remote Wactorz node\n"
                "  /clear-plans                      clear the plan cache\n\n"
                "Everything else goes to the main orchestrator.\n"
                "Spawn on a remote node: \"spawn a temp sensor on rpi-kitchen\"\n"
                "Migrate via chat:       \"move temp-sensor to rpi-bedroom\""
            )
            return True

        if cmd == "/clear-plans":
            main = self._main_actor()
            if main and hasattr(main, "persist"):
                main.persist("_plan_cache", {})
            await self._reply("[System: Plan cache cleared.]")
            return True

        if cmd == "/agents":
            await self._slash_agents()
            return True

        if cmd == "/nodes":
            await self._slash_nodes()
            return True

        if cmd == "/migrate":
            if len(parts) < 3:
                await self._reply("[usage] /migrate <agent-name> <target-node>\n"
                                  "Example: /migrate temp-sensor rpi-bedroom")
            else:
                await self._slash_migrate(parts[1], parts[2])
            return True

        if cmd == "/deploy":
            if len(parts) < 2:
                await self._reply("[usage] /deploy <node-name> [host [user [password [broker]]]]\n"
                                  "Run with just the node name to discover hosts automatically.")
            else:
                node   = parts[1]
                host   = parts[2] if len(parts) > 2 else ""
                user   = parts[3] if len(parts) > 3 else ""
                pw     = parts[4] if len(parts) > 4 else ""
                broker = parts[5] if len(parts) > 5 else ""
                await self._slash_deploy(node, host, user, pw, broker)
            return True

        return False

    async def _slash_agents(self):
        if self._registry is None:
            await self._reply("[agents] Registry not available.")
            return
        lines = []
        for actor in self._registry.all_actors():
            status    = actor.get_status() if hasattr(actor, "get_status") else {}
            state     = status.get("state", "?")
            protected = " [protected]" if getattr(actor, "protected", False) else ""
            node      = f" [{status['node']}]" if status.get("node") else ""
            lines.append(f"  [{state:8s}] @{actor.name:<22s} {actor.actor_id[:8]}{protected}{node}")
        await self._reply("Agents:\n" + "\n".join(lines) if lines else "[agents] No agents running.")

    async def _slash_nodes(self):
        main = self._main_actor()
        remote_nodes = []
        if main and hasattr(main, "list_nodes"):
            remote_nodes = main.list_nodes()

        local_agents = []
        if self._registry:
            local_agents = [a.name for a in self._registry.all_actors()]

        lines = [f"  {'local':20s} online   {', '.join('@' + n for n in local_agents) or '(none)'}"]
        for nd in sorted(remote_nodes, key=lambda x: x["node"]):
            status = "online" if nd["online"] else "OFFLINE"
            names  = ", ".join("@" + n for n in nd["agents"]) or "(no agents)"
            lines.append(f"  {nd['node']:20s} {status:6s}   {names}")
        if not remote_nodes:
            lines.append("  (no remote nodes — deploy one with /deploy <node-name>)")
        await self._reply("Nodes:\n" + "\n".join(lines))

    async def _slash_migrate(self, agent_name: str, target_node: str):
        main = self._main_actor()
        if main is None or not hasattr(main, "migrate_agent"):
            await self._reply("[error] migrate_agent not available.")
            return
        await self._reply(f"[migrating] Moving @{agent_name} → {target_node}...")
        result = await main.migrate_agent(agent_name, target_node)
        ok  = result.get("success", False)
        sym = "OK" if ok else "FAIL"
        await self._reply(f"[{sym}] {result.get('message', str(result))}")

    async def _slash_deploy(self, node_name: str, host: str, user: str, pw: str, broker: str):
        # ── Step 1: discover host if not provided ──────────────────────────
        if not host:
            await self._reply(f"[discover] Searching for '{node_name}' on the network...")

            # mDNS
            discovered = None
            for candidate in [f"{node_name}.local", "raspberrypi.local",
                               f"{node_name.replace('-', '')}.local"]:
                try:
                    ip = await asyncio.get_event_loop().run_in_executor(
                        None, socket.gethostbyname, candidate
                    )
                    discovered = ip
                    await self._reply(f"[discover] Found via mDNS: {candidate} → {ip}")
                    break
                except socket.gaierror:
                    pass

            # Subnet scan
            if not discovered:
                try:
                    local_ip = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: socket.gethostbyname(socket.gethostname())
                    )
                    subnet = ".".join(local_ip.split(".")[:3])
                except Exception:
                    subnet = "192.168.1"
                await self._reply(f"[discover] mDNS not found. Scanning {subnet}.1-254 for SSH...")
                found = await self._scan_subnet_ssh(subnet)
                if found:
                    host_list = "\n".join(f"  {ip}" for ip in found)
                    await self._reply(
                        f"[discover] Found {len(found)} SSH-accessible host(s):\n{host_list}\n\n"
                        f"Re-run with the host you want:\n"
                        f"  /deploy {node_name} <host> <user> <password> [broker]"
                    )
                else:
                    await self._reply(
                        "[discover] No SSH hosts found.\n"
                        f"Provide the host manually:\n"
                        f"  /deploy {node_name} <host> <user> <password> [broker]"
                    )
            else:
                await self._reply(
                    f"[discover] Host found: {discovered}\n"
                    f"Re-run with credentials:\n"
                    f"  /deploy {node_name} {discovered} <user> <password> [broker]"
                )
            return

        # ── Step 2: need credentials ───────────────────────────────────────
        if not user or not pw:
            await self._reply(
                f"[deploy] Host: {host}\n"
                f"Need SSH credentials. Re-run with:\n"
                f"  /deploy {node_name} {host} <user> <password> [broker]"
            )
            return

        # ── Step 3: deploy ─────────────────────────────────────────────────
        broker = broker or "localhost"
        main   = self._main_actor()
        if main is None or not hasattr(main, "delegate_to_installer"):
            await self._reply("[error] Installer agent not available.")
            return

        await self._reply(f"[deploy] Deploying to {user}@{host} as node '{node_name}'...\n"
                          f"(This may take 20-60 seconds)")
        result = await main.delegate_to_installer({
            "action":    "node_deploy",
            "host":      host,
            "user":      user,
            "password":  pw,
            "node_name": node_name,
            "broker":    broker,
        }, timeout=120.0)

        if result.get("success"):
            await self._reply(
                f"[OK] Node '{node_name}' is live! It will appear in /nodes within ~15 seconds.\n\n"
                f"Spawn agents on it:\n"
                f"  \"spawn a CPU monitor agent on {node_name}\"\n"
                f"  \"spawn a temperature sensor on {node_name}\""
            )
        else:
            await self._reply(f"[FAIL] Deploy failed: {result.get('error', result)}")

    async def _scan_subnet_ssh(self, subnet: str) -> list:
        """Async port-22 scan of a /24 subnet."""
        found = []
        sem   = asyncio.Semaphore(60)

        async def probe(ip):
            async with sem:
                try:
                    _, w = await asyncio.wait_for(
                        asyncio.open_connection(ip, 22), timeout=0.4
                    )
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
                    payload.get("reply") or payload.get("result")
                    or payload.get("text") or payload.get("content") or str(payload)
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