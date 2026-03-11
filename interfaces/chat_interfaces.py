"""
Chat Interfaces - Connect users to the MainActor via different channels.
Supported: CLI (terminal), Discord, WhatsApp (via Twilio), REST.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agents.main_actor import MainActor

logger = logging.getLogger(__name__)

# Path to remote_runner.py — at project root (parent of agentflow package)
_HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE_RUNNER_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "remote_runner.py"))


# ─── CLI Interface ──────────────────────────────────────────────────────────

class CLIInterface:
    """
    Terminal chat interface.

    Commands:
      @agent-name <message>         speak directly to a named agent
      /agents                       list all active agents
      /nodes                        list connected remote nodes
      /deploy <node-name>           deploy remote runner (auto-discovers host)
      /help                         show commands
      quit / exit                   shutdown
    """

    def __init__(self, main_actor: "MainActor"):
        self.agent = main_actor

    def _print_help(self):
        print("""
  Commands:
    @<n> <msg>               speak directly to a named agent
    /agents                     list all active agents and their state
    /nodes                      list remote nodes (online/offline) and their agents
    /migrate <agent> <node>     move a running agent to a different node
                                e.g.  /migrate temp-sensor rpi-bedroom
    /deploy <node-name>         set up a remote machine as an AgentFlow node
                                e.g.  /deploy rpi-node
    /help                       show this help
    quit / exit                 shutdown

  Everything else goes to the main orchestrator.
  Spawn on a remote node: "spawn a temp sensor on rpi-kitchen"
  Migrate via chat:       "move temp-sensor to rpi-bedroom"
