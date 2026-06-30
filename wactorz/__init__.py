"""Wactorz - Actor-Model Multi-Agent Framework"""

from ._version import __version__
from .core.actor import Actor, ActorState, Message, MessageType
from .core.registry import ActorRegistry, ActorSystem

__all__ = [
    "__version__",
    "Actor",
    "ActorState",
    "Message",
    "MessageType",
    "ActorSystem",
    "ActorRegistry",
]
# Optional agents — only exported when their dependencies are available.
try:
    from .agents.llm_agent import (
        AnthropicProvider,
        LLMAgent,
        NIMProvider,
        OllamaProvider,
        OpenAIProvider,
    )

    __all__ += ["LLMAgent", "AnthropicProvider", "OpenAIProvider", "OllamaProvider", "NIMProvider"]
except ImportError:
    pass
try:
    from .agents.catalog_agent import CatalogAgent
    from .agents.dynamic_agent import DynamicAgent
    from .agents.installer_agent import InstallerAgent
    from .agents.main_actor import MainActor
    from .agents.monitor_agent import MonitorActor
    from .agents.planner_agent import PlannerAgent

    __all__ += [
        "MainActor",
        "MonitorActor",
        "CodeAgent",
        "PlannerAgent",
        "DynamicAgent",
        "InstallerAgent",
        "CatalogAgent",
    ]
except ImportError:
    pass
