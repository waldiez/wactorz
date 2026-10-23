"""The machines a plan's agents can run on, as the planner's model is shown them.

A step's spawn may name a node, and placement keeps the one it names. The
model can name one only if it is told which there are, what each machine is,
and which of them would take an agent now, so both of the planner's prompts
carry this section. A node main would refuse a spawn to is still listed,
saying so, so the model can say why instead of naming another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import CONFIG

if TYPE_CHECKING:
    from ..main import MainActor

#: Said when there is nothing to list: every agent then runs on main.
NO_NODES = "(no nodes: every agent runs on main)"


def nodes_section(main: MainActor | None) -> str:
    """One line per node main knows of: what the machine is, and whether it takes agents now."""
    if main is None:
        return NO_NODES
    model_code_node = main.placement.model_code_node()
    listed = {str(info.get("node")): info for info in main.nodes.list_nodes()}
    names = sorted(set(listed) | {target.name for target in CONFIG.deploy_targets})
    lines = [
        node_line(
            name,
            listed.get(name),
            refusal=main.nodes.refuses_tasks(name),
            takes_model_code=name == model_code_node,
        )
        for name in names
    ]
    return "\n".join(lines) if lines else NO_NODES


def node_line(
    name: str, info: dict[str, Any] | None, *, refusal: str | None, takes_model_code: bool
) -> str:
    """How one node reads in the prompt."""
    if info is None:
        return f"- {name}: configured, never heard from — cannot take agents until deployed"
    parts = [machine_of(info.get("manifest") or {})]
    agents = [str(agent) for agent in info.get("agents") or []]
    if agents:
        parts.append("runs " + ", ".join(agents))
    if takes_model_code:
        parts.append("where a dynamic agent naming no node goes")
    if refusal:
        parts.append(f"CANNOT TAKE AGENTS NOW: {refusal}")
    elif not info.get("online"):
        parts.append("offline — cannot take agents now")
    said = "; ".join(part for part in parts if part)
    return f"- {name}: {said or 'has not said what machine it is'}"


def machine_of(manifest: dict[str, Any]) -> str:
    """What a node's machine is, from its manifest, or "" when it has not said."""
    described = [
        str(manifest.get("model") or ""),
        str(manifest.get("arch") or ""),
        f"{manifest['cpu_count']} CPUs" if manifest.get("cpu_count") else "",
        f"{manifest['ram_total_mb']} MB RAM" if manifest.get("ram_total_mb") else "",
        "in a container" if manifest.get("container") else "",
    ]
    devices = [str(kind) for kind in manifest.get("devices") or []]
    if devices:
        described.append("devices: " + " ".join(devices))
    return ", ".join(part for part in described if part)
