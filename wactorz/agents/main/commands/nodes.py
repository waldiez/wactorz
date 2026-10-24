"""Commands that act on a whole node.

Removing or shutting down a node stops every agent on it, and deploying one
installs software on a machine over SSH. None of them is undone by running the
opposite afterwards, which is why each says what it is about to affect.
"""

from ....core.node_operations import COMMAND_TIMEOUT_S
from .dispatch import CommandContext, command
from .groups import NODES


@command(
    "/deploy",
    exact=("/deploy",),
    prefixes=("/deploy ",),
    summary="deploy a remote Wactorz node",
    group=NODES,
    forms=(
        (
            "/deploy <node>",
            "deploy a node from DEPLOY_TARGETS; bare, list them (SSH credentials come from the environment, not chat)",
        ),
    ),
)
async def deploy_node(ctx: CommandContext, argument: str) -> str:
    """Deploy a remote Wactorz node."""
    chunks: list[str] = []
    async for chunk in ctx.actor._slash_deploy_stream(f"/deploy {argument}".strip()):
        if isinstance(chunk, str):
            chunks.append(chunk)
    return "\n".join(chunks)


@command(
    "/nodes remove",
    prefixes=("/nodes remove ",),
    summary="stop all agents on a node and remove it",
    group=NODES,
    forms=(("/nodes remove <node>", "stop all agents on a node and remove it"),),
)
async def remove_node(ctx: CommandContext, argument: str) -> str:
    """Stop all agents on a node and remove it."""
    node_name = argument
    # Clear the retained desired state, which is what would otherwise bring the
    # node's agents back when it next reconciles. The spawn message is not
    # retained, so there is nothing of it left to clear.
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/desired_state", b"", retain=True)
    # And the retained manifest, which would otherwise describe the machine to
    # every main that connects, for a node no longer there.
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/manifest", b"", retain=True)
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/stop_all", {"reason": "removed"}, qos=1)
    # Remove all agents for this node from spawn registry
    reg = ctx.actor._get_spawn_registry()
    removed = [n for n, c in reg.items() if c.get("node", "") == node_name]
    for n in removed:
        ctx.actor._remove_from_spawn_registry(n)
    ctx.actor.nodes.forget(node_name)
    return (
        f"Node '{node_name}' removed. "
        f"Cleared {len(removed)} agent(s): {', '.join(removed) or 'none'}. "
        f"The node will disappear from /nodes within 30s."
    )


@command(
    "/nodes run",
    prefixes=("/nodes run ",),
    summary="run a shell command on a node over SSH, as typed",
    group=NODES,
    forms=(("/nodes run <node> <command>", "run a shell command on a node over SSH, as typed"),),
)
async def run_on_node(ctx: CommandContext, argument: str) -> str:
    """Run a shell command on a node, over SSH with its deploy target's credentials.

    The one way free text reaches a node's shell, and it is the operator's: a
    slash command is read from what was typed, never from a reply the model
    wrote, and the social channels refuse them. The model asks for a named
    operation instead (`wactorz.core.node_operations`).
    """
    parts = argument.split(maxsplit=1)
    node_name = parts[0] if parts else ""
    command = parts[1].strip() if len(parts) > 1 else ""
    if not node_name or not command:
        return "Usage: /nodes run <node-name> <command>"
    result = await ctx.actor.delegate_to_installer(
        {"action": "node_run", "node_name": node_name, "command": command},
        timeout=COMMAND_TIMEOUT_S + 30,
    )
    if result.get("error") and not result.get("output"):
        return f"Could not run it on '{node_name}': {result['error']}"
    status = "exit 0" if result.get("success") else f"exit {result.get('exit_code', '?')}"
    output = str(result.get("output") or "").rstrip() or "(no output)"
    return f"{node_name} $ {command}  [{status}]\n{output}"


@command(
    "/nodes restart",
    prefixes=("/nodes restart ",),
    summary="restart the runner process on a node",
    group=NODES,
    forms=(("/nodes restart <node>", "restart the runner process on a node"),),
)
async def restart_node(ctx: CommandContext, argument: str) -> str:
    """Restart the runner process on a node."""
    node_name = argument
    if not node_name:
        return "Usage: /nodes restart <node-name>"
    info = ctx.actor._known_nodes.get(node_name)
    if not info:
        return f"Node '{node_name}' is not known. Check /nodes for online nodes."
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/restart", {"reason": "user request"}, qos=1)
    return (
        f"Restart signal sent to node '{node_name}'. "
        f"It will briefly drop offline then reappear within ~15s."
    )


@command(
    "/nodes shutdown",
    prefixes=("/nodes shutdown ",),
    summary="stop all agents and shut down a node",
    group=NODES,
    forms=(("/nodes shutdown <node>", "stop all agents and shut down a node"),),
)
async def shutdown_node(ctx: CommandContext, argument: str) -> str:
    """Stop all agents and shut down a node."""
    node_name = argument
    if not node_name:
        return "Usage: /nodes shutdown <node-name>"
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/stop_all", {"reason": "user request"}, qos=1)
    return (
        f"Shutdown signal sent to node '{node_name}'. "
        f"All agents will stop. The node will disappear from /nodes within 30s. "
        f"(It stays down until redeployed: the unit restarts on failure, and a "
        f"shutdown is not one.)"
    )
