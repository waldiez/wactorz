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
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from ...core.actor import ReplyError, ask_through, derive_actor_id
from ...core.addresses import MAIN_NODE, Address, agent_name_error
from ...core.artifacts import ArtifactError
from ...core.bundles import BundleError, bundle_for
from ...core.identity import given_actor_id
from ...core.packages import (
    APPROVAL_TIMEOUT_S,
    DEFAULT_ALLOWED,
    allowed_by_env,
    unlisted,
)
from ...core.topics import topic_name_error
from ...plugins import MODULE_TYPE, for_target, is_module_config
from ..catalog_agent import current_recipe_config
from ..mixins import KeptHere, NotSpawned, PlacedOnNode, agent_type
from .alongside import Settled, put_back, settle
from .spawn_registry import (
    NAME_KEYED_REGISTRY_KEY,
    SPAWN_REGISTRY_KEY,
    SpawnRegistry,
    address_of,
    placed_on,
    without_transient_keys,
)

if TYPE_CHECKING:
    from ...core.actor import Actor
    from ..mixins.spawning import SpawnPlaceholder
    from .hosts import SpawnHost

logger = logging.getLogger(__name__)


def why_a_node_cannot_run(config: dict[str, Any], node_targets: Collection[str] = ()) -> str | None:
    """Why a node could not run the agent ``config`` describes, or None if it can.

    A node runs an agent as generated code: what the config's ``code`` holds,
    or, for an agent of type ``llm``, the bridge code main writes for it. A
    module agent it builds from a package installed there, so one is placed
    only on a node whose manifest names the target among ``node_targets``.
    Every other kind of agent is a class built into this server -- a native
    catalogue agent, a Home Assistant actuator, a scheduled or rule agent --
    and its config carries no program. Sent to a node, it starts as an agent
    with nothing to run, answers every message with an error, and is reported
    as started all the same.
    """
    if (config.get("code") or "").strip():
        return None
    kind = agent_type(config)
    if kind == "llm":
        return None
    if kind == "dynamic":
        return "it has no program to send there"
    if kind == MODULE_TYPE:
        target = str(config.get("target") or "").strip()
        if target and (target in node_targets or isinstance(config.get("bundle"), dict)):
            return None
        return (
            f"it is built from {target or 'a target it does not name'}, which that node has "
            f"not said it has, and its code could not be sent with it. Redeploy the node so it "
            f"takes the code from main, or install the package there and name the agent in "
            f"WACTORZ_AGENTS, or list it as a wactorz.agents entry point"
        )
    return (
        f"it is a {kind} agent, built into this server, and a node runs only an "
        f"agent whose program goes with it: generated code, an LLM agent, or a library "
        f"agent from a package installed there"
    )


def catalogue_may_stand_in(config: dict[str, Any]) -> bool:
    """Whether the catalogue's agent of the same name may run in place of ``config``.

    The catalogue stands in for code the model wrote, so a recipe it maintains
    is not rewritten, unreviewed, every time it is asked for; and for a native
    agent, which only the catalogue builds. A spawn of any other kind -- an LLM,
    rule, scheduled, Home Assistant or module agent -- describes a different
    agent that happens to share the name, and runs as asked.
    """
    return agent_type(config) in ("dynamic", "native")


