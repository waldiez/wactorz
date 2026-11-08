# SPDX-License-Identifier: Apache-2.0
"""Terminal interface, and the deploy commands it exposes."""

import asyncio
import json
import logging
import socket
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...agents.main.addresses import registered_agent
from ...config import (
    deploy_env_prefix,
    deploy_target,
    deploy_target_for_host,
    deploy_target_help,
    deploy_target_names,
)
from ...core.addresses import MAIN_NODE, Address, parse
from ...core.mqtt import mqtt_client, user_properties, v5_kwargs
from ...core.node_publications import admit
from ...core.state_snapshot import FORCE_FLAG
from ...core.turns import acting_as, begin_turn
from ...orchestration import CLI

if TYPE_CHECKING:
    from ...agents.main import MainActor
    from ...orchestration import Orchestrator

logger: logging.Logger = logging.getLogger(__name__)


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

    A line that names no agent is the orchestrator's. Main is still held for
    what needs main itself: the lifecycle commands, and reaching an agent that
    is not in this process.
    """

    def __init__(self, orchestrator: "Orchestrator", main_actor: "MainActor") -> None:
        self.orchestrator = orchestrator
        self.agent = main_actor

    def _print_help(self) -> None:
        print("""
  Commands:
    @<n> <msg>               speak directly to a named agent
    /agents                     list all active agents and their state
    /nodes                      list remote nodes (online/offline) and their agents
    /migrate <agent> <node>     move a running agent to a different node
                                e.g.  /migrate temp-sensor rpi-bedroom
                                --force moves it even if some state cannot travel
    /deploy <node-name>         set up a remote machine as an Wactorz node
                                e.g.  /deploy rpi-node
    /help                       show this help
    quit / exit                 shutdown

  Everything else goes to the orchestrator.
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

    def _here(self, address: Address) -> Any:
        """The agent ``address`` names in this process, or None when it is on a node or nowhere."""
        registry = self.agent._registry
        if registry is None or address.node not in ("", MAIN_NODE):
            return None
        return registry.find_by_name(address.name)

    async def _get_remote_agent_response(
        self, agent_name: str, message: str, remote_node: str = ""
    ) -> str:
        """Route a message to ``agent_name`` on ``remote_node`` via MQTT and wait for reply."""
        main = self.agent

        if not remote_node or remote_node == MAIN_NODE:
            known = [a for nd in main._known_nodes.values() for a in nd.get("agents", [])]
            if known:
                return f"[error] Agent '{agent_name}' not found. Remote agents: {', '.join(known)}"
            return f"[error] Agent '{agent_name}' not found. No remote nodes connected."
        refusal = main.nodes.refuses_tasks(remote_node)
        if refusal:
            return f"[error] {agent_name} was not asked: {refusal}"

        reply_topic = f"main/reply/{main.actor_id}/{uuid.uuid4().hex[:8]}"
        result_holder = []

        async def _listen_for_reply() -> None:
            try:
                async with mqtt_client(main._mqtt_broker, main._mqtt_port, **v5_kwargs()) as client:
                    await client.subscribe(reply_topic)
                    async for msg in client.messages:
                        # Any node can answer here: only the one the task went
                        # to is listened to.
                        raw = bytes(msg.payload or b"")
                        if not admit(
                            remote_node, reply_topic, raw, user_properties(msg), "a task reply"
                        ):
                            continue
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
            main.task_topic_for(Address(agent_name, remote_node)),
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
  Node '{node_name}' is live and its first heartbeat has arrived.

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
        future: asyncio.Future[Any] = loop.create_future()

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
        """Read lines from the terminal and answer each, until ``quit``, ``exit``, end of input
        or Ctrl-C.
        """
        print("\nWactorz CLI | Type /help for commands\n")
        while True:
            try:
                text = (await self._prompt("You: ")).strip()
                if not text:
                    continue
                # Everything done to answer this line, wherever it is done.
                begin_turn()
                if text.lower() in ("quit", "exit"):
                    break
                await self._answer(text)
            except (KeyboardInterrupt, EOFError):
                break
        print("\nGoodbye!")

    async def _answer(self, text: str) -> None:
        """Answer one line: a command, a mention of an agent, or the orchestrator's turn."""
        command = self._command_for(text.lower())
        if command is not None:
            await command(text)
        elif text.startswith("@"):
            await self._mention(text)
        else:
            await self._ask_orchestrator(text)

    def _command_for(self, lowered: str) -> Callable[[str], Awaitable[None]] | None:
        """The handler for the command ``lowered`` is, if it is one.

        The words are matched whole, the verbs that take arguments by how the
        line starts: ``/deploy-pkg`` before ``/deploy``, which it also starts with.
        """
        words: dict[str, Callable[[str], Awaitable[None]]] = {
            "/help": self._help,
            "help": self._help,
            "/clear-plans": self._clear_plans,
            "/agents": self._list_agents,
            "agents": self._list_agents,
            "/nodes": self._list_nodes,
            "nodes": self._list_nodes,
        }
        if lowered in words:
            return words[lowered]
        verbs: tuple[tuple[str, Callable[[str], Awaitable[None]]], ...] = (
            ("/migrate", self._migrate),
            ("/deploy-pkg", self._deploy_packages),
            ("/deploy", self._deploy_command),
        )
        for prefix, handler in verbs:
            if lowered.startswith(prefix):
                return handler
        return None

    # ── Commands ───────────────────────────────────────────────────────────

    async def _help(self, _text: str) -> None:
        self._print_help()

    async def _clear_plans(self, _text: str) -> None:
        self.agent.persist("_plan_cache", {})
        print("\n[System: Plan cache cleared.]\n")

    async def _list_agents(self, _text: str) -> None:
        agents = await self.agent.list_agents()
        print()
        for a in agents:
            protected = " [protected]" if a.get("protected") else ""
            node = f" [{a['node']}]" if a.get("node") else ""
            print(f"  [{a['state']:8s}] @{a['name']:<22s} {a['actor_id'][:8]}{protected}{node}")
        print()

    async def _list_nodes(self, _text: str) -> None:
        """The nodes main has heard from, after this process's own agents as ``local``."""
        remote_nodes = self.agent.list_nodes() if hasattr(self.agent, "list_nodes") else []
        agents = await self.agent.list_agents()
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
            print("  (no remote nodes seen yet — deploy one with /deploy <node> <host>)")
        print()

    async def _migrate(self, text: str) -> None:
        """``/migrate <agent-name> <target-node> [--force]``: move a running agent to another node."""
        words = text.split()
        force = FORCE_FLAG in words
        parts = [w for w in words if w != FORCE_FLAG]
        if len(parts) < 3:
            print("[usage] /migrate <agent-name> <target-node> [--force]")
            print("        Moves a running agent to a different node.")
            print("        Example: /migrate temp-sensor rpi-bedroom")
            print()
        elif not hasattr(self.agent, "migrate_agent"):
            print("[error] migrate_agent not available on this actor.\n")
        elif refusal := registered_agent(self.agent, parts[1])[1]:
            print(f"[error] {refusal}\n")
        else:
            print(f"[Migrating @{parse(parts[1]).name} to {parts[2]}...]")
            result = await self.agent.migrate_agent(parts[1], parts[2], force=force)
            sym = "OK" if result.get("success", False) else "FAIL"
            print(f"[{sym}] {result.get('message', '')}\n")

    async def _deploy_packages(self, text: str) -> None:
        """``/deploy-pkg <node-name|host> <pkg1> [pkg2 ...]``: install packages on a node."""
        parts = text.split()
        if len(parts) < 3:
            print("[usage] /deploy-pkg <node-name> <package> [package2 ...]")
            print("        e.g.  /deploy-pkg rpi-kitchen adafruit-circuitpython-dht")
            print()
            return
        if not hasattr(self.agent, "delegate_to_installer"):
            print("[error] installer not available\n")
            return
        # Either the node name or its address: both resolve to the same
        # configured target. The installer reads credentials from the
        # environment and ignores any a payload carries, so none are asked for.
        node, packages = parts[1], parts[2:]
        pkg_target = deploy_target(node) or deploy_target_for_host(node)
        if pkg_target is None:
            print("[error] " + deploy_target_help(node) + "\n")
            return
        host = pkg_target.host or node
        # Typed here, so allowed here: the installer would
        # otherwise ask on the dashboard about what was just typed.
        await self.agent.delegate_to_installer(
            {"action": "packages_allow", "packages": packages}, timeout=10.0
        )
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
        if result.get("success", False):
            print(f"  [OK] {packages} installed on {host}\n")
        else:
            print(f"  [FAIL] {result.get('error', result)}\n")

    async def _deploy_command(self, text: str) -> None:
        """``/deploy <node-name>``: set up a configured target as a node."""
        parts = text.split()
        if len(parts) < 2:
            listing = "\n".join(f"  {n}" for n in deploy_target_names())
            print(
                f"[usage] /deploy <node-name>\nConfigured targets:\n{listing or '  (none configured)'}\n"
            )
        elif len(parts) > 2:
            # A host given here would aim one target's credentials at a machine
            # of the caller's choosing. Targets are whole, or they are not used.
            print("[error] /deploy takes a node name only.\n")
            print(deploy_target_help(parts[1]) + "\n")
        else:
            await self._deploy(parts[1])

    # ── Talking to agents ──────────────────────────────────────────────────

    async def _mention(self, text: str) -> None:
        """``@name <message>``: send the message to that agent, here or on a node."""
        parts = text[1:].split(" ", 1)
        agent_name = parts[0].strip()
        message = parts[1].strip() if len(parts) > 1 else ""
        if not message:
            print(f"[usage] @{agent_name} <your message>\n")
            return
        resolution = self.agent.resolve_address(agent_name)
        if resolution.ambiguous:
            print(f"\n[error] {resolution.refusal(mention=True)}\n")
            return
        found = resolution.found or Address(agent_name)
        if refusal := self.agent.task_refusal(found):
            print(f"\n[error] {refusal}\n")
            return
        print(f"\n[routing to @{found}]")
        await self._say_reply(found, message)

    async def _say_reply(self, found: Address, message: str) -> None:
        """Print the reply of the agent at ``found``, streamed when it streams."""
        agent_name = found.name
        target = self._here(found)
        if target is self.agent:
            # @main goes through the full orchestration pipeline.
            await self._stream_main(agent_name, message)
        elif target and hasattr(target, "chat_stream"):
            print(f"\n@{agent_name}: ", end="", flush=True)
            with acting_as(agent_name):
                async for chunk in target.chat_stream(message):  # pyright: ignore[reportAttributeAccessIssue]
                    if not isinstance(chunk, dict):
                        print(chunk, end="", flush=True)
            print("\n")
        elif target:
            response = await self._get_agent_response(agent_name, message)
            print(f"\n@{agent_name}: {response}\n")
        else:
            # Not found locally — try remote nodes
            response = await self._get_remote_agent_response(agent_name, message, found.node)
            print(f"\n@{agent_name}: {response}\n")

    async def _stream_main(self, agent_name: str, message: str) -> None:
        """Print main's streamed answer, then what it says about the turn."""
        print(f"\n@{agent_name}: ", end="", flush=True)
        system_msg = ""
        with acting_as(self.agent.name):
            async for chunk in self.agent.process_user_input_stream(message):
                if isinstance(chunk, dict):
                    system_msg = chunk.get("system_msg", "")
                else:
                    print(chunk, end="", flush=True)
        print()
        if system_msg:
            print(f"[System: {system_msg}]")
        print()

    async def _ask_orchestrator(self, text: str) -> None:
        print("\n@main: ", end="", flush=True)
        # The orchestrator labels its own work; what it has to say
        # about the turn, it says in the stream.
        async for chunk in self.orchestrator.handle_turn_stream(text, channel=CLI):
            print(chunk, end="", flush=True)
        print()  # newline after streamed response
        print()
