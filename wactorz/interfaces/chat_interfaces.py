"""Chat Interfaces - Connect users to the MainActor via different channels.
Supported: CLI (terminal), Discord, WhatsApp (via Twilio), REST.
"""

import asyncio
import importlib.util
import logging
import os
import socket
import threading
import time
from typing import TYPE_CHECKING

from ..config import (
    CONFIG,
    MAX_REQUEST_BYTES,
    deploy_env_prefix,
    deploy_target,
    deploy_target_for_host,
    deploy_target_help,
    deploy_target_names,
)
from ..core.mqtt import mqtt_client
from .chat.rest import RESTInterface

if TYPE_CHECKING:
    from ..agents.main_actor import MainActor

logger = logging.getLogger(__name__)


async def resolve_host(hostname: str) -> str | None:
    """Resolve `hostname`, or None if it does not resolve.

    Off the event loop: `gethostbyname` blocks the calling thread, and a name
    that does not resolve blocks it for the resolver's full timeout — seconds,
    during which every other actor in the process is frozen and MQTT keepalive
    stops.
    """
    try:
        return await asyncio.to_thread(socket.gethostbyname, hostname)
    except OSError:
        return None


# Import name → pip package for each social channel's optional dependency.
_INTERFACE_DEPENDENCIES = {
    "discord": ("discord", "discord.py"),
    "telegram": ("telegram", "python-telegram-bot"),
}


def missing_dependency(channel: str) -> str | None:
    """Return the pip package a social channel needs, or None if it's importable.

    Checked up front so a missing library warns at startup instead of failing
    silently inside run().
    """
    spec = _INTERFACE_DEPENDENCIES.get(channel)
    if not spec:
        return None
    module_name, pip_name = spec
    return None if importlib.util.find_spec(module_name) else pip_name


class SocialRateLimiter:
    """Per-sender request budget for the social channels.

    Every inbound message costs at least one intent classification plus one
    completion, so an unbounded channel is an unbounded bill. Holds a sliding
    one-minute window per sender and refuses one turn at a time — and refuses
    while that sender's previous turn is still running, so a user cannot stack
    concurrent generations by sending faster than the model replies.
    """

    def __init__(self, per_minute: int | None = None) -> None:
        self.per_minute = CONFIG.social_rate_limit_per_min if per_minute is None else per_minute
        self._hits: dict[str, list[float]] = {}
        self._in_flight: set[str] = set()

    def check(self, sender: str) -> str | None:
        """Return None to proceed, or the message to send back instead."""
        if self.per_minute <= 0:  # 0 disables the limit for trusted deployments
            return None
        if sender in self._in_flight:
            return "Still working on your last message — one at a time, please."
        now = time.monotonic()
        window = [t for t in self._hits.get(sender, []) if now - t < 60.0]
        if len(window) >= self.per_minute:
            self._hits[sender] = window
            return (
                f"That's more than {self.per_minute} messages in a minute, so I'm pausing "
                "for a moment. Try again shortly."
            )
        window.append(now)
        self._hits[sender] = window
        self._in_flight.add(sender)
        return None

    def done(self, sender: str) -> None:
        """Release the in-flight slot. Always call this, including on error."""
        self._in_flight.discard(sender)


def social_channel_blocked(channel: str, token: str, allowed: frozenset) -> str | None:
    """Why ``channel`` must not start, or None when it's safe to.

    A bot with a token but no sender allow-list answers anyone who finds it, and
    on these channels answering means spending tokens and controlling the user's
    home. That is not a safe default, so it fails closed with instructions.
    """
    if not token:
        return None
    missing = missing_dependency(channel)
    if missing:
        return (
            f"'{missing}' is not installed. Run `pip install {missing}` "
            "(or `pip install 'wactorz[all]'`) to enable it."
        )
    if not allowed:
        env = {
            "discord": "DISCORD_ALLOWED_USER_IDS",
            "telegram": "TELEGRAM_ALLOWED_USER_IDS",
        }.get(channel, "the allow-list")
        return (
            f"no sender allow-list configured. Set {env} to the user id(s) allowed "
            "to talk to the bot — without it anyone who finds the bot could control "
            "your devices and spend your LLM budget."
        )
    return None


