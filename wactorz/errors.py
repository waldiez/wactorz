# SPDX-License-Identifier: Apache-2.0
"""Exceptions a program embedding Wactorz can catch, in one place.

Each is defined beside the code that raises it and named here as well, so a
library user imports from ``wactorz.errors`` rather than from wherever the
raise happens to live. Most subclass the built-in a caller would catch
(`LookupError` for something not there, `TimeoutError` for a wait that ran
out, `ValueError` for input that cannot be taken, `TypeError` for an object of
the wrong kind), so code that catches those keeps working; `ModelError`
belongs to the artifact store's own `ArtifactError`. The library guide's
*What to catch* says which call raises which, and a test holds the two to
each other.

`StartupError` and `NoSystemError` are defined here. The modules named below
must not import :mod:`wactorz.app` or :mod:`wactorz.orchestration`, which
import this module for `StartupError`.
"""

from .agents.rule_agent import RuleError
from .core.actor import (
    AskError,
    NoAnswerError,
    NotBytesError,
    NotCallableError,
    NotFoundError,
    ReplyError,
    UnnamedModelError,
)
from .core.artifacts import ArtifactError
from .core.avatars import AvatarError
from .core.integrations.ag2 import AG2Error
from .core.labels import LabelError
from .core.models import ModelError
from .core.outbound import OptionsError, UnknownChannelError
from .core.subscriptions import ConcurrencyError
from .llm_factory import ProviderUnavailableError
from .pipelines import NotAPipelineError, PipelineError
from .plugins import NotAnAgentError

__all__ = [
    "AG2Error",
    "ArtifactError",
    "AskError",
    "AvatarError",
    "ConcurrencyError",
    "LabelError",
    "ModelError",
    "NoAnswerError",
    "NoSystemError",
    "NotAPipelineError",
    "NotAnAgentError",
    "NotBytesError",
    "NotCallableError",
    "NotFoundError",
    "OptionsError",
    "PipelineError",
    "ProviderUnavailableError",
    "ReplyError",
    "RuleError",
    "StartupError",
    "UnknownChannelError",
    "UnnamedModelError",
]


class StartupError(RuntimeError):
    """The system cannot start as configured.

    Raised by :func:`wactorz.serve` and :func:`wactorz.app.app` for what the
    ``wactorz`` command reports and exits on: an exposed bind address without
    an API key, a broker certificate that cannot be loaded, a chat interface
    whose token is missing, an orchestrator that did not come up. The message
    says what to change. The command turns it into exit status 1; a host
    program catches it like any other exception.
    """


class NoSystemError(RuntimeError):
    """A call that needs a running system, made with none running."""

    def __init__(self) -> None:
        super().__init__("wactorz.ask: no system is running; start one with run() or serve()")