""")

    # ── Agent routing ──────────────────────────────────────────────────────

    async def _get_agent_response(self, agent_name: str, message: str) -> str:
        registry = self.agent._registry
        if registry is None:
            return "[error] No registry available."

        target = registry.find_by_name(agent_name)
        if target is None:
            names = [a.name for a in registry.all_actors()]
            return f"[error] No agent named '{agent_name}'. Available: {', '.join(names)}"

        try:
            # Case 1: real LLMAgent — has self.llm and chat() backed by it
            # Detect by presence of _conversation_history (LLMAgent-specific)
            if hasattr(target, "_conversation_history") and hasattr(target, "chat"):
                return await target.chat(message)

            # Case 2: DynamicAgent with a handle_task function
            if hasattr(target, "_fn_handle_task") and target._fn_handle_task:
                result = await target._fn_handle_task(
                    target._api,
                    {"message": message, "text": message, "query": message}
                )
                if isinstance(result, dict):
                    for key in ("reply", "answer", "result", "text", "response"):
                        if result.get(key):
                            return str(result[key])
                    return str(result)
                return str(result) if result else f"[{agent_name}] No response"

            # Case 3: DynamicAgent with llm but no handle_task — direct llm call
            if hasattr(target, "_llm_provider") and target._llm_provider:
                return await target._api.llm.chat(message)

            # Case 4: any agent with a chat() method
            if hasattr(target, "chat"):
                return await target.chat(message)

        except Exception as e:
            return f"[error] {agent_name} failed: {e}"

        # Fallback: delegate via message passing
        result = await self.agent.delegate_task(agent_name, message, timeout=60.0)
        if result:
            for key in ("text", "reply", "answer", "result"):
                if result.get(key):
                    return str(result[key])
            return str(result)
        return f"[{agent_name}] Task sent (no synchronous response)"

    async def _get_remote_agent_response(self, agent_name: str, message: str) -> str:
        """Route a message to a remote agent via MQTT and wait for reply."""
        main = self.agent

        # Find which node hosts this agent
        remote_node = None
        for node_name, nd in main._known_nodes.items():
            if agent_name in nd.get("agents", []):
                remote_node = node_name
                break

        if not remote_node:
            known = [a for nd in main._known_nodes.values() for a in nd.get("agents", [])]
            if known:
                return f"[error] Agent '{agent_name}' not found. Remote agents: {', '.join(known)}"
            return f"[error] Agent '{agent_name}' not found. No remote nodes connected."

        import json, uuid as _uuid
        try:
            import aiomqtt
        except ImportError:
            return "[error] aiomqtt not installed"

        reply_topic = f"main/reply/{main.actor_id}/{_uuid.uuid4().hex[:8]}"
        result_holder = []

        async def _listen_for_reply():
            try:
                async with aiomqtt.Client(main._mqtt_broker, main._mqtt_port) as client:
                    await client.subscribe(reply_topic)
                    async for msg in client.messages:
                        try:
                            result_holder.append(json.loads(msg.payload.decode()))
                        except Exception:
                            result_holder.append({"result": msg.payload.decode()})
                        return
            except Exception as e:
                result_holder.append({"error": str(e)})

        import asyncio
        listener = asyncio.create_task(_listen_for_reply())
        await asyncio.sleep(0.15)  # let subscriber connect first

        await main._mqtt_publish(
            f"agents/by-name/{agent_name}/task",
            {"text": message, "payload": message,
             "_remote_task": True, "_reply_topic": reply_topic},
        )

        try:
            await asyncio.wait_for(asyncio.shield(listener), timeout=30.0)
        except asyncio.TimeoutError:
            listener.cancel()
            return f"[timeout] {agent_name} on {remote_node} did not respond within 30s"

        if not result_holder:
            return f"[error] No reply from {agent_name}"

        result = result_holder[0]
        if isinstance(result, str):
            return result
        if not isinstance(result, dict):
            return str(result)
        if "error" in result:
            return f"[error] {result['error']}"
        for key in ("reply", "answer", "result", "text", "response"):
            if result.get(key):
                return str(result[key])
        return str(result)

    # ── Node discovery ─────────────────────────────────────────────────────

    async def _discover_host(self, node_name: str) -> str:
        """
        Find a remote host automatically:
        1. mDNS  — try {node_name}.local and raspberrypi.local
        2. Scan  — scan local subnet for SSH (port 22)
        3. Manual — ask user
        """
        import socket

        print(f"\n[discover] Searching for '{node_name}' on the network...")

        # 1. mDNS
        candidates = [
            f"{node_name}.local",
            "raspberrypi.local",
            f"{node_name.replace('-', '')}.local",
        ]
        for hostname in candidates:
            try:
                ip = socket.gethostbyname(hostname)
                print(f"[discover] Found via mDNS: {hostname} → {ip}")
                ans = await asyncio.get_event_loop().run_in_executor(
                    None, lambda h=hostname, i=ip: input(f"  Use {h} ({i})? [Y/n]: ").strip().lower()
                )
                if ans in ("", "y", "yes"):
                    return hostname
            except socket.gaierror:
                pass

        # 2. Network scan
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            subnet   = ".".join(local_ip.split(".")[:3])
        except Exception:
            subnet = "192.168.1"

        print(f"[discover] mDNS not found. Scanning {subnet}.1-254 for SSH (~10s)...")
        found = await self._scan_subnet_ssh(subnet)

        if found:
            print(f"[discover] Found {len(found)} SSH-accessible host(s):")
            for i, ip in enumerate(found):
                print(f"  [{i+1}] {ip}")
            ans = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input(f"  Pick [1-{len(found)}] or type IP manually: ").strip()
            )
            # Accept "3", "[3]", or "[ 3 ]" — all mean pick item 3
            ans_stripped = ans.strip("[] \t")
            if ans_stripped.isdigit() and 1 <= int(ans_stripped) <= len(found):
                return found[int(ans_stripped) - 1]
            elif ans:
                return ans  # treat as a literal IP/hostname
        else:
            print("[discover] No SSH hosts found on local network.")

        # 3. Manual
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("  Enter host IP or hostname: ").strip()
        )

    async def _scan_subnet_ssh(self, subnet: str) -> list:
        """Async port-22 scan of an entire /24 subnet."""
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

    # ── Deploy ─────────────────────────────────────────────────────────────

    async def _deploy(self, node_name: str, host: str = ""):
        """
        Deploy an AgentFlow edge node to a remote machine.
        Discovers the host, prompts for credentials, then delegates the
        actual SSH work to the installer agent (node_deploy action).
        """
        # Discover host
        if not host:
            host = await self._discover_host(node_name)
        if not host:
            print("[error] No host found. Aborting.")
            return

        # Credentials
        loop = asyncio.get_event_loop()
        user     = await loop.run_in_executor(None, lambda: input(f"\n  SSH user [pi]: ").strip() or "pi")
        password = await loop.run_in_executor(None, lambda: __import__("getpass").getpass("  SSH password (leave blank for key auth): "))
        broker   = await loop.run_in_executor(None, lambda: input("  MQTT broker IP (this machine's LAN IP): ").strip() or "localhost")

        print(f"\n  Deploying to {user}@{host} as node '{node_name}'...")
        print("  (This may take 20-60s while packages install on the remote machine)")

        if not hasattr(self.agent, "delegate_to_installer"):
            print("[error] delegate_to_installer not available. Is the installer agent running?")
            return

        result = await self.agent.delegate_to_installer({
            "action":    "node_deploy",
            "host":      host,
            "user":      user,
            "password":  password,
            "node_name": node_name,
            "broker":    broker,
        }, timeout=120.0)

        if result.get("success"):
            print(f"""
  Node '{node_name}' is live! It will appear in /nodes within ~15 seconds.

  Now spawn agents on it — just tell main:
    "spawn a CPU monitor agent on {node_name}"
    "spawn a temperature sensor on {node_name}"

  To install extra packages on the Pi before spawning:
    /deploy-pkg {node_name} adafruit-circuitpython-dht RPi.GPIO

  Remote logs on the Pi:
    ~/agentflow/{node_name}.log