def build_social_companions(main_actor: "MainActor", primary: str) -> list:
    """Discord/Telegram interfaces to run alongside the primary interface.

    These talk to the main agent in restricted mode (no spawn/delete/code), so
    they run next to the dashboard rather than replacing it — how the HA add-on
    exposes them. Skips a channel that's already the primary (no duplicate login),
    one whose library is missing, and one with no sender allow-list — each with a
    log line saying why. Returns interface objects, not coroutines, so the
    selection is easy to unit-test.
    """
    companions: list = []
    if CONFIG.discord_token and primary != "discord":
        blocked = social_channel_blocked(
            "discord", CONFIG.discord_token, CONFIG.discord_allowed_user_ids
        )
        if blocked:
            logger.warning("Discord companion NOT started: %s", blocked)
        else:
            companions.append(
                DiscordInterface(
                    main_actor,
                    token=CONFIG.discord_token,
                    allowed_user_ids=CONFIG.discord_allowed_user_ids,
                )
            )
            logger.info("Discord companion interface enabled (alongside '%s').", primary)
    if CONFIG.telegram_token and primary != "telegram":
        blocked = social_channel_blocked(
            "telegram", CONFIG.telegram_token, CONFIG.telegram_allowed_user_ids
        )
        if blocked:
            logger.warning("Telegram companion NOT started: %s", blocked)
        else:
            companions.append(
                TelegramInterface(
                    main_actor,
                    token=CONFIG.telegram_token,
                    allowed_user_ids=CONFIG.telegram_allowed_user_ids,
                )
            )
            logger.info("Telegram companion interface enabled (alongside '%s').", primary)
    return companions


def run_all_interfaces(interfaces: list) -> list:
    """Turn companion interface objects into their ``.run()`` coroutines for gather."""
    return [iface.run() for iface in interfaces]


# Path to remote_runner.py inside the wactorz package.
_HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE_RUNNER_PATH = os.path.normpath(os.path.join(_HERE, "..", "remote_runner.py"))


# ─── CLI Interface ──────────────────────────────────────────────────────────


