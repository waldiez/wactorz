"""AgentFlow - Actor-Model Multi-Agent Framework"""
from ._version import __version__
from .core.actor import Actor, ActorState, Message, MessageType
from .core.registry import ActorSystem, ActorRegistry

__all__ = [
    "__version__",
    "Actor", "ActorState", "Message", "MessageType",
    "ActorSystem", "ActorRegistry",
]

# Optional agents — only exported when their dependencies are available.
try:
    from .agents.llm_agent import LLMAgent, AnthropicProvider, OpenAIProvider, OllamaProvider, NIMProvider
    __all__ += ["LLMAgent", "AnthropicProvider", "OpenAIProvider", "OllamaProvider", "NIMProvider"]
except ImportError:
    pass

try:
    from .agents.main_actor import MainActor
    from .agents.monitor_agent import MonitorActor
    from .agents.code_agent import CodeAgent
    from .agents.manual_agent import ManualAgent
    from .agents.planner_agent import PlannerAgent
    __all__ += ["MainActor", "MonitorActor", "CodeAgent", "ManualAgent", "PlannerAgent"]
except ImportError:
    pass

try:
    from .agents.ml_agent import MLAgent, YOLOAgent, AnomalyDetectorAgent
    __all__ += ["MLAgent", "YOLOAgent", "AnomalyDetectorAgent"]
except ImportError:
    pass

try:
    from .agents.home_assistant_hardware_agent import HomeAssistantHardwareAgent
    __all__ += ["HomeAssistantHardwareAgent"]
except ImportError:
    pass