"""Commands that only read and report.

Nothing here changes anything, which is why they are the first to move: a
handler that cannot do damage is one whose move can be judged on whether the
text still matches.
"""

import asyncio
import time
from collections.abc import Iterable
from typing import Any

from ...._version import __version__
from ....core.artifacts import ArtifactError, ArtifactStore, human_size, summary
from ....core.compatibility import compatible
from ....core.paths import resolve_state_dir
from .dispatch import Command, CommandContext, Form, command, registry
from .groups import AGENTS, MODELS, NODES, ORDER, PIPELINES, SYSTEM

#: What `/help` lists that is not a command: the ways to address an agent, and
#: the prefix that skips a plan's approval.
HELP_NOTES: dict[str, tuple[Form, ...]] = {
    AGENTS: (
        Form("@agent-name <msg>", "send a message directly to a named agent"),
        Form("@catalog list", "list available catalog recipes"),
        Form("@catalog spawn <name>", "spawn a catalog agent"),
    ),
    PIPELINES: (Form("pipeline! <task>", "bypass approval and execute immediately (power users)"),),
}


def help_line(form: Form) -> str:
    """One line of `/help`: the usage, padded so most summaries line up."""
    return f"  {form.usage:<23} — {form.summary}"


def help_sections(commands: Iterable[Command]) -> list[tuple[str, list[Form]]]:
    """What `/help` lists: each heading, in `groups.ORDER`, with its forms.

    Within a heading, alphabetical by what is typed, so a reader scanning for a
    command finds it where they expect; the notes that are not commands follow.
    A heading with nothing under it is left out. The docs' command table is
    written from this too, so the two cannot disagree.
    """
    grouped: dict[str, list[Form]] = {group: [] for group in ORDER}
    for described in commands:
        grouped.setdefault(described.group or SYSTEM, []).extend(described.forms)
    sections: list[tuple[str, list[Form]]] = []
    for group, forms in grouped.items():
        listed = sorted(forms, key=lambda form: form.usage) + list(HELP_NOTES.get(group, ()))
        if listed:
            sections.append((group, listed))
    return sections


def help_lines(commands: Iterable[Command]) -> list[str]:
    """`/help`, from the commands' own forms, as `help_sections` orders them."""
    lines = ["**Wactorz commands**"]
    for group, forms in help_sections(commands):
        lines += ["", f"**{group}**", *(help_line(form) for form in forms)]
    return lines


@command(
    "/help",
    exact=("/help", "help", "/?"),
    summary="show this help",
    restricted_ok=False,
    group=SYSTEM,
    forms=(("/help", "show this help"),),
)
async def show_help(_ctx: CommandContext, _argument: str) -> str:
    """List every command, grouped by what it acts on."""
    return "\n".join(help_lines(registry))


def _node_version_label(node: dict[str, Any]) -> str:
    """What a node row says about its version: the number, and whether it matches."""
    reported = node.get("version")
    if not reported:
        return "v? (older runtime)"
    if compatible(__version__, str(reported)):
        return f"v{reported}"
    return f"v{reported} ≠ server, redeploy"


def _node_resources_label(node: dict[str, Any]) -> str:
    """What a node row says about how close it is to running out.

    Only what the node reported: a reading it could not take is left out rather
    than shown as zero. Throttling comes last and loud, since it is what takes
    a board down.
    """
    parts = []
    if isinstance(node.get("cpu_pct"), (int, float)):
        parts.append(f"cpu {node['cpu_pct']:.0f}%")
    if isinstance(node.get("load_1m"), (int, float)):
        parts.append(f"load {node['load_1m']:.2f}")
    if isinstance(node.get("mem_free_mb"), (int, float)):
        parts.append(f"{_megabytes(node['mem_free_mb'])} memory free")
    if isinstance(node.get("disk_free_mb"), (int, float)):
        parts.append(f"{_megabytes(node['disk_free_mb'])} disk free")
    if isinstance(node.get("temp_c"), (int, float)):
        parts.append(f"{node['temp_c']:.0f}°C")
    if node.get("throttled"):
        parts.append("⚠ " + ", ".join(str(f).replace("_", " ") for f in node["throttled"]))
    return " · ".join(parts)


