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
# Agents & LLM providers. Optional provider SDKs (anthropic/openai) are imported
# lazily inside the providers, so every symbol below resolves on a base install.
from .agents import (
    AnthropicProvider,
    CatalogAgent,
    DynamicAgent,
    HomeAssistantActuatorAgent,
    HomeAssistantAgent,
    HomeAssistantMapAgent,
    HomeAssistantStateBridgeAgent,
    InstallerAgent,
    IOAgent,
    LLMAgent,
    MainActor,
    MonitorActor,
    NIMProvider,
    OllamaProvider,
    OneOffActuatorAgent,
    OpenAIProvider,
    PlannerAgent,
    ScheduledAgent,
)

__all__ += [
    "LLMAgent",
    "AnthropicProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "NIMProvider",
    "CatalogAgent",
    "DynamicAgent",
    "HomeAssistantActuatorAgent",
    "HomeAssistantAgent",
    "HomeAssistantMapAgent",
    "HomeAssistantStateBridgeAgent",
    "InstallerAgent",
    "IOAgent",
    "MainActor",
    "MonitorActor",
    "OneOffActuatorAgent",
    "PlannerAgent",
    "ScheduledAgent",
]
