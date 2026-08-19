"""Commands that act on a single agent.

Each of these changes what is running: restarting one, or moving it to another
machine. The checks that come first are the point — a command that acts on the
wrong agent is not recoverable by typing the right one afterwards.
"""

from __future__ import annotations

import logging

from ...core.actor import ActorState
from .dispatch import CommandContext, command

logger = logging.getLogger(__name__)


@command(
    "/migrate",
    exact=("/migrate",),
    prefixes=("/migrate ",),
    summary="move an agent to a different node",
)
async def migrate_agent_cmd(ctx: CommandContext, argument: str) -> str:
    """Move an agent to a different node."""
    parts = argument.split()
    if len(parts) < 2:
        return (
            "Usage: /migrate <agent-name> <target-node>\n"
            "Examples:\n"
            "  /migrate temp-sensor rpi-bedroom   — remote to remote\n"
            "  /migrate temp-sensor local         — remote back to main node\n"
            "  /migrate temp-sensor rpi-a         — local to remote"
        )
    agent_name, target_node = parts[0], parts[1]
    try:
        result = await ctx.actor.migrate_agent(agent_name, target_node)
    except Exception as exc:
        logger.exception("[main] /migrate failed for %r → %r", agent_name, target_node)
        return f"Migrate failed: {exc}"
    sym = "OK" if result.get("success") else "FAIL"
    return f"[{sym}] {result.get('message', str(result))}"


@command(
    "/agents restart",
    prefixes=("/agents restart ",),
    summary="restart an agent, state preserved",
)
async def restart_agent(ctx: CommandContext, argument: str) -> str:
    """Restart an agent, state preserved."""
    agent_name = argument
    if not agent_name:
        return "Usage: /agents restart <agent-name>"
    reg = ctx.actor._get_spawn_registry()
    node = reg.get(agent_name, {}).get("node", "").strip()
    if node:
        await ctx.actor._mqtt_publish(f"nodes/{node}/restart_agent", {"name": agent_name}, qos=1)
        return (
            f"Restart signal sent to '{agent_name}' on node '{node}'. "
            f"State is preserved — the agent will resume from its last saved state."
        )
    # Local agent — stop and re-spawn using config from spawn registry
    config = reg.get(agent_name)
    if not config:
        return f"Agent '{agent_name}' not found in spawn registry."
    if ctx.actor._registry:
        target = ctx.actor._registry.find_by_name(agent_name)
        if target:
            # Release from supervision and stop *before* unregistering.
            # Stopping a still-supervised actor looks like a crash, and
            # unregistering first leaves it unreachable while it shuts
            # down. Unlike the stop path above, the actor really does go
            # here — a fresh one replaces it below.
            await target.apply_command("stop")
            await ctx.actor._registry.unregister(target.actor_id)
    new_config = dict(config)
    new_config["replace"] = True
    # Straight from the registry a few lines up, so a catalog agent
    # restarts trusted rather than failing the validator it never had
    # to pass.
    await ctx.actor._spawn_from_config(new_config, save=True, from_registry=True)
    return f"Agent '{agent_name}' restarted locally."


async def _apply_lifecycle(ctx: CommandContext, verb: str, agent_name: str) -> str:
    """Run one lifecycle verb against a local agent."""
    if not agent_name:
        return f"Usage: /{verb} <agent-name>"

    # Checked before anything else: the runner on a node implements no
    # pause or resume topic, so there is nothing to send and the agent would
    # be reported as paused while it kept running.
    reg = ctx.actor._get_spawn_registry()
    node = reg.get(agent_name, {}).get("node", "").strip()
    if node:
        return (
            f"'{agent_name}' is running on remote node '{node}'. "
            f"Pause/resume is only supported for local agents. "
            f"Use /stop {agent_name} to stop it instead."
        )

    if not ctx.actor._registry:
        return "No actor registry available."

    target = ctx.actor._registry.find_by_name(agent_name)
    if target is None:
        return f"Agent '{agent_name}' not found locally."

    # Idempotent guards — be explicit so the user knows nothing changed
    if verb == "pause" and target.state == ActorState.PAUSED:
        return f"Agent '{agent_name}' is already paused."
    if verb == "resume" and target.state != ActorState.PAUSED:
        return f"Agent '{agent_name}' is not paused (state: {target.state.name})."
    if verb == "start" and target.state != ActorState.STOPPED:
        return f"Agent '{agent_name}' is not stopped (state: {target.state.name})."

    try:
        # apply_command rather than target.pause()/resume(): it is the
        # one place the lifecycle rules live, including refusing to
        # pause a protected agent — which this path did not check —
        # and putting a started agent back under supervision.
        applied = await target.apply_command(verb)
    except Exception as exc:
        logger.exception("[main] /%s failed for %r", verb, agent_name)
        return f"Failed to {verb} '{agent_name}': {exc}"

    if not applied:
        return f"'{agent_name}' refused {verb} (it may be protected)."
    _past = {"start": "started", "pause": "paused", "resume": "resumed"}[verb]
    return f"Agent '{agent_name}' {_past}."


