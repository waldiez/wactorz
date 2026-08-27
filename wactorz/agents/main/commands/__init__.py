"""The commands a user can type.

The machinery lives in `dispatch`; a handler module imports from there, so
nothing imports the package it belongs to.

**The list below is the dispatch order.** The chain this replaces encoded
precedence in its sequence — an exact match ahead of a prefix that would also
match, a rewrite expanded before the family it expands into — so the order is
stated here rather than left to emerge from anything.
"""

from .agents import (
    delete_agent,
    list_agents,
    migrate_agent_cmd,
    remove_agent,
    restart_agent,
    start_agent,
    stop_agent,
)
from .dispatch import Command, CommandContext, CommandRegistry, command, registry
from .info import (
    show_bus,
    show_help,
    show_mqtt_status,
    show_nodes,
    show_registry,
    show_topics,
)
from .nodes import deploy_node, remove_node, restart_node, shutdown_node
from .state import (
    clear_plans,
    delete_rule,
    manage_memory,
    manage_plans,
    manage_webhooks,
    show_rules,
)

registry.add(show_nodes)
registry.add(show_topics)
registry.add(show_mqtt_status)
registry.add(show_bus)
registry.add(show_help)
registry.add(show_registry)
registry.add(manage_memory)
registry.add(show_rules)
registry.add(delete_rule)
registry.add(clear_plans)
registry.add(manage_plans)
registry.add(manage_webhooks)
registry.add(migrate_agent_cmd)
registry.add(deploy_node)
registry.add(restart_agent)
registry.add(remove_node)
registry.add(restart_node)
registry.add(shutdown_node)
registry.add(start_agent)
registry.add(stop_agent)
registry.add(delete_agent)
registry.add(remove_agent)
# Last of the /agents family: it matches any `/agents …`, so ahead of the verbs
# above it would read `stop` as a capability search term.
registry.add(list_agents)

__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "command",
    "registry",
]