class CLIInterface:
    """Terminal chat interface.

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
    /deploy <node-name>         set up a remote machine as an Wactorz node
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
                return await target.chat(message)  # pyright: ignore[reportAttributeAccessIssue]

            # Case 2: DynamicAgent with a handle_task function
            if hasattr(target, "_fn_handle_task") and target._fn_handle_task:  # pyright: ignore[reportAttributeAccessIssue]
                result = await target._fn_handle_task(  # pyright: ignore[reportAttributeAccessIssue]
                    target._api,  # pyright: ignore[reportAttributeAccessIssue]
                    {"message": message, "text": message, "query": message},
                )
                if isinstance(result, dict):
                    for key in ("reply", "answer", "result", "text", "response"):
                        if result.get(key):
                            return str(result[key])
                    return str(result)
                return str(result) if result else f"[{agent_name}] No response"

            # Case 3: DynamicAgent with llm but no handle_task — direct llm call
            if hasattr(target, "_llm_provider") and target._llm_provider:  # pyright: ignore[reportAttributeAccessIssue]
                return await target._api.llm.chat(message)  # pyright: ignore[reportAttributeAccessIssue]

            # Case 4: any agent with a chat() method
            if hasattr(target, "chat"):
                return await target.chat(message)  # pyright: ignore[reportAttributeAccessIssue]

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

        import json
        import uuid as _uuid

        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            return "[error] aiomqtt not installed"

        reply_topic = f"main/reply/{main.actor_id}/{_uuid.uuid4().hex[:8]}"
        result_holder = []

        async def _listen_for_reply():
            try:
                async with mqtt_client(main._mqtt_broker, main._mqtt_port) as client:
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
            {
                "text": message,
                "payload": message,
                "_remote_task": True,
                "_reply_topic": reply_topic,
            },
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

    # ── Deploy ─────────────────────────────────────────────────────────────

    async def _deploy(self, node_name: str):
        """Deploy a Wactorz edge node to a configured remote machine.

        The target — host, user, SSH auth, broker — comes from the environment
        (``DEPLOY_TARGETS`` plus a ``DEPLOY_<NODE>_*`` block). This used to
        prompt for an SSH password at the terminal and, with no host, port-scan
        the local /24 for open SSH ports; both are gone. The scan told anyone
        who could reach this command where every SSH server on the LAN was, and
        the typed password was handed straight to the installer as message data.
        """
        target = deploy_target(node_name)
        if target is None:
            print("[error] " + deploy_target_help(node_name))
            return

        host = target.host
        if not host:
            # One name lookup for one host — no sweep of the network.
            print(f"[discover] No host configured for '{node_name}' — trying mDNS...")
            host = await resolve_host(f"{node_name}.local") or ""
            if not host:
                print(
                    f"[error] Could not resolve '{node_name}.local'. "
                    f"Set {deploy_env_prefix(node_name)}_HOST in your environment."
                )
                return
            print(f"[discover] Found via mDNS: {node_name}.local → {host}")

        print(f"\n  Deploying to {target.user}@{host} as node '{node_name}'...")
        print("  (This may take 20-60s while packages install on the remote machine)")

        if not hasattr(self.agent, "delegate_to_installer"):
            print("[error] delegate_to_installer not available. Is the installer agent running?")
            return

        result = await self.agent.delegate_to_installer(
            {
                "action": "node_deploy",
                "host": host,
                "node_name": target.name,
                "broker": target.broker or "localhost",
                "port": target.broker_port,
            },
            timeout=120.0,
        )

        if result.get("success"):
            print(f"""
  Node '{node_name}' is live! It will appear in /nodes within ~15 seconds.

  Now spawn agents on it — just tell main:
    "spawn a CPU monitor agent on {node_name}"
    "spawn a temperature sensor on {node_name}"

  To install extra packages on the Pi before spawning:
    /deploy-pkg {node_name} adafruit-circuitpython-dht RPi.GPIO

  Remote logs on the Pi:
    ~/wactorz/{node_name}.log