def _node_machine_label(manifest: dict[str, Any] | None) -> str:
    """What a node row says about the machine itself, from its manifest; "" without one."""
    if not isinstance(manifest, dict):
        return ""
    parts = [
        str(manifest[key])
        for key in ("model", "arch", "os_release")
        if isinstance(manifest.get(key), str) and manifest[key]
    ]
    if isinstance(manifest.get("python"), str):
        parts.append(f"Python {manifest['python']}")
    if _is_number(manifest.get("cpu_count")):
        parts.append(f"{manifest['cpu_count']} CPUs")
    if _is_number(manifest.get("ram_total_mb")):
        parts.append(f"{_megabytes(manifest['ram_total_mb'])} memory")
    if manifest.get("container") is True:
        parts.append("in a container")
    gpus = manifest.get("gpu")
    if isinstance(gpus, list):
        parts.extend(str(g.get("name")) for g in gpus if isinstance(g, dict) and g.get("name"))
    devices = manifest.get("devices")
    if isinstance(devices, list) and devices:
        parts.append(", ".join(str(d) for d in devices))
    return " · ".join(parts)


def _is_number(value: object) -> bool:
    """Whether a manifest field is a number; a JSON ``true`` is not one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _megabytes(value: float) -> str:
    """A size given in MiB, in the unit a person reads it in."""
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


@command(
    "/nodes",
    exact=("main.list_nodes", "list_nodes", "/nodes"),
    summary="list local + remote nodes and their agents",
    group=NODES,
    forms=(("/nodes", "list local + remote nodes and their agents"),),
)
async def show_nodes(ctx: CommandContext, _argument: str) -> str:
    """List local + remote nodes and their agents."""
    nodes = ctx.actor.list_nodes()
    # Local row first, then remote — one format whatever the source
    local_agents = []
    if ctx.actor._registry:
        local_agents = sorted(a.name for a in ctx.actor._registry.all_actors())
    local_str = ", ".join("@" + n for n in local_agents) or "(none)"
    lines = [f"  {'local':22s} 🟢 online  |  v{__version__}  |  agents: {local_str}"]

    # Remote rows. The version sits beside the status because it decides
    # whether the node can take an agent at all: one that differs from the
    # server's is refused, and this is where a person looks to see why.
    for nd in sorted(nodes, key=lambda x: x["node"]):
        status = "🟢 online " if nd["online"] else "🔴 offline"
        agents = ", ".join("@" + a for a in nd["agents"]) or "(no agents)"
        age = int(time.time() - nd["last_seen"])
        version = _node_version_label(nd)
        # Only an online node's readings describe the machine as it is now.
        resources = _node_resources_label(nd) if nd["online"] else ""
        lines.append(
            f"  {nd['node']:22s} {status}  |  {version}  |  agents: {agents}"
            + (f"  |  {resources}" if resources else "")
            + f"  |  last heartbeat: {age}s ago"
        )
        # Beneath, what the machine is: true whether or not it is online.
        machine = _node_machine_label(nd.get("manifest"))
        if machine:
            lines.append(f"  {'':22s} {machine}")

    footer = ""
    if not nodes:
        footer = "\n(no remote nodes seen yet — deploy one with /deploy <node-name>)"
    else:
        footer = "\nTo remove a remote node: /nodes remove <node-name>"

    return "Nodes:\n" + "\n".join(lines) + footer


@command(
    "/topics",
    exact=("/topics",),
    prefixes=("/topics",),
    summary="list MQTT topics published by known agents",
    group=SYSTEM,
    forms=(
        ("/topics", "list MQTT topics published by known agents"),
        ("/topics <keyword>", "filter topics by keyword"),
    ),
)
async def show_topics(ctx: CommandContext, argument: str) -> str:
    """List MQTT topics published by known agents."""
    # The argument arrives stripped of surrounding whitespace; the brackets
    # are what a model writes when it treats the command as a call.
    keyword = argument.lstrip("(").rstrip(")")
    topics = ctx.actor.list_topics(keyword)
    if not topics:
        msg = "No topics found" + (f" matching '{keyword}'" if keyword else "") + "."
        msg += " Topics are registered automatically when agents publish for the first time."
        return msg
    lines = [f"Known MQTT topics{' matching ' + repr(keyword) if keyword else ''}:"]
    for t in topics:
        agent_strs = ", ".join(
            f"{a['name']}" + (f" ({a['node']})" if a.get("node") else "") for a in t["agents"]
        )
        lines.append(f"  {t['topic']:40s} ← {agent_strs}")
    return "\n".join(lines)


@command(
    "/artifacts",
    exact=("/artifacts",),
    prefixes=("/artifacts ",),
    summary="list the models kept on main",
    group=MODELS,
    forms=(
        ("/artifacts", "list the models kept on main (wactorz-artifacts)"),
        ("/artifacts <name>", "every version of one model"),
    ),
)
async def show_artifacts(_ctx: CommandContext, argument: str) -> str:
    """The models kept on main, or every version of one."""
    return await stored_models_text(argument)


async def stored_models_text(name: str = "") -> str:
    """The models kept on main, or every version of ``name``, as `/artifacts` shows them."""
    store = ArtifactStore.in_state_dir(resolve_state_dir())
    try:
        if name:
            return await asyncio.to_thread(_versions_of, store, name)
        lines = await asyncio.to_thread(summary, store)
    except ArtifactError as exc:
        return f"Cannot list models: {exc}"
    if not lines:
        return "No models are kept here. Add one with `wactorz-artifacts add <file> <name>`."
    return "Models kept on main:\n" + "\n".join(f"  {line}" for line in lines)


def _versions_of(store: ArtifactStore, name: str) -> str:
    versions = store.versions(name)
    if not versions:
        return f"No model named '{name}'. /artifacts lists them."
    lines = [f"Versions of '{name}':"]
    for artifact in versions:
        code = ", runs code when it loads" if artifact.runs_code else ""
        lines.append(
            f"  {artifact.reference:28s} {artifact.format:12s} {human_size(artifact.size):>10s}{code}"
        )
    return "\n".join(lines)


#: Wactorz's own programs. Each runs in a terminal on the machine it acts on,
#: and nothing in chat can run one. Typed into chat, one is answered by
#: `terminal_command_reply` before any model sees it: a model asked to run a
#: command it has no way to run tends to say that it did.
TERMINAL_COMMANDS = ("wactorz-artifacts", "wactorz-state", "wactorz-reset", "wactorz-node")


def terminal_command(text: str) -> str | None:
    """The Wactorz program ``text`` runs, if it is a terminal command; else None."""
    words = text.split(maxsplit=1)
    return words[0] if words and words[0] in TERMINAL_COMMANDS else None


async def terminal_command_reply(text: str) -> str | None:
    """What main says to a terminal command typed into chat, or None for anything else."""
    program = terminal_command(text)
    if program is None:
        return None
    reply = (
        f"`{program}` is a command for a terminal on the machine Wactorz runs on, not for chat, "
        "so nothing was run. Open a terminal there, in the environment Wactorz is installed "
        f"in, and run it as typed:\n\n    {text.strip()}"
    )
    if program == "wactorz-artifacts":
        reply += "\n\nWhat is kept now:\n" + await stored_models_text()
    return reply


@command(
    "/mqtt",
    exact=("/mqtt",),
    summary="MQTT publisher status",
    group=SYSTEM,
    forms=(("/mqtt", "MQTT publisher status (connected, queue depth, outbox)"),),
)
async def show_mqtt_status(ctx: CommandContext, _argument: str) -> str:
    """MQTT publisher status."""
    client = ctx.actor._mqtt_client
    if client is None:
        return "MQTT publisher not initialised."
    connected = getattr(client, "connected", False)
    queue_depth = getattr(client, "queue_depth", 0)
    client_id = getattr(client, "_client_id", "?")
    db_path = getattr(client, "_db_path", "?")
    status_icon = "🟢" if connected else "🔴"
    lines = [
        "MQTT Publisher Status:",
        f"  {status_icon} connected   : {connected}",
        f"  client_id   : {client_id}",
        f"  queue_depth : {queue_depth} message(s) pending",
        f"  outbox_db   : {db_path}",
        "  QoS 1 topics: nodes/*, agents/by-name/*",
        "  QoS 0 topics: */logs, */metrics, */status, */heartbeat",
    ]
    if queue_depth > 0:
        lines.append(f"  ⚠️  {queue_depth} message(s) queued — will deliver when reconnected")
    return "\n".join(lines)


@command(
    "/bus",
    exact=("/bus",),
    summary="TopicBus registry: contracts, data flows, wiring pairs",
    group=SYSTEM,
    forms=(("/bus", "TopicBus registry: contracts, data flows, wiring pairs"),),
)
async def show_bus(ctx: CommandContext, _argument: str) -> str:
    """TopicBus registry: contracts, data flows, wiring pairs."""
    try:
        from ....core.topic_bus import get_topic_bus

        bus = get_topic_bus()
        if not bus:
            return "TopicBus not initialised."
        summary = bus.registry.summary()
        lines = [
            "TopicBus — Reactive Pub/Sub Registry",
            f"  agents with contracts : {summary['total_agents']}",
            f"  published topics      : {summary['total_published']}",
            f"  subscribed topics     : {summary['total_subscribed']}",
            f"  auto-wiring pairs     : {summary['wiring_pairs']}",
            "",
        ]
        for c in sorted(summary["agents"], key=lambda x: x["name"]):
            lines.append(f"  [{c['name']}]" + (f" on {c['node']}" if c.get("node") else ""))
            if c["publishes"]:
                lines.append(f"    publishes : {', '.join(c['publishes'])}")
            if c["subscribes"]:
                lines.append(f"    subscribes: {', '.join(c['subscribes'])}")
            if c.get("triggers_when"):
                lines.append(f"    triggers  : {c['triggers_when']}")
        pairs = bus.registry.find_wiring_opportunities()
        if pairs:
            lines.append("\nAuto-wiring opportunities:")
            for prod, cons, topic in pairs:
                lines.append(f"  {prod.name} → {cons.name}  via {topic}")
        return "\n".join(lines)
    except Exception as e:
        return f"TopicBus error: {e}"


@command(
    "/registry",
    exact=("/registry",),
    summary="diagnostic: compare live registry, spawn registry, manifest cache",
    group=SYSTEM,
    forms=(("/registry", "diagnostic: compare live registry, spawn registry, manifest cache"),),
)
async def show_registry(ctx: CommandContext, _argument: str) -> str:
    """Diagnostic: compare live registry, spawn registry, manifest cache."""
    # 1. Live in-memory registry — what's actually running in this process
    live_names = (
        {a.name for a in ctx.actor._registry.all_actors()} if ctx.actor._registry else set()
    )
    # Skip housekeeping actors so the comparison focuses on user agents
    housekeeping = {
        "main",
        "monitor",
        "installer",
        "home-assistant-agent",
        "anomaly-detector",
        "code-agent",
    }
    live_user = live_names - housekeeping

    # 2. Spawn registry — what main intends to have running (persisted)
    spawn_reg = ctx.actor._get_spawn_registry()
    spawn_names = set(spawn_reg.keys())

    # 3. Manifest cache — every agent that has ever announced itself,
    #    including remote ones on other nodes
    manifest_names = set(ctx.actor._agent_manifests.keys()) - housekeeping

    # 4. Node heartbeats — what each remote node says it's running
    heartbeat_names: set[str] = set()
    for nd_info in ctx.actor._known_nodes.values():
        heartbeat_names.update(nd_info.get("agents", []))

    return "\n".join(
        [
            "**Agent registry diagnostic**",
            "",
            *_registry_sections(ctx, live_user, spawn_reg, spawn_names, manifest_names),
            *_registry_discrepancies(
                ctx, live_names, live_user, spawn_reg, spawn_names, manifest_names, heartbeat_names
            ),
        ]
    )


def _registry_sections(
    ctx: CommandContext,
    live_user: set[str],
    spawn_reg: dict[str, Any],
    spawn_names: set[str],
    manifest_names: set[str],
) -> list[str]:
    """What each of the three sources currently holds."""
    lines: list[str] = []

    # ── Live registry ──
    lines.append("\U0001f7e2 **Live registry** (running NOW in this process):")
    if live_user and ctx.actor._registry:
        for name in sorted(live_user):
            actor = ctx.actor._registry.find_by_name(name)
            state = actor.state.name if actor else "?"
            lines.append(f"    {name}  ({state})")
    else:
        lines.append("    (none)")

    # ── Spawn registry ──
    lines.append("")
    lines.append("\U0001f4be **Spawn registry** (auto-restore on restart, persisted to disk):")
    if spawn_names:
        for name in sorted(spawn_names):
            cfg = spawn_reg.get(name, {})
            node = cfg.get("node", "").strip() or "local"
            lines.append(f"    {name}  on {node}")
    else:
        lines.append("    (none)")

    # ── Manifest cache ──
    lines.append("")
    lines.append("\U0001f4e6 **Manifest cache** (announced via MQTT — includes remote agents):")
    if manifest_names:
        for name in sorted(manifest_names):
            m = ctx.actor._agent_manifests.get(name, {})
            node = m.get("node") or "local"
            lines.append(f"    {name}  on {node}")
    else:
        lines.append("    (none)")
    return lines


def _registry_discrepancies(
    ctx: CommandContext,
    live_names: set[str],
    live_user: set[str],
    spawn_reg: dict[str, Any],
    spawn_names: set[str],
    manifest_names: set[str],
    heartbeat_names: set[str],
) -> list[str]:
    """Where the three sources disagree — the point of the report."""
    lines: list[str] = []
    issues = []
    # Live but not in spawn registry → an ad-hoc spawn that won't survive restart
    for name in sorted(live_user - spawn_names):
        issues.append(
            f"\u26a0\ufe0f  '{name}' is RUNNING but NOT in spawn registry — won't auto-restore on restart"
        )
    # Spawn registry says local but not live → main thinks it should be running
    for name in sorted(spawn_names - live_names):
        cfg = spawn_reg.get(name, {})
        if not cfg.get("node", "").strip():  # local-only check
            issues.append(
                f"\u26a0\ufe0f  '{name}' is in spawn registry but NOT running locally — start failed or was stopped without cleanup"
            )
    # In manifest but not live and not in spawn registry → ghost
    ghosts = manifest_names - live_user - spawn_names - heartbeat_names
    for name in sorted(ghosts):
        issues.append(
            f"\U0001f47b '{name}' is in manifest cache but nowhere else — stale entry, run `/agents delete {name}` to clean up"
        )
    # In spawn registry as remote, but the node is offline / not heartbeating
    online_nodes = set(ctx.actor.nodes.online_names())
    for name, cfg in spawn_reg.items():
        node = cfg.get("node", "").strip()
        if node and node not in online_nodes:
            issues.append(
                f"\u26a0\ufe0f  '{name}' assigned to node '{node}' which is OFFLINE — agent unreachable"
            )

    lines.append("")
    if issues:
        lines.append("**Discrepancies found:**")
        for s in issues:
            lines.append(f"  {s}")
    else:
        lines.append("\u2705 All three sources agree — registry is consistent.")

    return lines
