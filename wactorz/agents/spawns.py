"""Putting an agent somewhere and remembering that it is there.

The local half already lives in `mixins/spawning.py`, shared with the planner
so every caller constructs an agent the same way. What is here is the rest: the
remote path over MQTT, the registry that survives a restart, and the catalogue
checks that decide whether a name means something already maintained.

Two configs leave the remote path and they are deliberately different. The wire
carries synthesized bridge code, because the runner on a node only knows how to
run DynamicAgent-shaped source. The registry keeps the clean config, so the
bridge is regenerated on each spawn instead of accumulating there and being
carried home again on a migrate-back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..core.actor import MessageType
from .helpers.main_actor_helpers import SPAWN_REGISTRY_KEY, _parse_spawn_config

if TYPE_CHECKING:
    from ..core.actor import Actor
    from .hosts import SpawnHost
    from .mixins.spawning import SpawnPlaceholder

logger = logging.getLogger(__name__)

#: How long to wait for a node's packages before spawning without them.
INSTALL_TIMEOUT_S = 180.0

#: How long a catalogue spawn is given to appear in the registry.
CATALOGUE_SETTLE_S = 0.5


#: The body given to a `type: "llm"` agent spawned on another machine.
#:
#: A node's runner only knows how to run DynamicAgent-shaped source: it compiles
#: what it is sent and looks for `handle_task`. An LLM agent has no code of its
#: own, so this is synthesized to give it one.
#:
#: **No API key leaves main.** The generated agent calls `agent.chat(...)`, which
#: on the node is an RPC back over `main/llm_request` — the whole reason the
#: bridge exists rather than shipping credentials to the edge.
#:
#: It persists through `agent.persist`/`recall`, which reach the node's own state
#: file. A history sent along as `_initial_state` is picked up by the first
#: `recall`, so an agent keeps its memory across a migrate and back again.
#:
#: The system prompt is baked in at synthesis time rather than looked up at
#: runtime, quoted with `json.dumps` so a quote or newline in it cannot end the
#: string early. History is bounded, or the prompt grows for as long as the agent
#: lives.
LLM_BRIDGE_CODE_TEMPLATE = """
# ── Auto-generated LLM bridge (synthesized by main when this agent was spawned
# remotely with type: "llm"). Do not edit; replace the agent if you need to
# change behavior. ─────────────────────────────────────────────────────────
import time
from datetime import datetime

_SYSTEM_PROMPT = {system_prompt_literal}
_MAX_HISTORY   = {max_history}


def _now_block():
    # Recomputed every task so the agent always sees the real present moment
    # (process-local zone — honors the host TZ env var). Kept self-contained so
    # the synthesized agent has no import dependency on llm_agent. Newlines are
    # built with chr(10) to avoid backslash-escaping through the code template.
    _n = datetime.now().astimezone()
    _nl = chr(10)
    return (
        "== CURRENT DATE & TIME (live) ==" + _nl
        + "It is now " + _n.strftime("%A, %d %B %Y, %H:%M %Z")
        + " (UTC" + _n.strftime("%z") + ")." + _nl
        + "Trust this over training data; resolve 'today'/'tomorrow' against it."
        + _nl + _nl
    )

async def setup(agent):
    # Conversation history may have been shipped via _initial_state at spawn
    # time — agent.recall() finds it. If this is a fresh spawn, default to
    # an empty list.
    agent.state["history"] = list(agent.recall("conversation_history", []) or [])