""")
        else:
            err = result.get("error", "Unknown error")
            print(f"[error] Deploy failed: {err}")
            if "asyncssh" in err:
                print("  Hint: pip install asyncssh")

    # ── Main loop ──────────────────────────────────────────────────────────

    @staticmethod
    async def _prompt(prompt: str) -> str:
        """Read one line from stdin without blocking the loop *or* interpreter exit.

        ``run_in_executor`` puts the read on the default thread pool, whose
        threads are non-daemon: cancelling the await leaves the thread parked in
        ``input()`` forever, and shutdown then waits for it twice — once in
        ``Runner.close``'s ``shutdown_default_executor``, once in the threading
        atexit hook. That is why stopping the CLI used to need repeated Ctrl-C.
        Owning a daemon thread means a parked read never delays exit.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def _read() -> None:
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                loop.call_soon_threadsafe(lambda: future.done() or future.cancel())
                return
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result(line))

        threading.Thread(target=_read, daemon=True, name="wactorz-cli-stdin").start()
        return await future

    async def run(self):
        print("\nWactorz CLI | Type /help for commands\n")
        while True:
            try:
                user_input = await self._prompt("You: ")
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
                        node = f" [{a['node']}]" if a.get("node") else ""
                        print(
                            f"  [{a['state']:8s}] @{a['name']:<22s} {a['actor_id'][:8]}{protected}{node}"
                        )
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
                    print(
                        f"  {'local':20s} {'online':6s}  {', '.join('@' + n for n in local_names) or '(none)'}"
                    )
                    for nd in sorted(remote_nodes, key=lambda x: x["node"]):
                        status = "online" if nd["online"] else "OFFLINE"
                        names = ", ".join("@" + n for n in nd["agents"]) or "(no agents)"
                        print(f"  {nd['node']:20s} {status:6s}  {names}")
                    if not remote_nodes:
                        print(
                            "  (no remote nodes seen yet — deploy one with /deploy <node> <host>)"
                        )
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
                        agent_name = parts[1]
                        target_node = parts[2]
                        print(f"[Migrating @{agent_name} to {target_node}...]")
                        result = await self.agent.migrate_agent(agent_name, target_node)
                        ok = result.get("success", False)
                        sym = "OK" if ok else "FAIL"
                        print(f"[{sym}] {result.get('message', '')}\n")
                    continue

                if text.lower().startswith("/deploy-pkg"):
                    # /deploy-pkg <node-name|host> <pkg1> [pkg2 ...]
                    parts = text.split()
                    if len(parts) < 3:
                        print("[usage] /deploy-pkg <node-name> <package> [package2 ...]")
                        print("        e.g.  /deploy-pkg rpi-kitchen adafruit-circuitpython-dht")
                        print()
                    elif not hasattr(self.agent, "delegate_to_installer"):
                        print("[error] installer not available\n")
                    else:
                        # Accept either the node name or its address; both resolve
                        # to the same configured target. This used to prompt for
                        # an SSH password at the terminal and put it in the task
                        # payload — the installer now reads credentials from the
                        # environment and ignores any a payload carries.
                        node = parts[1]
                        packages = parts[2:]
                        pkg_target = deploy_target(node) or deploy_target_for_host(node)
                        if pkg_target is None:
                            print("[error] " + deploy_target_help(node) + "\n")
                            continue
                        host = pkg_target.host or node
                        print(f"  Installing {packages} on {host}...")
                        result = await self.agent.delegate_to_installer(
                            {
                                "action": "node_install",
                                "host": host,
                                "node_name": pkg_target.name,
                                "packages": packages,
                            },
                            timeout=120.0,
                        )
                        ok = result.get("success", False)
                        if ok:
                            print(f"  [OK] {packages} installed on {host}\n")
                        else:
                            print(f"  [FAIL] {result.get('error', result)}\n")
                    continue

                if text.lower().startswith("/deploy"):
                    parts = text.split()
                    if len(parts) < 2:
                        listing = "\n".join(f"  {n}" for n in deploy_target_names())
                        print(
                            f"[usage] /deploy <node-name>\nConfigured targets:\n{listing or '  (none configured)'}\n"
                        )
                    elif len(parts) > 2:
                        # The old form took a host override here, which would aim
                        # one target's credentials at a machine of the caller's
                        # choosing. Targets are whole, or they are not used.
                        print("[error] /deploy takes a node name only.\n")
                        print(deploy_target_help(parts[1]) + "\n")
                    else:
                        await self._deploy(parts[1])
                    continue

                if text.startswith("@"):
                    parts = text[1:].split(" ", 1)
                    agent_name = parts[0].strip()
                    message = parts[1].strip() if len(parts) > 1 else ""
                    if not message:
                        print(f"[usage] @{agent_name} <your message>\n")
                        continue
                    print(f"\n[routing to @{agent_name}]")
                    target = (
                        self.agent._registry.find_by_name(agent_name)
                        if self.agent._registry
                        else None
                    )
                    # @main goes through the full orchestration pipeline
                    if target is self.agent:
                        print(f"\n@{agent_name}: ", end="", flush=True)
                        system_msg = ""
                        async for chunk in self.agent.process_user_input_stream(message):
                            if isinstance(chunk, dict):
                                system_msg = chunk.get("system_msg", "")
                            else:
                                print(chunk, end="", flush=True)
                        print()
                        if system_msg:
                            print(f"[System: {system_msg}]")
                        print()
                        continue
                    # Stream if target is an LLMAgent with chat_stream support
                    if target and hasattr(target, "chat_stream"):
                        print(f"\n@{agent_name}: ", end="", flush=True)
                        async for chunk in target.chat_stream(message):  # pyright: ignore[reportAttributeAccessIssue]
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
    """Discord bot interface. Requires: pip install discord.py
    Set DISCORD_BOT_TOKEN in environment.
    """

    def __init__(
        self,
        main_actor: "MainActor",
        token: str,
        channel_id: int | None = None,
        allowed_user_ids: frozenset[int] | set[int] | None = None,
    ):
        self.agent = main_actor
        self.token = token
        self.channel_id = channel_id
        self.allowed_user_ids = frozenset(allowed_user_ids or ())
        self.limiter = SocialRateLimiter()

    async def run(self):
        try:
            import discord
        except ImportError:
            logger.error("discord.py not installed. Run: pip install discord.py")
            return

        # Fail closed: a bot that answers anyone can drain the LLM budget and
        # control the user's home. Refuse to log in without an allow-list.
        if not self.allowed_user_ids:
            logger.error(
                "[Discord] Not starting: DISCORD_ALLOWED_USER_IDS is empty. Set it to the "
                "Discord user id(s) allowed to talk to the bot (enable Developer Mode, "
                "right-click your name, Copy User ID)."
            )
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            logger.info(f"[Discord] Logged in as {client.user}")

        @client.event
        async def on_message(message):
            me = client.user
            if me is None:
                # None until the login handshake finishes; a message cannot be
                # attributed to us before that, so there is nothing to answer.
                return
            if message.author == me:
                return
            if self.channel_id and message.channel.id != self.channel_id:
                return
            if not me.mentioned_in(message):
                return  # Only respond when the bot is mentioned
            if message.author.id not in self.allowed_user_ids:
                logger.warning("[Discord] Rejected message from user %s", message.author.id)
                return

            sender = str(message.author.id)
            throttled = self.limiter.check(sender)
            if throttled:
                await message.channel.send(throttled)
                return

            text = message.content.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()
            # Restricted mode: converse + control devices, no spawn/delete/code.
            try:
                async with message.channel.typing():
                    response = await self.agent.process_user_input_restricted(text)
            finally:
                self.limiter.done(sender)
            for i in range(0, len(response), 2000):
                await message.channel.send(response[i : i + 2000])

        await client.start(self.token)


