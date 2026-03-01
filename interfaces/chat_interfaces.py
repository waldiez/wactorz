"""
Chat Interfaces - Connect users to the MainActor via different channels.
Supported: CLI (terminal), Discord, WhatsApp (via Twilio / OpenClaw).
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agents.main_actor import MainActor

logger = logging.getLogger(__name__)


# ─── CLI Interface ─────────────────────────────────────────────────────────

class CLIInterface:
    """
    Terminal chat interface.

    Commands:
      @agent-name <message>   — speak directly to a named agent
      /agents                 — list all active agents
      /help                   — show commands
      quit / exit             — shutdown
    """

    def __init__(self, main_actor: "MainActor"):
        self.agent = main_actor

    def _print_help(self):
        print("""
  Commands:
    @<name> <msg>   speak directly to a named agent  (e.g. @code-agent write a sort function)
    /agents         list all active agents and their state
    /help           show this help
    quit / exit     shutdown
  Anything else goes to the main orchestrator.
""")

    async def _get_agent_response(self, agent_name: str, message: str) -> str:
        """Route a message directly to a named agent and get its response."""
        registry = self.agent._registry
        if registry is None:
            return "[error] No registry available."

        target = registry.find_by_name(agent_name)
        if target is None:
            # List available agents to help user
            names = [a.name for a in registry.all_actors()]
            return f"[error] No agent named '{agent_name}'. Available: {', '.join(names)}"

        # Check if agent has a chat() method (LLM-based)
        if hasattr(target, "chat"):
            try:
                response = await target.chat(message)
                return response
            except Exception as e:
                return f"[error] {agent_name} failed: {e}"

        # For non-LLM agents, send a TASK message and wait for RESULT
        result = await self.agent.delegate_task(agent_name, message, timeout=60.0)
        if result:
            return str(result.get("result", result))
        return f"[{agent_name}] Task sent (no response — agent may not support direct messaging)"

    async def run(self):
        print(f"\nAgentFlow CLI | Type /help for commands\n")
        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("You: ")
                )
                text = user_input.strip()
                if not text:
                    continue

                # ── Exit ──────────────────────────────────────────────
                if text.lower() in ("quit", "exit"):
                    break

                # ── Help ──────────────────────────────────────────────
                if text.lower() in ("/help", "help"):
                    self._print_help()
                    continue

                # ── List agents ───────────────────────────────────────
                if text.lower() in ("/agents", "agents"):
                    agents = await self.agent.list_agents()
                    print()
                    for a in agents:
                        protected = " [protected]" if a.get("protected") else ""
                        print(f"  [{a['state']:8s}] @{a['name']:<20s} {a['actor_id'][:8]}{protected}")
                    print()
                    continue

                # ── Direct agent addressing: @agent-name <message> ────
                if text.startswith("@"):
                    parts = text[1:].split(" ", 1)
                    agent_name = parts[0].strip()
                    message    = parts[1].strip() if len(parts) > 1 else ""
                    if not message:
                        print(f"[usage] @{agent_name} <your message>\n")
                        continue
                    print(f"\n[routing to @{agent_name}]")
                    response = await self._get_agent_response(agent_name, message)
                    print(f"\n@{agent_name}: {response}\n")
                    continue

                # ── Default: send to main orchestrator ────────────────
                response = await self.agent.process_user_input(text)
                print(f"\n@main: {response}\n")

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