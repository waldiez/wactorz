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
    @<name> <msg>          speak directly to a named agent
    /agents                list all active agents and their state
    /nodes                 list connected remote nodes and their agents
    /deploy <node-name>    set up a remote machine as an AgentFlow node
                           auto-discovers via mDNS then network scan
                           e.g.  /deploy rpi-node
    /help                  show this help
    quit / exit            shutdown

  Everything else goes to the main orchestrator.
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
            if ans.isdigit() and 1 <= int(ans) <= len(found):
                return found[int(ans) - 1]
            elif ans:
                return ans
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
        SSH into a remote machine, upload remote_runner.py, install deps, start runner.
        Host is discovered automatically if not provided.
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
        password = await loop.run_in_executor(None, lambda: __import__("getpass").getpass("  SSH password: "))
        ssh_port = await loop.run_in_executor(None, lambda: input("  SSH port [22]: ").strip() or "22")
        broker   = await loop.run_in_executor(None, lambda: input("  MQTT broker IP (this PC's LAN IP): ").strip() or "localhost")

        try:
            import asyncssh
        except ImportError:
            print("\n[error] asyncssh not installed. Run: pip install asyncssh")
            return

        # Find remote_runner.py
        runner = REMOTE_RUNNER_PATH
        if not os.path.exists(runner):
            runner = "remote_runner.py"   # fallback to cwd
        if not os.path.exists(runner):
            print("[error] remote_runner.py not found. Make sure it's in the project root.")
            return

        print(f"\n  Deploying to {user}@{host}:{ssh_port} as node '{node_name}'")

        try:
            async with asyncssh.connect(
                host, port=int(ssh_port), username=user, password=password, known_hosts=None
            ) as conn:
                print("  [1/4] Connected ✓")

                await conn.run("mkdir -p ~/agentflow", check=True)

                async with conn.start_sftp_client() as sftp:
                    await sftp.put(runner, f"/home/{user}/agentflow/remote_runner.py")
                print("  [2/4] Uploaded remote_runner.py ✓")

                res = await conn.run(
                    "pip install aiomqtt psutil --break-system-packages -q 2>&1 | tail -2",
                    check=False
                )
                print(f"  [3/4] Dependencies installed ✓")
                if res.stdout.strip():
                    print(f"        {res.stdout.strip()}")

                # Kill any existing runner for this node name
                await conn.run(
                    f"pkill -f 'remote_runner.py.*--name {node_name}' 2>/dev/null; true",
                    check=False
                )

                cmd = (
                    f"nohup python3 ~/agentflow/remote_runner.py "
                    f"--broker {broker} --name {node_name} "
                    f"> ~/agentflow/{node_name}.log 2>&1 &"
                )
                await conn.run(cmd, check=True)
                print(f"  [4/4] Remote runner started ✓")

                print(f"""
  Node '{node_name}' is live! It will appear in /nodes shortly.
  Remote logs: {user}@{host}:~/agentflow/{node_name}.log

  Now spawn agents on it:
    @main spawn an agent on {node_name} that monitors CPU and RAM
    @main spawn an agent on {node_name} that reads from the Pi camera and runs YOLO
""")

        except asyncssh.PermissionDenied:
            print("[error] Permission denied. Check username/password.")
        except asyncssh.ConnectionLost:
            print(f"[error] Connection lost to {host}.")
        except Exception as e:
            print(f"[error] Deploy failed: {e}")

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
                    agents = await self.agent.list_agents()
                    nodes  = {}
                    for a in agents:
                        n = a.get("node") or "local"
                        nodes.setdefault(n, []).append(a["name"])
                    print()
                    for node, names in sorted(nodes.items()):
                        print(f"  {node:20s} {', '.join('@' + n for n in names)}")
                    print()
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
                    else:
                        response = await self._get_agent_response(agent_name, message)
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
