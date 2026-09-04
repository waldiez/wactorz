"""Commands that act on a whole node.

Removing or shutting down a node stops every agent on it, and deploying one
installs software on a machine over SSH. None of them is undone by running the
opposite afterwards, which is why each says what it is about to affect.
"""

from __future__ import annotations

from .dispatch import CommandContext, command


@command(
    "/deploy",
    exact=("/deploy",),
    prefixes=("/deploy ",),
    summary="deploy a remote Wactorz node",
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
)
async def remove_node(ctx: CommandContext, argument: str) -> str:
    """Stop all agents on a node and remove it."""
    node_name = argument
    # Clear retained MQTT messages
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/spawn", b"", retain=True)
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/desired_state", b"", retain=True)
    await ctx.actor._mqtt_publish(f"nodes/{node_name}/stop_all", {"reason": "removed"}, qos=1)
    # Remove all agents for this node from spawn registry
    reg = ctx.actor._get_spawn_registry()
    removed = [n for n, c in reg.items() if c.get("node", "") == node_name]
    for n in removed:
        ctx.actor._remove_from_spawn_registry(n)
    ctx.actor._known_nodes.pop(node_name, None)
    return (
        f"Node '{node_name}' removed. "
        f"Cleared {len(removed)} agent(s): {', '.join(removed) or 'none'}. "
        f"The node will disappear from /nodes within 30s."
    )


@command(
    "/nodes restart",
    prefixes=("/nodes restart ",),
    summary="restart the runner process on a node",
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
