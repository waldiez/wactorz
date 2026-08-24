"""What each planner mixin requires of the actor that mixes it in.

Typing-only, in the spirit of `mixins/host.py`: nothing here runs and nothing is
inherited at runtime, so the real MRO is exactly what it would be without these.

Layered rather than combined, because the layering is the interesting part. The
context mixin needs nothing the shared protocols do not already give it; the
pipeline mixin reaches into both of the others. A mixin asking for something
surprising is a fact about coupling, not a detail to hide behind one protocol
listing everything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ..mixins.host import SpawnHost

if TYPE_CHECKING:
    from ...core.actor import Actor
    from ..mixins.spawning import SpawnPlaceholder


class PlannerHost(SpawnHost, Protocol):
    """State and helpers every planner mixin reads off the agent.

    Built on ActorHost rather than LLMHost even though the planner uses an LLM:
    LLMHost also promises `_persist_cost`, which belongs to LLMAgent, and
    PlannerAgent extends Actor directly. Inheriting it would have the protocol
    assert something about the host that is not true. SpawnHost already carries
    `llm` and `_result_futures`, which is the whole LLM surface these mixins use.
    """

    _plan_only: bool
    _approved_plan: dict[str, Any] | None
    _auto_terminate: bool
    _spawned_by_planner: list[str]
    _spawn_results: dict[str, dict[str, Any]]

    def _accrue_usage(self, usage: dict[str, Any]) -> None: ...

    def _now_context(self) -> str: ...

    def _validate_pipeline_code(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    async def _llm_answer(self, task: str) -> str: ...

    async def _log(self, msg: str) -> None: ...


class ExecutionHost(PlannerHost, Protocol):
    """Running a plan also needs the spawn machinery the agent mixes in."""

    async def _spawn_local_from_config(
        self,
        config: dict[str, Any],
        *,
        register: bool = True,
        blocking_install: bool = False,
        from_registry: bool = False,
    ) -> Actor | SpawnPlaceholder | None: ...


class PipelineHost(ExecutionHost, Protocol):
    """The pipeline path drives both of the other mixins."""

    async def _spawn_agent(self, config: dict[str, Any]) -> Actor | SpawnPlaceholder | None: ...

    async def _delegate(
        self, agent_name: str, task: str, timeout: float = 60.0
    ) -> dict[str, Any] | None: ...

    async def _delegate_with_payload(
        self, agent_name: str, payload: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any] | None: ...

    async def _resolve_data_references(self, task: str) -> tuple[str, str]: ...

    async def _sample_live_topics(self, bus: Any) -> list[str]: ...
