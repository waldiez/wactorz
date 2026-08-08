"""Terminal interface, and the deploy commands it exposes."""

import asyncio
import json
import logging
import socket
import threading
import uuid
from typing import TYPE_CHECKING

from ...config import (
    deploy_env_prefix,
    deploy_target,
    deploy_target_for_host,
    deploy_target_help,
    deploy_target_names,
)
from ...core.mqtt import mqtt_client

if TYPE_CHECKING:
    from ...agents.main_actor import MainActor

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

    def __init__(self, main_actor: "MainActor") -> None:
        self.agent = main_actor

    def _print_help(self) -> None:
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

        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            return "[error] aiomqtt not installed"

        reply_topic = f"main/reply/{main.actor_id}/{uuid.uuid4().hex[:8]}"
        result_holder = []

        async def _listen_for_reply() -> None:
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

    async def _deploy(self, node_name: str) -> None:
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

    async def run(self) -> None:
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