# ─── Telegram Interface ─────────────────────────────────────────────────────


class TelegramInterface:
    """Telegram bot interface.

    With no allow-list the bot runs in **setup mode**: it answers `/start` with
    the sender's user id and nothing else. That keeps the documented way of
    finding your id working (the id is needed to fill the allow-list) without
    letting a stranger reach the LLM or the user's devices.
    """

    def __init__(
        self,
        main_actor: "MainActor",
        token: str,
        allowed_user_id: int | None = None,
        allowed_user_ids: frozenset[int] | set[int] | None = None,
    ):
        self.agent = main_actor
        self.token = token
        # allowed_user_id (singular) is the older single-user form; fold it in.
        ids = set(allowed_user_ids or ())
        if allowed_user_id:
            ids.add(int(allowed_user_id))
        self.allowed_user_ids = frozenset(ids)
        self.limiter = SocialRateLimiter()

    async def run(self):
        try:
            from telegram import Update
            from telegram.constants import ChatAction
            from telegram.ext import (
                Application,
                CommandHandler,
                ContextTypes,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
            return

        setup_mode = not self.allowed_user_ids
        if setup_mode:
            logger.warning(
                "[Telegram] TELEGRAM_ALLOWED_USER_IDS is empty — starting in setup mode. "
                "The bot will only reply to /start with your user id; add that id to "
                "TELEGRAM_ALLOWED_USER_IDS (or the add-on option) and restart to enable chat."
            )

        async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            uid = user.id if user else "unknown"
            if not update.message:
                return
            if setup_mode:
                await update.message.reply_text(
                    f"Your Telegram user id is: {uid}\n\n"
                    "This bot isn't configured yet. Add that id to "
                    "TELEGRAM_ALLOWED_USER_IDS (or the add-on's telegram_allowed_user_id "
                    "option) and restart Wactorz to start chatting."
                )
                return
            await update.message.reply_text(
                f"Hi {user.first_name if user else ''}. Telegram interface is online.\n"
                f"Your Telegram user id is: {uid}"
            )

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.message.text:
                return

            user = update.effective_user
            if not user:
                return

            logger.info(
                "[Telegram] Message from id=%s username=%s: %s",
                user.id,
                user.username,
                update.message.text[:60],
            )

            if setup_mode:
                await update.message.reply_text(
                    "This bot isn't configured yet. Send /start to get your user id."
                )
                return

            if user.id not in self.allowed_user_ids:
                logger.warning("[Telegram] Rejected message from user %s", user.id)
                return

            sender = str(user.id)
            throttled = self.limiter.check(sender)
            if throttled:
                await update.message.reply_text(throttled)
                return

            text = update.message.text.strip()
            if not update.effective_chat:
                return
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.TYPING
            )

            # Restricted mode: converse + control devices, no spawn/delete/code.
            try:
                response = await self.agent.process_user_input_restricted(text)
            finally:
                self.limiter.done(sender)
            response = response or "(no response)"

            for i in range(0, len(response), 4096):
                await update.message.reply_text(response[i : i + 4096])

        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logger.info("[Telegram] Bot starting (polling)...")
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling()
        await asyncio.Event().wait()


