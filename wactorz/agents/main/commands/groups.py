"""The headings `/help` lists commands under, in the order it lists them.

Named once here, so a command and the help that lists it cannot spell its
heading two ways, and a heading no command uses is easy to see.
"""

AGENTS = "Agents"
NODES = "Nodes"
MODELS = "Models"
PIPELINES = "Pipelines & Plans"
MEMORY = "Memory"
NOTIFICATIONS = "Notifications"
SYSTEM = "System & diagnostics"

#: Every heading, in the order `/help` shows them.
ORDER = (AGENTS, NODES, MODELS, PIPELINES, MEMORY, NOTIFICATIONS, SYSTEM)
