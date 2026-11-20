# SPDX-License-Identifier: Apache-2.0
"""Wactorz - Actor-Model Multi-Agent Framework"""

from typing import Any

# Before the rest: the Windows fixups must run before anything below imports aiohttp.
from . import _bootstrap  # pyright: ignore[reportUnusedImport]
from ._version import __version__

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
from .app import run, serve, system
from .core.actor import DEFAULT_ASK_TIMEOUT_S, Actor, ActorState, Message, MessageType
from .core.registry import ActorRegistry, ActorSystem
from .errors import NoSystemError, StartupError
from .orchestration import DirectOrchestrator, MainOrchestrator, Orchestrator
from .pipelines import pipeline

__all__ = [
    "Actor",
    "ActorRegistry",
    "ActorState",
    "ActorSystem",
    "AnthropicProvider",
    "CatalogAgent",
    "DirectOrchestrator",
    "DynamicAgent",
    "FunctionAgent",
    "HomeAssistantActuatorAgent",
    "HomeAssistantAgent",
    "HomeAssistantMapAgent",
    "HomeAssistantStateBridgeAgent",
    "InstallerAgent",
    "LLMAgent",
    "MainActor",
    "MainOrchestrator",
    "Message",
    "MessageType",
    "MonitorActor",
    "NIMProvider",
    "OllamaProvider",
    "OneOffActuatorAgent",
    "OpenAIProvider",
    "Orchestrator",
    "PlannerAgent",
    "RuleAction",
    "RuleAgent",
    "RuleCondition",
    "RuleConfig",
    "ScheduledAgent",
    "StartupError",
    "__version__",
    "agent",
    "ask",
    "pipeline",
    "run",
    "serve",
    "spec_of",
    "system",
]


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
        raise NoSystemError()
    return await running.registry.ask(target, payload, timeout=timeout)