# ─── WhatsApp Interface (via Twilio) ───────────────────────────────────────


class WhatsAppInterface:
    """WhatsApp via Twilio. Runs an aiohttp webhook server.
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
        allowed_numbers: frozenset[str] | set[str] | None = None,
    ):
        self.agent = main_actor
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.port = port
        # The webhook is a public HTTP endpoint, so anyone who finds it can spend
        # tokens unless senders are pinned. Numbers are compared without the
        # `whatsapp:` prefix Twilio adds.
        self.allowed_numbers = frozenset(
            self._normalize_number(n) for n in (allowed_numbers or CONFIG.whatsapp_allowed_numbers)
        )
        self.limiter = SocialRateLimiter()

    @staticmethod
    def _normalize_number(number: str) -> str:
        return str(number or "").strip().replace("whatsapp:", "")

    async def _send_message(self, twilio, body: str, to: str) -> None:
        """Send one WhatsApp message, off the event loop.

        The Twilio SDK is synchronous, so calling it directly from the webhook
        handler froze every actor in the process for a full network round-trip —
        on every inbound message.
        """
        await asyncio.to_thread(
            twilio.messages.create,
            body=body,
            from_=f"whatsapp:{self.from_number}",
            to=to,
        )

    async def run(self):
        try:
            from aiohttp import web
            from twilio.rest import Client as TwilioClient
        except ImportError:
            logger.error("Missing deps. Run: pip install aiohttp twilio")
            return

        if not self.allowed_numbers:
            logger.error(
                "[WhatsApp] Not starting: WHATSAPP_ALLOWED_NUMBERS is empty. The webhook is a "
                "public endpoint — set it to the number(s) allowed to message the bot "
                "(e.g. WHATSAPP_ALLOWED_NUMBERS=+306912345678)."
            )
            return

        twilio = TwilioClient(self.account_sid, self.auth_token)

        async def webhook(request):
            data = await request.post()
            user_msg = data.get("Body", "")
            from_number = data.get("From", "")
            logger.info(f"[WhatsApp] Message from {from_number}: {user_msg[:60]}")

            sender = self._normalize_number(from_number)
            if sender not in self.allowed_numbers:
                logger.warning("[WhatsApp] Rejected message from %s (not allow-listed)", sender)
                return web.Response(text="OK")

            throttled = self.limiter.check(sender)
            if throttled:
                await self._send_message(twilio, throttled, from_number)
                return web.Response(text="OK")

            # Restricted mode: same guarantees as the other social channels.
            try:
                response_text = await self.agent.process_user_input_restricted(user_msg)
            finally:
                self.limiter.done(sender)

            await self._send_message(twilio, response_text, from_number)
            return web.Response(text="OK")

        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.router.add_post("/webhook/whatsapp", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"[WhatsApp] Webhook server running on port {self.port}")
        await asyncio.Event().wait()  # Run forever


__all__ = [
    "REMOTE_RUNNER_PATH",
    "CLIInterface",
    "DiscordInterface",
    "RESTInterface",
    "SocialRateLimiter",
    "TelegramInterface",
    "WhatsAppInterface",
    "build_social_companions",
    "missing_dependency",
    "resolve_host",
    "run_all_interfaces",
    "social_channel_blocked",
]