async def handle_task(agent, payload):
    # Accept the same shapes LLMAgent does: dict with text/task/message/query,
    # or a bare string. Anything else gets str()'d so the LLM at least gets
    # something to look at instead of crashing on a non-string.
    if isinstance(payload, dict):
        text = (
            payload.get("text")
            or payload.get("task")
            or payload.get("message")
            or payload.get("query")
            or str(payload)
        )
    else:
        text = str(payload) if payload is not None else ""

    started = time.time()
    history = agent.state.setdefault("history", [])
    history.append({{"role": "user", "content": text, "ts": started}})

    # Keep the prompt bounded — only send the last _MAX_HISTORY turns to the LLM.
    safe_history = [
        {{"role": m["role"], "content": str(m["content"])}}
        for m in history[-_MAX_HISTORY:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    response = await agent.chat(safe_history, system=_now_block() + _SYSTEM_PROMPT)
    duration = time.time() - started

    history.append({{"role": "assistant", "content": response, "ts": time.time()}})
    # Trim in-memory list too so it doesn't grow forever between persists.
    if len(history) > _MAX_HISTORY * 2:
        del history[: len(history) - _MAX_HISTORY * 2]
    agent.persist("conversation_history", history)

    return {{"text": response, "task": text[:60], "duration": duration}}
"""


class SpawnService:
    """Spawns agents, here or on another machine, and remembers which."""

    def __init__(self, host: SpawnHost) -> None:
        self.host = host

    def _get_spawn_registry(self) -> dict[str, Any]:
        return self.host.recall(SPAWN_REGISTRY_KEY) or {}

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        reg = self._get_spawn_registry()
        reg[config["name"]] = config
        self.host.persist(SPAWN_REGISTRY_KEY, reg)
        logger.info("[%s] Spawn registry: %s", self.host.name, list(reg.keys()))

    def _remove_from_spawn_registry(self, name: str) -> None:
        reg = self._get_spawn_registry()
        if name in reg:
            del reg[name]
            self.host.persist(SPAWN_REGISTRY_KEY, reg)
            logger.info("[%s] Removed %r from spawn registry.", self.host.name, name)

    def _supervised_names(self) -> set[str]:
        """The agents the Supervisor has already brought up.

        Both it and restore spawn user agents at startup, and both register
        under the same deterministic actor id, so a second spawn overwrites the
        registry entry while the first instance keeps its MQTT subscriptions
        and every message arrives twice. Nothing about that is visible from
        here, which is why the names are checked rather than the outcome.
        """
        registry = self.host._registry
        supervisor = getattr(registry, "_supervisor_ref", None) if registry else None
        if supervisor is None:
            return set()
        return {
            name
            for name, spec in supervisor._specs.items()
            if spec.actor is not None and not spec.retired
        }

    async def _restore_one(self, name: str, config: dict[str, Any]) -> None:
        """Bring one agent back, wherever it was running.

        Never saves: this is reading the registry to rebuild from it, and
        writing as it goes would rewrite every entry on every startup.
        """
        node = config.get("node", "").strip()
        if node:
            logger.info("[%s] Re-spawning remote agent %r on node %r", self.host.name, name, node)
            await self._spawn_remote(config, node, save=False)
            return
        if self.host._registry and self.host._registry.find_by_name(name):
            logger.info("[%s] %r already running, skipping.", self.host.name, name)
            return
        await self._spawn_from_config(config, save=False, from_registry=True)
        logger.info("[%s] Restored: %s", self.host.name, name)

    async def _restore_spawned_agents(self) -> None:
        """Put back everything the registry says should be running."""
        registry = self._get_spawn_registry()
        if not registry:
            return

        supervised = self._supervised_names()
        skipped = sorted(n for n in registry if n in supervised)
        if skipped:
            logger.info(
                "[%s] Supervisor already restarted %s agent(s); skipping restore for: %s",
                self.host.name,
                len(skipped),
                skipped,
            )

        pending = {n: c for n, c in registry.items() if n not in supervised}
        if not pending:
            return

        logger.info("[%s] Restoring %s agent(s): %s", self.host.name, len(pending), list(pending))
        for name, config in pending.items():
            try:
                await self._restore_one(name, config)
            except Exception:
                # One agent that will not start must not strand the rest.
                logger.exception("[%s] Failed to restore %r", self.host.name, name)

    async def _resolve_or_spawn(self, agent_name: str) -> tuple[Any, bool]:
        """Resolve an agent by name, auto-spawning it from a catalog recipe if it
        isn't running yet. Shared by @mention delegations and <delegate> blocks.

        Returns (target_actor_or_None, spawnable_bool).
        """
        target = self.host._registry.find_by_name(agent_name) if self.host._registry else None
        spawnable = False
        if not target:
            manifest = self.host._agent_manifests.get(agent_name, {})
            # Fall back to a fuzzy catalog match so 'smart energy agent' or
            # 'smart-energy-agent' still resolve to the 'smart-energy' recipe.
            if not manifest:
                matched = self.host._match_catalog_recipe(agent_name)
                if matched:
                    agent_name = matched
                    manifest = self.host._agent_manifests.get(matched, {})
            spawnable = bool(manifest.get("spawnable") and manifest.get("catalog"))
            if spawnable:
                catalog_actor = (
                    self.host._registry.find_by_name(manifest["catalog"])
                    if self.host._registry
                    else None
                )
                if catalog_actor and hasattr(catalog_actor, "_action_spawn"):
                    logger.info("[%s] Auto-spawning %r via catalog...", self.host.name, agent_name)
                    try:
                        spawn_result = await catalog_actor._action_spawn(agent_name, {})  # pyright: ignore[reportAttributeAccessIssue]
                        if spawn_result and spawn_result.get("ok"):
                            await asyncio.sleep(0.5)
                            target = (
                                self.host._registry.find_by_name(agent_name)
                                if self.host._registry
                                else None
                            )
                            logger.info("[%s] %r spawned successfully", self.host.name, agent_name)
                        else:
                            err = (
                                spawn_result.get("message", "unknown")
                                if spawn_result
                                else "no response"
                            )
                            logger.warning(
                                "[%s] Spawn failed for %r: %s", self.host.name, agent_name, err
                            )
                    except Exception:
                        logger.exception("[%s] Spawn error for %r", self.host.name, agent_name)
        return target, spawnable

    async def _catalogue_substitute(self, requested: str) -> Any:
        """The maintained agent that already covers `requested`, if there is one.

        Asked for something the catalogue provides, the catalogue's version is
        what should run — not the one the model just wrote. Otherwise "spawn the
        smart energy agent" produces fresh unreviewed code every time, under a
        name that only resembles the real one.

        Returns None when nothing covers it, or when the catalogue was asked and
        could not deliver: the model's own code is the fallback, so a broken
        catalogue does not leave the user with nothing.
        """
        registry = self.host._registry
        recipe = self.host._match_catalog_recipe(requested)
        logger.info("[%s] spawn %r: catalog-dedup match = %r", self.host.name, requested, recipe)
        if not recipe or not registry:
            return None

        running = registry.find_by_name(recipe)
        if running:
            logger.info(
                "[%s] %r already covered by running catalog agent %r — skipping "
                "LLM reimplementation",
                self.host.name,
                requested,
                recipe,
            )
            return running

        catalogue = registry.find_by_name("catalog")
        if not (catalogue and hasattr(catalogue, "_action_spawn")):
            return None

        logger.info(
            "[%s] LLM tried to write %r; spawning catalog recipe %r instead",
            self.host.name,
            requested,
            recipe,
        )
        result = await catalogue._action_spawn(recipe, {})  # pyright: ignore[reportAttributeAccessIssue]
        if result and result.get("ok"):
            await asyncio.sleep(CATALOGUE_SETTLE_S)
            spawned = registry.find_by_name(recipe)
            if spawned:
                return spawned
        logger.warning(
            "[%s] Catalog spawn of %r failed (%s); falling back to LLM code",
            self.host.name,
            recipe,
            result,
        )
        return None

    def _is_runnable(self, config: dict[str, Any]) -> bool:
        """Whether this config describes something that could actually start.

        A dynamic agent with no code is a model that stopped mid-sentence, not
        an agent. An LLM agent without a prompt gets a default, because one that
        answers generically is more use than one that never arrives.
        """
        agent_type = config.get("type", "dynamic")
        if agent_type == "dynamic" and not (config.get("code", "") or "").strip():
            logger.error("[%s] Dynamic agent has no code: %s", self.host.name, config.get("name"))
            return False
        if agent_type == "llm" and not (config.get("system_prompt", "") or "").strip():
            logger.warning(
                "[%s] LLM agent has no system_prompt, using default: %s",
                self.host.name,
                config.get("name"),
            )
        return True

    async def _process_spawn_commands(self, response: str) -> tuple[str, list[Any]]:
        """Run every `<spawn>` block the model wrote, and strip them from the reply."""
        pattern = r"<spawn>(.*?)</spawn>"
        spawned: list[Any] = []

        for match in re.findall(pattern, response, re.DOTALL):
            try:
                config = _parse_spawn_config(match.strip())
                if not config.get("replace"):
                    substitute = await self._catalogue_substitute(config.get("name", ""))
                    if substitute is not None:
                        spawned.append(substitute)
                        continue
                if not self._is_runnable(config):
                    continue
                actor = await self._spawn_from_config(config, save=True)
                if actor:
                    spawned.append(actor)
            except Exception:
                # One bad block must not cost the user the rest of the reply.
                logger.exception("[%s] Spawn failed\nRaw block:\n%s", self.host.name, match[:500])

        clean = re.sub(pattern, "", response, flags=re.DOTALL).strip()
        return clean, spawned

    async def _spawn_from_config(
        self, config: dict[str, Any], save: bool = True, *, from_registry: bool = False
    ) -> Actor | SpawnPlaceholder | None:
        """Spawn one agent from a config dict.

        Remote (node-targeted) spawns go out over MQTT via ``_spawn_remote``;
        everything local is handled by the shared ``SpawnMixin`` so main and the
        planner construct agents identically.

        ``from_registry`` says the config was read back from the spawn registry,
        which is what entitles it to a ``trusted`` flag. It defaults to False so
        a new call site is untrusted until someone decides otherwise.
        """
        node = config.get("node", "").strip()
        if node:
            return await self._spawn_remote(config, node, save)
        return await self.host._spawn_local_from_config(
            config, register=save, from_registry=from_registry
        )

    def _inject_llm_bridge_code(self, config: dict[str, Any]) -> dict[str, Any]:
        """If ``config`` is for an LLM-typed agent without code, return a copy
        with synthesized bridge code so the remote runner can actually run it.

        No-op (returns the original config) when:
          - the config already has code (caller provided their own logic)
          - the config isn't type "llm" (DynamicAgent, ha_actuator, etc. are
            handled directly by the runner already)

        Why a copy: callers may pass the same config dict for multiple writes
        (spawn registry, desired_state, MQTT publish) and we don't want to
        mutate it under their feet.
        """
        if not isinstance(config, dict):
            return config
        if (config.get("code") or "").strip():
            return config
        if (config.get("type") or "").strip().lower() != "llm":
            return config

        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        max_history = int(config.get("max_history", 32) or 32)
        bridge = LLM_BRIDGE_CODE_TEMPLATE.format(
            system_prompt_literal=json.dumps(system_prompt),
            max_history=max_history,
        )

        out = dict(config)
        out["code"] = bridge
        # description and capabilities are commonly already set by the LLM
        # at spawn time; fill in sensible defaults if not so list_capabilities
        # still works on the remote side.
        out.setdefault(
            "description",
            f"LLM-driven agent (remote bridge). System prompt: "
            f"{system_prompt[:80].rstrip()}{'...' if len(system_prompt) > 80 else ''}",
        )
        out.setdefault("input_schema", {"text": "str — the question or request"})
        out.setdefault("output_schema", {"text": "str — the LLM response"})
        logger.info(
            "[%s] Synthesized LLM bridge code for %r: %s chars, max_history=%s",
            self.host.name,
            out.get("name"),
            len(bridge),
            max_history,
        )
        return out

    def _node_address(self, node: str) -> str | None:
        """Where to reach `node`, from whichever source knows.

        Three sources in order of how current they are: the live heartbeat
        table, then the spawn registry, then whatever the installer recorded at
        deploy time. Only the address — the installer resolves the SSH user and
        credentials from the node's own configured target, so none of that
        travels.
        """
        host = self.host._known_nodes.get(node, {}).get("host")
        if host:
            return host
        for cfg in self._get_spawn_registry().values():
            if cfg.get("node") == node and cfg.get("host"):
                return cfg["host"]
        if self.host._registry:
            installer = self.host._registry.find_by_name("installer")
            if installer:
                return installer.recall(f"node_host_{node}")
        return None

    async def _install_packages(self, node: str, agent_name: str, packages: list[str]) -> None:
        """Install `packages` on `node` before its agent is spawned.

        Best-effort by design. An agent that starts without its packages fails
        on the import, which names the problem; one that never starts because a
        pip call was slow is a silent loss.
        """
        address = self._node_address(node)
        if not address:
            logger.warning(
                "[%s] No host known for node %r — cannot pre-install %s. "
                "Install manually: ssh into %s and run: pip install %s --break-system-packages",
                self.host.name,
                node,
                packages,
                node,
                " ".join(packages),
            )
            return

        installer = self.host._registry.find_by_name("installer") if self.host._registry else None
        if installer is None:
            # Reported apart from the missing-address case on purpose: the node
            # is reachable and the fix is to get the installer running, not to
            # go looking for an address that is already known.
            logger.warning(
                "[%s] installer not found — skipping remote package install for %r",
                self.host.name,
                agent_name,
            )
            return

        logger.info(
            "[%s] Installing %s on %s (%s) before spawn...",
            self.host.name,
            packages,
            node,
            address,
        )
        task_id = f"remote_install_{uuid.uuid4().hex[:8]}"
        future = asyncio.get_running_loop().create_future()
        self.host._result_futures[task_id] = future
        # The node's name rather than its credentials: the installer already
        # holds those, and putting them in a message would spread them.
        await self.host.send(
            installer.actor_id,
            MessageType.TASK,
            {
                "action": "node_install",
                "host": address,
                "packages": packages,
                "node_name": node,
                "_task_id": task_id,
                "task": task_id,
            },
        )
        try:
            result = await asyncio.wait_for(future, timeout=INSTALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("[%s] Remote install timed out — spawning anyway", self.host.name)
        else:
            if result.get("success"):
                logger.info("[%s] Remote install OK: %s", self.host.name, packages)
            else:
                logger.warning(
                    "[%s] Remote install issue: %s", self.host.name, result.get("error", "?")
                )
        finally:
            self.host._result_futures.pop(task_id, None)

    async def _spawn_remote(self, config: dict[str, Any], node: str, save: bool) -> None:
        """Publish a spawn command to a node, which runs the agent there.

        A remote agent appears in the dashboard like a local one, because both
        talk to the same broker.

        Two configs leave here. The wire carries synthesized bridge code, since
        the runner only knows DynamicAgent-shaped source. The registry keeps the
        clean one, so the bridge is regenerated per spawn rather than
        accumulating there and riding home on a migrate-back.

        The node's desired state is retained, so a node that reboots reconciles
        itself back to running this agent without being told again.
        """
        wire_config = self._inject_llm_bridge_code(config)
        name = wire_config.get("name", "remote-agent")

        packages = wire_config.get("install", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]

        logger.info("[%s] Spawning %r on remote node %r", self.host.name, name, node)
        if packages:
            await self._install_packages(node, name, packages)

        await self.host._mqtt_publish(f"nodes/{node}/spawn", wire_config, retain=True, qos=1)
        await self.host._update_node_desired_state(node, wire_config)
        await self.host._mqtt_publish(
            f"agents/{self.host.actor_id}/logs",
            {
                "type": "spawned",
                "message": f"Spawned '{name}' on node '{node}'",
                "child_name": name,
                "node": node,
                "timestamp": time.time(),
            },
        )

        if save:
            self._save_to_spawn_registry(config)
