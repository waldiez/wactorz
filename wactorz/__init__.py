"""Wactorz - Actor-Model Multi-Agent Framework"""

from typing import Any

from ._version import __version__
from .core.actor import Actor, ActorState, Message, MessageType
from .core.registry import ActorRegistry, ActorSystem

__all__ = [
    "Actor",
    "ActorRegistry",
    "ActorState",
    "ActorSystem",
    "Message",
    "MessageType",
    "__version__",
]
# Agents & LLM providers. Optional provider SDKs (anthropic/openai) are imported
# lazily inside the providers, so every symbol below resolves on a base install.
from .agents import (
    AnthropicProvider,
    CatalogAgent,
    DynamicAgent,
    FunctionAgent,
    HomeAssistantActuatorAgent,
    HomeAssistantAgent,
    HomeAssistantMapAgent,
    HomeAssistantStateBridgeAgent,
    InstallerAgent,
    LLMAgent,
    MainActor,
    MonitorActor,
    NIMProvider,
    OllamaProvider,
    OneOffActuatorAgent,
    OpenAIProvider,
    PlannerAgent,
    RuleAgent,
    ScheduledAgent,
    agent,
)

__all__ += [
    "AnthropicProvider",
    "CatalogAgent",
    "DynamicAgent",
    "FunctionAgent",
    "HomeAssistantActuatorAgent",
    "HomeAssistantAgent",
    "HomeAssistantMapAgent",
    "HomeAssistantStateBridgeAgent",
    "InstallerAgent",
    "LLMAgent",
    "MainActor",
    "MonitorActor",
    "NIMProvider",
    "OllamaProvider",
    "OneOffActuatorAgent",
    "OpenAIProvider",
    "PlannerAgent",
    "RuleAgent",
    "ScheduledAgent",
    "agent",
    "pipeline",
    "run",
]


def pipeline(*args: Any, **kwargs: Any) -> Any:
    """Declare a pipeline of agents; see :func:`wactorz.pipelines.pipeline`.

    Imported on call for the same reason as :func:`run`: the pipelines module
    reads the configuration, which a script that only declares agents may
    not want at import.
    """
    from .pipelines import pipeline as _pipeline

    return _pipeline(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> None:
    """Start Wactorz from a script; see :func:`wactorz.app.run`.

    Imported on call rather than at package import: the application module
    pulls in the web server and every interface, which a script that only
    declares agents never needs.
    """
    from .app import run as _run

    _run(*args, **kwargs)
