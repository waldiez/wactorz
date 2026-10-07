"""Wactorz - Actor-Model Multi-Agent Framework"""

from typing import Any

from ._version import __version__
from .core.actor import DEFAULT_ASK_TIMEOUT_S, Actor, ActorState, Message, MessageType
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
from .agents.function_agent import spec_of
from .agents.rule_agent import RuleAction, RuleCondition, RuleConfig
from .errors import StartupError

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
    "RuleAction",
    "RuleAgent",
    "RuleCondition",
    "RuleConfig",
    "ScheduledAgent",
    "StartupError",
    "agent",
    "ask",
    "pipeline",
    "run",
    "serve",
    "spec_of",
    "system",
]


def pipeline(*args: Any, **kwargs: Any) -> Any:
    """Declare a pipeline of agents; see :func:`wactorz.pipelines.pipeline`.

    Imported on call for the same reason as :func:`run`: the pipelines module
    reads the configuration, which a script that only declares agents may
    not want at import.
    """
    from .pipelines import pipeline as _pipeline

    return _pipeline(*args, **kwargs)


async def serve(*args: Any, **kwargs: Any) -> None:
    """Run Wactorz on the caller's event loop; see :func:`wactorz.app.serve`."""
    from .app import serve as _serve

    await _serve(*args, **kwargs)


def system() -> ActorSystem | None:
    """The running :class:`ActorSystem`, or ``None`` outside a run; see :func:`wactorz.app.system`.

    For a host that embeds the system and wants at its actors:
    ``wactorz.system().registry.find_by_name("imu-anomaly")``.
    """
    from .app import system as _system

    return _system()


async def ask(target: str, payload: Any, *, timeout: float = DEFAULT_ASK_TIMEOUT_S) -> Any:
    """Send the running agent called ``target`` a task and wait for its reply.

    For host code beside the system -- a notebook cell, a web handler, a test
    that started :func:`serve` in the background::

        score = await wactorz.ask("imu-anomaly", {"ax": 9, "ay": 0, "az": 1})

    Returns what the agent answered: a decorated function's return value (a
    non-dict wrapped as ``{"result": ...}``), less the correlation id. Raises
    ``RuntimeError`` when no system is running or the agent answered with an
    error, ``LookupError`` when no agent of that name is running, and
    ``asyncio.TimeoutError`` after ``timeout`` seconds. An agent asks another
    the same way with :meth:`Actor.ask`.
    """
    running = system()
    if running is None:
        raise RuntimeError("wactorz.ask: no system is running; start one with run() or serve()")
    return await running.registry.ask(target, payload, timeout=timeout)


def run(*args: Any, **kwargs: Any) -> None:
    """Start Wactorz from a script; see :func:`wactorz.app.run`.

    Imported on call rather than at package import: the application module
    pulls in the web server and every interface, which a script that only
    declares agents never needs.
    """
    from .app import run as _run

    _run(*args, **kwargs)