async def _act_on_agent(ctx: CommandContext, verb: str, agent_name: str) -> str:
    """Stop, pause or permanently delete an agent, local or remote."""
    if not agent_name:
        return f"Usage: /agents {verb} <agent-name>"
    is_delete = verb in ("delete", "remove")

    if is_delete:
        # Delegate to the canonical permanent-delete helper so the
        # chat path and the LLM <delete> path behave identically.
        try:
            await ctx.actor.delete_spawned_agent(agent_name)
        except Exception as e:
            logger.exception("[%s] delete_spawned_agent(%r) failed", ctx.actor.name, agent_name)
            return f"Delete of '{agent_name}' failed: {e}"
        return (
            f"Agent '{agent_name}' permanently deleted "
            f"(state file removed, retained MQTT topics cleared)."
        )

    # stop / pause — reversible. Keep state and spawn-registry entry.
    reg = ctx.actor._get_spawn_registry()
    node = reg.get(agent_name, {}).get("node", "").strip()
    # Past-tense rendering: stop → stopped, pause → paused.
    # Both are double-the-consonant + ed (English regular doubling
    # for monosyllabic CVC verbs); just hard-code the table.
    past = {"stop": "stopped", "pause": "paused"}.get(verb, f"{verb}ped")

    if node:
        # Remote agent — plain stop (no delete flag), keep registry
        # so /agents restart can resume it cleanly later.
        await ctx.actor._mqtt_publish(f"nodes/{node}/stop", {"name": agent_name}, qos=1)
        ctx.actor._record_agent_deletion(
            agent_name, reason=f"manually {past} via /agents on node '{node}'"
        )
        return (
            f"Stop signal sent to '{agent_name}' on node '{node}'. "
            f"State is preserved — use /agents restart {agent_name} to resume."
        )
    # Local agent
    if ctx.actor._registry:
        target = ctx.actor._registry.find_by_name(agent_name)
        if target:
            # apply_command, not stop(): this ran stop() for *both*
            # verbs, so `/agents pause` stopped the agent and then
            # reported it paused. It also skipped the supervision
            # release, which makes a deliberate stop look like a
            # crash to the watchdog, and unregistered before
            # stopping, leaving the actor unreachable while it was
            # still shutting down.
            if not await target.apply_command(verb):
                return f"'{agent_name}' refused {verb} (it may be protected)."
            ctx.actor._record_agent_deletion(agent_name, reason=f"manually {past} via /agents")
            return (
                f"Agent '{agent_name}' {past}. "
                f"State is preserved — use /agents restart {agent_name} to resume."
            )
    return f"Agent '{agent_name}' not found locally."


@command("/start", prefixes=("/start ",), bare_ok=True, summary="start a stopped agent back up")
async def start_agent(ctx: CommandContext, argument: str) -> str:
    """Start a stopped agent back up."""
    return await _apply_lifecycle(ctx, "start", argument)


@command(
    "/pause",
    prefixes=("/pause ",),
    bare_ok=True,
    summary="pause a local agent (remote not supported)",
)
async def pause_agent(ctx: CommandContext, argument: str) -> str:
    """Pause a local agent."""
    return await _apply_lifecycle(ctx, "pause", argument)


@command("/resume", prefixes=("/resume ",), bare_ok=True, summary="resume a paused local agent")
async def resume_agent(ctx: CommandContext, argument: str) -> str:
    """Resume a paused local agent."""
    return await _apply_lifecycle(ctx, "resume", argument)


@command("/agents stop", prefixes=("/agents stop ",), summary="stop an agent, keeping its state")
async def stop_agent(ctx: CommandContext, argument: str) -> str:
    """Stop an agent, keeping its state."""
    return await _act_on_agent(ctx, "stop", argument)


@command("/agents pause", prefixes=("/agents pause ",), summary="pause an agent")
async def agents_pause(ctx: CommandContext, argument: str) -> str:
    """Pause an agent."""
    return await _act_on_agent(ctx, "pause", argument)


@command("/agents delete", prefixes=("/agents delete ",), summary="delete an agent permanently")
async def delete_agent(ctx: CommandContext, argument: str) -> str:
    """Delete an agent and every trace of it."""
    return await _act_on_agent(ctx, "delete", argument)


@command("/agents remove", prefixes=("/agents remove ",), summary="delete an agent permanently")
async def remove_agent(ctx: CommandContext, argument: str) -> str:
    """Delete an agent and every trace of it. Another spelling of delete."""
    return await _act_on_agent(ctx, "remove", argument)


@command(
    "/agents",
    exact=("/agents", "/capabilities"),
    prefixes=("/agents ", "/capabilities "),
    summary="list all known agents, or filter by capability keyword",
)
async def list_agents(ctx: CommandContext, argument: str) -> str:
    """List every known agent, optionally filtered by a capability keyword.

    Registered after the `/agents <verb>` handlers on purpose: this matches any
    `/agents …`, so ahead of them it would read `stop` as a search term and
    report that no agent matches it, having stopped nothing.
    """
    keyword = argument
    caps = ctx.actor.list_capabilities(keyword)
    if not caps:
        msg = "No agents found" + (f" matching {keyword!r}" if keyword else "") + "."
        msg += " Agents publish their capabilities on startup."
        return msg
    lines = ["Agent capabilities" + (" matching " + repr(keyword) if keyword else "") + ":"]
    for a in caps:
        running = (
            "\U0001f7e2" if a["running"] else ("\U0001f4e6" if a["spawnable"] else "\U0001f534")
        )
        node_str = f" on {a['node']}" if a.get("node") else ""
        lines.append("")
        lines.append(f"  {running} [{a['name']}]{node_str}")
        lines.append(f"    description : {a['description']}")
        if a["capabilities"]:
            lines.append(f"    capabilities: {', '.join(a['capabilities'])}")
        if a["input_schema"]:
            lines.append(f"    input       : {a['input_schema']}")
        if a["output_schema"]:
            lines.append(f"    output      : {a['output_schema']}")
        if a["spawnable"]:
            lines.append(f"    spawnable   : yes — @catalog spawn {a['name']}")
    lines.append(
        "\nLegend: \U0001f7e2 running  \U0001f4e6 spawnable (not yet running)  \U0001f534 stopped"
    )
    lines.append("Filter: /agents <keyword>   e.g. /agents discord")
    return "\n".join(lines)
