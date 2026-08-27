"""Commands that only read and report.

Nothing here changes anything, which is why they are the first to move: a
handler that cannot do damage is one whose move can be judged on whether the
text still matches.
"""

from __future__ import annotations

import time
from typing import Any

from .dispatch import CommandContext, command

#: Every command, as the help text lists them. Kept as data rather than one
#: long string so a command that is added without a line here reads as an
#: omission rather than as prose someone forgot to edit.
HELP_LINES: tuple[str, ...] = (
    "**Wactorz commands**",
    "",
    "**Agents**",
    "  /agents                 — list all known agents with descriptions and schemas",
    "  /agents <keyword>       — filter agents by capability keyword",
    "  /capabilities           — alias for /agents",
    "  /delete <agent>         — stop an agent and remove it from the spawn registry",
    "  /stop <agent>           — stop an agent, keeping its state (reversible)",
    "  /start <agent>          — start a stopped agent back up",
    "  @agent-name <msg>       — send a message directly to a named agent",
    "  @catalog list           — list available catalog recipes",
    "  @catalog spawn <n>      — spawn a catalog agent",
    "",
    "**Nodes**",
    "  /nodes                  — list local + remote nodes and their agents",
    "  /nodes restart <node>   — restart the runner process on a node",
    "  /nodes shutdown <node>  — stop all agents and shut down a node",
    "  /nodes remove <node>    — stop all agents on a node and remove it",
    "  /deploy <node>          — deploy a remote Wactorz node",
    "                            (a target configured via DEPLOY_TARGETS;",
    "                             run bare to list them. SSH credentials",
    "                             come from the environment, not chat)",
    "  /migrate <agent> <node> — move an agent to a different node (state preserved)",
    "  /agents restart <name>  — restart an agent (local or remote, state preserved)",
    "",
    "**Pipelines & Plans**",
    "  /plans                  — list pending pipeline proposals (dry-run)",
    "  /plans show <id>        — inspect a proposal's full code",
    "  /plans approve <id>     — execute a proposed pipeline",
    "  /plans reject <id>      — discard a proposed pipeline",
    "  /clear-plans            — clear the plan cache",
    "  /rules                  — list active pipeline rules",
    "  /rules delete <id>      — stop agents and remove a rule",
    "  pipeline! <task>        — bypass approval and execute immediately (power users)",
    "",
    "**Memory**",
    "  /memory                 — show stored user facts and conversation summary",
    "  /memory clear           — wipe all memory",
    "  /memory forget <key>    — remove one stored fact",
    "",
    "**Notifications**",
    "  /webhook                — list stored webhook URLs",
    "  /webhook discord <url>  — store a Discord webhook URL",
    "  /webhook telegram <url> — store a Telegram webhook URL",
    "",
    "**System & diagnostics**",
    "  /topics                 — list MQTT topics published by known agents",
    "  /topics <keyword>       — filter topics by keyword",
    "  /bus                    — TopicBus registry: contracts, data flows, wiring pairs",
    "  /mqtt                   — MQTT publisher status (connected, queue depth, outbox)",
    "  /registry               — diagnostic: compare live registry, spawn registry, manifest cache",
    "  /help                   — show this help",
)


@command(
    "/help",
    exact=("/help", "help", "/?"),
    summary="show this help",
    restricted_ok=False,
)
async def show_help(_ctx: CommandContext, _argument: str) -> str:
    """List every command, grouped by what it acts on."""
    return "\n".join(HELP_LINES)


@command(
    "/nodes",
    exact=("main.list_nodes", "list_nodes", "/nodes"),
    summary="list local + remote nodes and their agents",
)
async def show_nodes(ctx: CommandContext, _argument: str) -> str:
    """List local + remote nodes and their agents."""
    nodes = ctx.actor.list_nodes()
    # Local row first, then remote — one format whatever the source
    local_agents = []
    if ctx.actor._registry:
        local_agents = sorted(a.name for a in ctx.actor._registry.all_actors())
    local_str = ", ".join("@" + n for n in local_agents) or "(none)"
    lines = [f"  {'local':22s} 🟢 online  |  agents: {local_str}"]

    # Remote rows
    for nd in sorted(nodes, key=lambda x: x["node"]):
        status = "🟢 online " if nd["online"] else "🔴 offline"
        agents = ", ".join("@" + a for a in nd["agents"]) or "(no agents)"
        age = int(time.time() - nd["last_seen"])
        lines.append(
            f"  {nd['node']:22s} {status}  |  agents: {agents}  |  last heartbeat: {age}s ago"
        )

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
    "/mqtt",
    exact=("/mqtt",),
    summary="MQTT publisher status",
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