def with_models_of(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` naming, as ``artifacts``, the models a library agent declares in its code.

    A spawn that names artifacts itself keeps them. The agent's own
    declaration -- ``models=`` on the decorator, ``MODELS`` on a class -- is
    what main has and the node does not, so it goes with the spawn.
    """
    if not is_module_config(config) or config.get("artifacts"):
        return config
    plugin = for_target(str(config.get("target") or ""))
    if plugin is None or not plugin.models:
        return config
    return {**config, "artifacts": plugin.models}


async def with_code_for(
    config: dict[str, Any], node_targets: Collection[str], takes_bundles: bool
) -> tuple[dict[str, Any], str | None]:
    """``config`` as a node can build it, and why its code cannot go, or None.

    A library agent whose target the node has installed is built from that.
    Otherwise, on a node that takes them, its code goes with the spawn as a
    bundle -- the folder its module is in, see `wactorz.core.bundles` -- made
    again from the code here each time, so the node runs what main runs, with
    the packages the agent requires to be installed there. A config that is
    not a library agent's is returned as it is.
    """
    if not is_module_config(config):
        return config, None
    target = str(config.get("target") or "").strip()
    if not target or target in node_targets or not takes_bundles:
        return config, None
    plugin = for_target(target)
    if plugin is None:
        # Not built here, so there is nothing to send: a bundle the config
        # already carries is the one it was placed with.
        return config, None
    try:
        # It reads and parses every file in the folder: off the loop.
        bundle = await asyncio.to_thread(bundle_for, target)
    except BundleError as exc:
        return config, str(exc)
    bundled = {**config, "bundle": bundle}
    packages = plugin.requires.get("packages")
    if packages and not config.get("install"):
        bundled["install"] = [str(p) for p in packages]
    return bundled, None


def id_on_node(name: str, kept: str | None, takes_ids: bool | None) -> str:
    """The id an agent sent to a node runs under there.

    ``kept`` is the id the agent already has, if any; it goes with it. ``takes_ids``
    is whether the node runs an agent under the id its spawn names, as its
    manifest says: None before the manifest has arrived. A new agent is given a
    fresh id only on a node that says it honours one; elsewhere it gets its
    name's derived id, which every node arrives at by itself.
    """
    if kept is not None:
        return kept
    return str(uuid.uuid4()) if takes_ids else derive_actor_id(name)


def ids_refused(name: str, kept: str | None, takes_ids: bool | None) -> bool:
    """Whether a node would run the agent under another id than the one it has.

    So for an agent with an id of its own, minted rather than derived, on a node
    whose manifest says it does not honour one. A node not heard from yet is
    not refused: at a restart, main restores before every manifest arrives.
    """
    return kept is not None and kept != derive_actor_id(name) and takes_ids is False


def ids_not_honoured(node: str) -> str:
    """Why an agent with an id of its own is not sent to ``node``, and what to do."""
    return (
        f"{node} runs from a release that gives an agent an id derived from its name, so it "
        f"would run this one under another id. Redeploy it with `/deploy {node}` first."
    )


def _parse_spawn_config(raw: str) -> dict:
    """Robustly parse a spawn config that may contain raw multiline code strings.
    Uses character scanning to correctly handle } and " inside the code value.
    """
    raw = raw.strip()

    # Strategy 1: standard JSON (works when LLM properly escapes newlines)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: backtick-delimited code (rare but some LLMs use it)
    bt_match = re.search(r'"code"\s*:\s*`(.*?)`', raw, re.DOTALL)
    if bt_match:
        code_raw = bt_match.group(1)
        placeholder = re.sub(r'"code"\s*:\s*`.*?`', '"code": "__CODE__"', raw, flags=re.DOTALL)
        config = json.loads(placeholder)
        config["code"] = code_raw
        return config

    # Strategy 3: locate the code value's bounds, then swap it out for a
    # placeholder so the surrounding object parses as normal JSON.
    #
    # We can't find the value's end by scanning forward for the first unescaped
    # '"': the code is raw (that's why strategies 1/2 failed) and legitimately
    # contains its own double quotes — e.g. a Python string like
    # 'phrases like "Vamos!"'. A forward scan stops at that first inner quote,
    # truncates the code, and corrupts the placeholder ("Expecting ',' delimiter").
    #
    # Instead, treat every '"' after the opening one as a candidate closing
    # quote, right-most first, and keep the first that makes the REST of the
    # object valid JSON.
    #
    # NB the right-most candidate wins whenever it parses, so a key written
    # *after* `code` is absorbed into the code value and silently lost. Code is
    # the last field by convention, which is why that rarely bites — but these
    # configs are model-authored, so the convention is a hope, not a guarantee.
    # `test_a_field_after_code_is_silently_swallowed` pins the current answer.
    key_match = re.search(r'"code"\s*:\s*"', raw)
    if not key_match:
        msg = f"No 'code' key found in spawn config:\n{raw[:200]}"
        raise ValueError(msg)

    code_start = key_match.end()  # index right after the opening "
    prefix = raw[: key_match.start()]
    quote_positions = [code_start + m.start() for m in re.finditer(r'"', raw[code_start:])]

    config = None
    code_raw = ""
    for close in reversed(quote_positions):
        placeholder = prefix + '"code": "__CODE__"' + raw[close + 1 :]
        try:
            config = json.loads(placeholder)
        except json.JSONDecodeError:
            continue
        code_raw = raw[code_start:close]
        break

    if config is None:
        msg = (
            "Spawn config JSON invalid after code extraction: no closing quote for "
            f"the code value produced parseable JSON.\nRaw:\n{raw[:300]}"
        )
        raise ValueError(msg)

    # Unescape sequences the LLM may have added. Decode as a real JSON string in
    # one correct pass (json.loads handles \\, \n, \t, \", \uXXXX, … together and,
    # crucially, collapses an escaped backslash instead of leaving it doubled).
    # strict=False tolerates the raw newlines/tabs that made the top-level
    # json.loads fail in the first place. Falling back to sequential .replace()
    # would re-introduce the original bug: a Python escape like \' arrives here as
    # \\' (JSON-escaped), and without collapsing \\ the string literal terminates
    # early — stranding any following non-ASCII char (e.g. an em-dash) outside the
    # string and raising SyntaxError only once the runner compiles it.
    try:
        config["code"] = json.loads('"' + code_raw + '"', strict=False)
    except json.JSONDecodeError:
        # Best-effort fallback for genuinely invalid escapes (e.g. a bare \');
        # collapse the common sequences with the backslash LAST so it doesn't
        # corrupt the ones decoded before it.
        config["code"] = (
            code_raw.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return config


#: How long to wait for a node's packages before spawning without them: the
#: operator's answer about a package not on the list, then pip over SSH.
INSTALL_TIMEOUT_S = APPROVAL_TIMEOUT_S + 180.0

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

    def _get_spawn_registry(self) -> SpawnRegistry:
        """What main remembers about the agents it spawned: see `SpawnRegistry`."""
        return SpawnRegistry.stored(
            self.host.recall(SPAWN_REGISTRY_KEY), self.host.recall(NAME_KEYED_REGISTRY_KEY)
        )

    def _registered_actor_id(self, name: str, node: str | None = None) -> str | None:
        """The id the registry keeps for ``name`` -- on ``node``, or its only entry -- or None."""
        reg = self._get_spawn_registry()
        entry = reg.only(name) if node is None else reg.at(name, node)
        return entry["actor_id"] if entry else None

    def _entry_saved_over(
        self, reg: SpawnRegistry, config: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The entry a save of ``config`` rewrites, or None when it adds one.

        The entry of the id the config carries; then the one of its name on its
        node; then, unless the config asks for a second agent beside it, the
        only one of its name, which a move rewrites with the node it went to.
        """
        name = config["name"]
        entry = reg.by_id(given_actor_id(config.get("actor_id")))
        if entry is not None and entry["name"] == name:
            return entry
        entry = reg.at(name, str(config.get("node") or ""))
        if entry is not None or config.get("alongside"):
            return entry
        return reg.only(name)

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        """Record ``config``, keeping the id of the entry it rewrites.

        Many places save an entry -- a spawn, a move, a code or model update --
        and most hand over a config with no id, or one read before the agent had
        one. The id is settled here instead: the one the entry it rewrites keeps
        (see `_entry_saved_over`); for a new entry, the agent of that name
        running here, then the id the spawn gave it, then the name's derived id.
        """
        reg = self._get_spawn_registry()
        name = config["name"]
        kept = self._entry_saved_over(reg, config)
        running = self.host._registry.find_by_name(name) if self.host._registry else None
        if placed_on(config):
            running = None
        actor_id = (
            (kept["actor_id"] if kept is not None else None)
            or (running.actor_id if running is not None else None)
            or given_actor_id(config.get("actor_id"))
            or derive_actor_id(name)
        )
        if kept is None and reg.by_id(actor_id) is not None:
            # A new entry under another agent's id would overwrite that agent's:
            # a second agent of the name, with no id of its own yet, gets one.
            actor_id = str(uuid.uuid4())
        reg = reg.with_entry({**without_transient_keys(config), "actor_id": actor_id})
        self.host.persist(SPAWN_REGISTRY_KEY, reg.stored_form())
        logger.info("[%s] Spawn registry: %s", self.host.name, sorted(reg.names()))

    def _remove_from_spawn_registry(
        self, name: str, node: str | None = None, *, actor_id: str | None = None
    ) -> None:
        """Forget the agent ``name``: the one ``actor_id`` is, the one on ``node``, or its only one.

        A bare name kept in more than one place names none of them, and
        nothing is removed.
        """
        reg = self._get_spawn_registry()
        entry = reg.by_id(actor_id) if actor_id else None
        if entry is None or entry["name"] != name:
            entry = reg.only(name) if node is None else reg.at(name, node)
        if entry is None:
            if len(reg.named(name)) > 1:
                logger.warning(
                    "[%s] Not removing %r from the spawn registry: it is kept on %s",
                    self.host.name,
                    name,
                    ", ".join(str(address_of(e)) for e in reg.named(name)),
                )
            return
        self.host.persist(SPAWN_REGISTRY_KEY, reg.without(entry["actor_id"]).stored_form())
        logger.info("[%s] Removed %s from spawn registry.", self.host.name, address_of(entry))

    def _clear_spawn_registry(self) -> None:
        """Forget every agent main spawned."""
        self.host.persist(SPAWN_REGISTRY_KEY, {})

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

    async def _restore_one(self, config: dict[str, Any]) -> None:
        """Bring one agent back, wherever it was running.

        Never saves: this is reading the registry to rebuild from it, and
        writing as it goes would rewrite every entry on every startup.
        """
        name = config["name"]
        config = current_recipe_config(config)
        node = placed_on(config)
        if node:
            logger.info("[%s] Re-spawning remote agent %r on node %r", self.host.name, name, node)
            await self._spawn_remote(config, node, save=False, restoring=True)
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

        # Only main's own: the Supervisor starts nothing on a node.
        supervised = self._supervised_names()
        here = [e for e in registry if not placed_on(e)]
        skipped = sorted(e["name"] for e in here if e["name"] in supervised)
        if skipped:
            logger.info(
                "[%s] Supervisor already restarted %s agent(s); skipping restore for: %s",
                self.host.name,
                len(skipped),
                skipped,
            )

        pending = [e for e in registry if placed_on(e) or e["name"] not in supervised]
        if not pending:
            return

        listed = [str(address_of(e)) for e in pending]
        logger.info("[%s] Restoring %s agent(s): %s", self.host.name, len(pending), listed)
        for config in pending:
            try:
                await self._restore_one(config)
            except Exception:
                # One agent that will not start must not strand the rest.
                logger.exception("[%s] Failed to restore %s", self.host.name, address_of(config))

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
        if result and result.get("elsewhere"):
            return KeptHere(recipe, MAIN_NODE, tuple(result["elsewhere"]))
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
        an agent; one with a prompt and no code is an LLM agent (see
        :func:`agent_type`). An LLM agent without a prompt gets a default,
        because one that answers generically is more use than one that never
        arrives.
        """
        kind = agent_type(config)
        if kind == "dynamic" and not (config.get("code", "") or "").strip():
            logger.error("[%s] Dynamic agent has no code: %s", self.host.name, config.get("name"))
            return False
        if kind == "llm" and not (config.get("system_prompt", "") or "").strip():
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
                asked_for_one = config.get("replace") or config.get("alongside")
                if not asked_for_one and catalogue_may_stand_in(config):
                    substitute = await self._catalogue_substitute(config.get("name", ""))
                    if substitute is not None:
                        spawned.append(substitute)
                        continue
                if not self._is_runnable(config):
                    continue
                result = await self.spawn_placed(config)
                if result:
                    spawned.append(result)
            except Exception:
                # One bad block must not cost the user the rest of the reply.
                logger.exception("[%s] Spawn failed\nRaw block:\n%s", self.host.name, match[:500])

        clean = re.sub(pattern, "", response, flags=re.DOTALL).strip()
        return clean, spawned

    async def settle_spawn(self, config: dict[str, Any]) -> Settled:
        """Decide a spawn made outside this service, by the catalog; see `alongside.settle`."""
        return await settle(self.host, config)

    async def put_back_spawn(self, source: Address) -> None:
        """Start the agent a settled spawn stopped at ``source``; see `alongside.put_back`."""
        await put_back(self.host, source)

    async def spawn_placed(self, config: dict[str, Any]) -> Any:
        """Place ``config``, settle it against where its name already runs, and spawn it.

        What the turn reports: the agent, where it was placed, or why it was
        not spawned (see `alongside.settle`). A move whose spawn does not
        happen puts the agent back where it was.
        """
        placed = await self.host.placement.place(config)
        if isinstance(placed, NotSpawned):
            logger.warning("[%s] Not spawning %r: %s", self.host.name, placed.name, placed.reason)
            return placed
        settled = await settle(self.host, placed)
        if settled.refused is not None:
            return settled.refused
        actor = await self._spawn_from_config(placed, save=True)
        if not actor and settled.moved_from is not None:
            await put_back(self.host, settled.moved_from)
        return actor

    async def _spawn_from_config(
        self, config: dict[str, Any], save: bool = True, *, from_registry: bool = False
    ) -> Actor | SpawnPlaceholder | PlacedOnNode | None:
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
            if not await self._spawn_remote(config, node, save, restoring=from_registry):
                return None
            return PlacedOnNode(
                str(config.get("name", "remote-agent")), node, node in self.host._known_nodes
            )
        return await self.host._spawn_local_from_config(
            config, register=save, from_registry=from_registry
        )

    def _inject_llm_bridge_code(self, config: dict[str, Any]) -> dict[str, Any]:
        """If ``config`` is for an LLM-typed agent without code, return a copy
        with synthesized bridge code so the remote runner can actually run it.

        No-op (returns the original config) when:
          - the config already has code (caller provided their own logic)
          - the config isn't type "llm": a node runs nothing else without code,
            and `why_a_node_cannot_run` keeps such a config off it

        Why a copy: callers may pass the same config dict for multiple writes
        (spawn registry, desired_state, MQTT publish) and we don't want to
        mutate it under their feet.
        """
        if not isinstance(config, dict):
            return config
        if (config.get("code") or "").strip():
            return config
        if agent_type(config) != "llm":
            return config

        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        max_history = int(config.get("max_history", 32) or 32)
        bridge = LLM_BRIDGE_CODE_TEMPLATE.format(
            system_prompt_literal=json.dumps(system_prompt),
            max_history=max_history,
        )

        out = dict(config)
        out["code"] = bridge
        # Named, so the config still reads as an LLM agent now it carries code:
        # a move home takes the bridge code out again by this.
        out["type"] = "llm"
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
        for cfg in self._get_spawn_registry().on_node(node):
            if cfg.get("host"):
                return cfg["host"]
        if self.host._registry:
            installer = self.host._registry.find_by_name("installer")
            if installer:
                return installer.recall(f"node_host_{node}")
        return None

    async def _packages_cleared(self, agent_name: str, packages: list[str]) -> str | None:
        """Why ``packages`` may not be installed for ``agent_name``, or None when they may.

        The installer decides, asking the operator about a package not on the
        list. With no installer running, only what is on the list without
        asking is cleared.
        """
        if self.host._registry is None or self.host._registry.find_by_name("installer") is None:
            refused = unlisted(packages, DEFAULT_ALLOWED | allowed_by_env())
            if refused:
                return (
                    f"no installer is running to ask about {', '.join(refused)}, "
                    "which the list of packages installed without asking does not hold"
                )
            return None
        request = {"action": "packages_clear", "packages": packages, "agent": agent_name}
        try:
            await ask_through(self.host, "installer", request, timeout=APPROVAL_TIMEOUT_S + 30)
        except ReplyError as exc:
            return str(exc.reply.get("error", "the installer refused its packages"))
        except (asyncio.TimeoutError, RuntimeError) as exc:
            return f"its packages could not be cleared: {exc}"
        return None

    async def _packages_placed(
        self, config: dict[str, Any], node: str, packages: list[str], restoring: bool
    ) -> bool:
        """Clear ``packages`` for the agent in ``config`` and install them on ``node``.

        False when they are refused; the refusal is then on the dashboard and
        in the log. Cleared first: the node installs what the spawn lists, whether or
        not it could be installed from here. An agent put back where it ran
        (``restoring``) is not asked about again: its packages were cleared
        when it was first placed, and main saves no other.
        """
        name = str(config.get("name", "remote-agent"))
        if not restoring:
            refusal = await self._packages_cleared(name, packages)
            if refusal:
                await self._refuse_remote_spawn(config, node, refusal)
                return False
        await self._install_packages(node, name, packages, restoring=restoring)
        return True

    async def _install_packages(
        self, node: str, agent_name: str, packages: list[str], *, restoring: bool = False
    ) -> None:
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
        # The node's name rather than its credentials: the installer already
        # holds those, and putting them in a message would spread them.
        request = {
            # A restore installs without asking, as on main: see `_packages_placed`.
            "action": "node_install_restored" if restoring else "node_install",
            "host": address,
            "packages": packages,
            "node_name": node,
            "agent": agent_name,
        }
        try:
            result = await ask_through(self.host, "installer", request, timeout=INSTALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("[%s] Remote install timed out — spawning anyway", self.host.name)
        except ReplyError as exc:
            logger.warning(
                "[%s] Remote install issue: %s", self.host.name, exc.reply.get("error", "?")
            )
        except RuntimeError as exc:
            logger.warning("[%s] Remote install not sent: %s", self.host.name, exc)
        else:
            if isinstance(result, dict) and result.get("success"):
                logger.info("[%s] Remote install OK: %s", self.host.name, packages)
            else:
                logger.warning("[%s] Remote install issue: %s", self.host.name, result)

    async def pinned_for(
        self, config: dict[str, Any], node: str
    ) -> tuple[dict[str, Any], str | None]:
        """``config`` with every model it names pinned for ``node``, or why one cannot be.

        A library agent's own models are named first. What is saved and what
        is sent then name the same versions, so an agent restarted from its
        record runs the model it ran. A copy: the caller's dict may be written
        elsewhere too. A migration runs this before it stops the agent: an
        artifact that is not stored would otherwise be found only after the
        stop, with the agent running nowhere.
        """
        config = with_models_of(config)
        if not config.get("artifacts"):
            return config, None
        try:
            pinned = await self.host.artifact_service.pin_for_node(config["artifacts"], node)
        except ArtifactError as exc:
            return config, f"its artifacts cannot be sent: {exc}"
        return {**config, "artifacts": pinned}, None

    def _kept_actor_id(self, name: str, node: str, *, alongside: bool = False) -> str | None:
        """The id the agent ``name`` spawned onto ``node`` already has, or None for a new one.

        The registry's entry for it on that node; unless the spawn adds a second
        agent of the name (``alongside``), then the only entry of the name,
        which a move keeps; then the agent of that name running here. Never the
        spawn config's own, which a model may have written.
        """
        reg = self._get_spawn_registry()
        entry = reg.at(name, node)
        if entry is not None:
            return entry["actor_id"]
        if alongside:
            return None
        entry = reg.only(name)
        if entry is not None:
            return entry["actor_id"]
        running = self.host._registry.find_by_name(name) if self.host._registry else None
        return running.actor_id if running is not None else None

    async def _refuse_remote_spawn(self, config: dict[str, Any], node: str, reason: str) -> None:
        """Say in the log and on the dashboard why an agent was not sent to ``node``."""
        name = config.get("name")
        logger.error("[%s] Cannot spawn %r on %s: %s", self.host.name, name, node, reason)
        await self.host._mqtt_publish(
            f"agents/{self.host.actor_id}/logs",
            {
                "type": "error",
                "message": f"Cannot spawn '{name}' on '{node}': {reason}",
                "child_name": name,
                "node": node,
                "timestamp": time.time(),
            },
        )

    async def _node_cannot_take(self, config: dict[str, Any], node: str, restoring: bool) -> bool:
        """Whether ``node`` itself rules the agent out, said where the user looks if so."""
        mismatch = self.host._node_version_mismatch(node)
        if mismatch:
            # Before the install, the spawn and the desired state: a node on
            # other code must not be handed an agent it may not be able to run,
            # and must not be told to keep running it after a reboot.
            logger.error("[%s] Cannot spawn %r: %s", self.host.name, config.get("name"), mismatch)
            await self.host._mqtt_publish(
                f"agents/{self.host.actor_id}/logs",
                {
                    "type": "error",
                    "message": f"Cannot spawn '{config.get('name')}': {mismatch}",
                    "child_name": config.get("name"),
                    "node": node,
                    "timestamp": time.time(),
                },
            )
            return True
        name = str(config.get("name", ""))
        kept = self._kept_actor_id(name, node, alongside=bool(config.get("alongside")))
        if ids_refused(name, kept, self.host._node_takes_actor_ids(node)):
            await self._refuse_remote_spawn(config, node, ids_not_honoured(node))
            return True
        unheard = None if restoring else self.host._node_refuses_tasks(node)
        if unheard:
            # Sent anyway, the agent would run while main ignored everything it
            # said, and whoever spawned it would wait for it to appear until
            # they gave up. Restoring is let through: the node's retained
            # desired state already names the agent, and it is heard from once
            # the node is deployed again.
            await self._refuse_remote_spawn(config, node, f"{unheard}.")
            return True
        return False

    async def _spawn_remote(
        self, config: dict[str, Any], node: str, save: bool, *, restoring: bool = False
    ) -> bool:
        """Publish a spawn command to a node, which runs the agent there; False if refused.

        A remote agent appears in the dashboard like a local one, because both
        talk to the same broker.

        Two configs leave here. The wire carries synthesized bridge code, since
        the runner only knows DynamicAgent-shaped source. The registry keeps the
        clean one, so the bridge is regenerated per spawn rather than
        accumulating there and riding home on a migrate-back.

        The node's desired state is retained, so a node that reboots reconciles
        itself back to running this agent without being told again. The spawn
        message deliberately is not: see the note on the publish itself.
        """
        name_check = topic_name_error if restoring or config.get("replace") else agent_name_error
        problem = name_check(str(config.get("name", "remote-agent")))
        if problem:
            # As for a local spawn: the name is a level of the agent's topics,
            # and a new one may not hold the address separator.
            logger.error(
                "[%s] Cannot spawn %r on %s: %s",
                self.host.name,
                config.get("name"),
                node,
                problem,
            )
            return False
        # The node's targets are asked for only when they decide: a host that
        # cannot say -- a test's stand-in -- places code and LLM agents as before.
        targets: Collection[str] = ()
        if is_module_config(config):
            targets = self.host._node_targets(node)
            config, problem = await with_code_for(
                config, targets, self.host._node_takes_bundles(node)
            )
            if problem:
                await self._refuse_remote_spawn(config, node, f"{problem}.")
                return False
        unrunnable = why_a_node_cannot_run(config, targets)
        if unrunnable:
            await self._refuse_remote_spawn(config, node, f"{unrunnable}.")
            return False
        if await self._node_cannot_take(config, node, restoring):
            return False
        config, problem = await self.pinned_for(config, node)
        if problem:
            await self._refuse_remote_spawn(config, node, f"{problem}.")
            return False
        name = str(config.get("name", "remote-agent"))
        actor_id = id_on_node(
            name,
            self._kept_actor_id(name, node, alongside=bool(config.get("alongside"))),
            self.host._node_takes_actor_ids(node),
        )
        config = {**config, "actor_id": actor_id}
        wire_config = self._inject_llm_bridge_code(config)
        name = wire_config.get("name", "remote-agent")

        packages = wire_config.get("install", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]

        logger.info("[%s] Spawning %r on remote node %r", self.host.name, name, node)
        if packages and not await self._packages_placed(config, node, packages, restoring):
            return False

        # Not retained, and that is the whole point. A retained spawn is handed
        # to the node again on every reconnect, so an agent withdrawn after this
        # was published -- a migration rolled back, an agent deleted -- comes
        # back at the node's next reboot with nothing following it to say
        # otherwise. Withdrawing the message instead is not open to us: the
        # topic is per node rather than per agent, so clearing it would discard
        # whatever spawn is sitting in that slot, which may be someone else's.
        #
        # Reaching a node that was away is the desired state's job, and it is
        # retained. QoS 1 covers the shorter absence: the runner's session
        # outlives a reconnect, so the broker holds this until it returns, and
        # a stop published afterwards is delivered behind it and undoes it. That
        # last part is Mosquitto queueing per client in order and the runner
        # draining one message at a time -- MQTT only promises ordering within a
        # single topic, so it is the deployment that makes it true, not the spec.
        await self.host._mqtt_publish(f"nodes/{node}/spawn", wire_config, retain=False, qos=1)
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
            # Under the node it went to, whatever the config said: the entry is
            # what main checks the node's word about this agent against.
            self._save_to_spawn_registry({**config, "node": node})
        return True