""")
        else:
            err = result.get("error", "Unknown error")
            print(f"[error] Deploy failed: {err}")
            if "asyncssh" in err:
                print("  Hint: pip install asyncssh")

    # ── Main loop ──────────────────────────────────────────────────────────

    async def run(self):
        print("\nAgentFlow CLI | Type /help for commands\n")
        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("You: ")
                )
                text = user_input.strip()
                if not text:
                    continue

                if text.lower() in ("quit", "exit"):
                    break

                if text.lower() in ("/help", "help"):
                    self._print_help()
                    continue

                if text.lower() == "/clear-plans":
                    self.agent.persist("_plan_cache", {})
                    print("\n[System: Plan cache cleared.]\n")
                    continue

                if text.lower() in ("/agents", "agents"):
                    agents = await self.agent.list_agents()
                    print()
                    for a in agents:
                        protected = " [protected]" if a.get("protected") else ""
                        node      = f" [{a['node']}]" if a.get("node") else ""
                        print(f"  [{a['state']:8s}] @{a['name']:<22s} {a['actor_id'][:8]}{protected}{node}")
                    print()
                    continue

                if text.lower() in ("/nodes", "nodes"):
                    # Show remote nodes from heartbeat tracking + local agents by node
                    remote_nodes = []
                    if hasattr(self.agent, "list_nodes"):
                        remote_nodes = self.agent.list_nodes()
                    agents = await self.agent.list_agents()

                    # Build local node group from actor registry
                    local_names = [a["name"] for a in agents if not a.get("node")]
                    print()
                    print(f"  {'local':20s} {'online':6s}  {', '.join('@' + n for n in local_names) or '(none)'}")
                    for nd in sorted(remote_nodes, key=lambda x: x["node"]):
                        status = "online" if nd["online"] else "OFFLINE"
                        names  = ', '.join('@' + n for n in nd["agents"]) or '(no agents)'
                        print(f"  {nd['node']:20s} {status:6s}  {names}")
                    if not remote_nodes:
                        print("  (no remote nodes seen yet — deploy one with /deploy <node> <host>)")
                    print()
                    continue

                if text.lower().startswith("/migrate"):
                    # /migrate <agent-name> <target-node>
                    parts = text.split()
                    if len(parts) < 3:
                        print("[usage] /migrate <agent-name> <target-node>")
                        print("        Moves a running agent to a different node.")
                        print("        Example: /migrate temp-sensor rpi-bedroom")
                        print()
                    elif not hasattr(self.agent, "migrate_agent"):
                        print("[error] migrate_agent not available on this actor.\n")
                    else:
                        agent_name  = parts[1]
                        target_node = parts[2]
                        print(f"[Migrating @{agent_name} to {target_node}...]")
                        result = await self.agent.migrate_agent(agent_name, target_node)
                        ok  = result.get("success", False)
                        sym = "OK" if ok else "FAIL"
                        print(f"[{sym}] {result.get('message', '')}\n")
                    continue

                if text.lower().startswith("/deploy-pkg"):
                    # /deploy-pkg <host-ip> <pkg1> [pkg2 ...]
                    parts = text.split()
                    if len(parts) < 3:
                        print("[usage] /deploy-pkg <host-ip> <package> [package2 ...]")
                        print("        e.g.  /deploy-pkg 192.168.1.50 adafruit-circuitpython-dht RPi.GPIO")
                        print()
                    elif not hasattr(self.agent, "delegate_to_installer"):
                        print("[error] installer not available\n")
                    else:
                        host     = parts[1]
                        packages = parts[2:]
                        loop     = asyncio.get_event_loop()
                        user     = await loop.run_in_executor(None, lambda: input("  SSH user [pi]: ").strip() or "pi")
                        password = await loop.run_in_executor(None, lambda: __import__("getpass").getpass("  SSH password: "))
                        print(f"  Installing {packages} on {host}...")
                        result = await self.agent.delegate_to_installer({
                            "action":   "node_install",
                            "host":     host,
                            "user":     user,
                            "password": password,
                            "packages": packages,
                        }, timeout=120.0)
                        ok = result.get("success", False)
                        if ok:
                            print(f"  [OK] {packages} installed on {host}\n")
                        else:
                            print(f"  [FAIL] {result.get('error', result)}\n")
                    continue

                if text.lower().startswith("/deploy"):
                    parts = text.split()
                    if len(parts) < 2:
                        print("[usage] /deploy <node-name> [host]\n")
                    else:
                        host = parts[2] if len(parts) >= 3 else ""
                        await self._deploy(parts[1], host)
                    continue

                if text.startswith("@"):
                    parts      = text[1:].split(" ", 1)
                    agent_name = parts[0].strip()
                    message    = parts[1].strip() if len(parts) > 1 else ""
                    if not message:
                        print(f"[usage] @{agent_name} <your message>\n")
                        continue
                    print(f"\n[routing to @{agent_name}]")
                    target = self.agent._registry.find_by_name(agent_name) if self.agent._registry else None
                    # Stream if target is an LLMAgent with chat_stream support
                    if target and hasattr(target, "chat_stream"):
                        print(f"\n@{agent_name}: ", end="", flush=True)
                        async for chunk in target.chat_stream(message):
                            if not isinstance(chunk, dict):
                                print(chunk, end="", flush=True)
                        print("\n")
                    elif target:
                        response = await self._get_agent_response(agent_name, message)
                        print(f"\n@{agent_name}: {response}\n")
                    else:
                        # Not found locally — try remote nodes
                        response = await self._get_remote_agent_response(agent_name, message)
                        print(f"\n@{agent_name}: {response}\n")
                    continue

                print("\n@main: ", end="", flush=True)
                system_msg = ""
                async for chunk in self.agent.process_user_input_stream(text):
                    if isinstance(chunk, dict):
                        system_msg = chunk.get("system_msg", "")
                    else:
                        print(chunk, end="", flush=True)
                print()  # newline after streamed response
                if system_msg:
                    print(f"[System: {system_msg}]")
                print()

            except (KeyboardInterrupt, EOFError):
                break
        print("\nGoodbye!")


# ─── Discord Interface ──────────────────────────────────────────────────────

class DiscordInterface:
    """
    Discord bot interface. Requires: pip install discord.py
    Set DISCORD_BOT_TOKEN in environment.
    """

    def __init__(self, main_actor: "MainActor", token: str, channel_id: int = None):
        self.agent = main_actor
        self.token = token
        self.channel_id = channel_id

    async def run(self):
        try:
            import discord
        except ImportError:
            logger.error("discord.py not installed. Run: pip install discord.py")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            logger.info(f"[Discord] Logged in as {client.user}")

        @client.event
        async def on_message(message):
            if message.author == client.user:
                return
            if self.channel_id and message.channel.id != self.channel_id:
                return
            if not message.content.startswith("!"):
                return  # Only respond to commands prefixed with !

            text = message.content[1:].strip()
            async with message.channel.typing():
                response = await self.agent.process_user_input(text)
            await message.channel.send(response)

        await client.start(self.token)


# ─── WhatsApp Interface (via Twilio) ───────────────────────────────────────

class WhatsAppInterface:
    """
    WhatsApp via Twilio. Runs an aiohttp webhook server.
    Requires: pip install aiohttp twilio
    Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in environment.
    """

    def __init__(
        self,
        main_actor: "MainActor",
        account_sid: str,
        auth_token: str,
        from_number: str,
        port: int = 8080,
    ):
        self.agent = main_actor
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.port = port

    async def run(self):
        try:
            from aiohttp import web
            from twilio.rest import Client as TwilioClient
        except ImportError:
            logger.error("Missing deps. Run: pip install aiohttp twilio")
            return

        twilio = TwilioClient(self.account_sid, self.auth_token)

        async def webhook(request):
            data = await request.post()
            user_msg = data.get("Body", "")
            from_number = data.get("From", "")
            logger.info(f"[WhatsApp] Message from {from_number}: {user_msg[:60]}")

            response_text = await self.agent.process_user_input(user_msg)

            twilio.messages.create(
                body=response_text,
                from_=f"whatsapp:{self.from_number}",
                to=from_number,
            )
            return web.Response(text="OK")

        app = web.Application()
        app.router.add_post("/webhook/whatsapp", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"[WhatsApp] Webhook server running on port {self.port}")
        await asyncio.Event().wait()  # Run forever


# ─── OpenClaw / Generic REST Interface ────────────────────────────────────

class RESTInterface:
    """
    Generic REST API interface. Connect any chat platform via webhooks.
    POST /chat with {"message": "..."} → returns {"response": "..."}
    """

    def __init__(self, main_actor: "MainActor", port: int = 8000, api_key: str = None):
        self.agent = main_actor
        self.port = port
        self.api_key = api_key

    async def run(self):
        try:
            from aiohttp import web
        except ImportError:
            logger.error("aiohttp not installed. Run: pip install aiohttp")
            return

        async def chat_endpoint(request):
            if self.api_key:
                auth = request.headers.get("X-API-Key")
                if auth != self.api_key:
                    return web.json_response({"error": "Unauthorized"}, status=401)

            body = await request.json()
            message = body.get("message", "")
            if not message:
                return web.json_response({"error": "No message provided"}, status=400)

            response = await self.agent.process_user_input(message)
            return web.json_response({"response": response, "agent": self.agent.name})

        async def agents_endpoint(request):
            agents = await self.agent.list_agents()
            return web.json_response({"agents": agents})

        async def command_endpoint(request):
            body = await request.json()
            target = body.get("target")
            command = body.get("command")
            from ..core.actor import MessageType
            cmd_map = {
                "stop": MessageType.STOP,
                "pause": MessageType.PAUSE,
                "resume": MessageType.RESUME,
            }
            if command in cmd_map and target:
                await self.agent.send_command(target, cmd_map[command])
                return web.json_response({"ok": True})
            return web.json_response({"error": "Invalid command"}, status=400)

        app = web.Application()
        app.router.add_post("/chat", chat_endpoint)
        app.router.add_get("/agents", agents_endpoint)
        app.router.add_post("/agents/command", command_endpoint)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"[REST] API running at http://0.0.0.0:{self.port}")
        await asyncio.Event().wait()