"""A pipeline: agents that work together, declared in one place.

The planner builds one from a request; this is the same thing written down by
a developer. Each step is an agent of its own -- a function declared with
:func:`wactorz.agent`, an Actor subclass, or a registered target -- with its
own card, counters and persistence. The pipeline is what groups them: a
schedule that ticks the first step, rules that act on what the steps publish,
and a record in main's rule registry under the pipeline's name, so ``/rules``
lists it and ``/rules delete`` stops the whole of it.

    import wactorz

    watch = wactorz.pipeline(
        "imu-watch",
        steps=[detect, notify],
        inputs=["sensors/imu/#"],
        schedule={"type": "interval", "seconds": 300},
        rules=[{"triggers": ["anomalies/imu"],
                "conditions": [{"field": "score", "op": "gt", "value": 20}],
                "actions": [{"type": "task", "agent": "notify",
                             "payload": {"text": "IMU anomaly, score {score}"}}]}],
    )

Wiring is checked when the pipeline is declared: a step or a rule that listens
on a topic nothing in the pipeline publishes, and that is not named in
``inputs``, is an error before anything starts. The first step may listen to
the outside world without saying so; that is what a first step is for.
"""

from __future__ import annotations

import importlib.metadata
import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from . import plugins
from .agents.rule_agent import RuleConfig
from .config import CONFIG
from .core.topic_bus import topic_matches
from .plugins import AgentPlugin

logger = logging.getLogger(__name__)

#: The entry-point group a package lists its pipelines under.
ENTRY_POINT_GROUP = "wactorz.pipelines"
#: The environment variable naming pipeline targets to load.
ENV_VAR = "WACTORZ_PIPELINES"


@dataclass(frozen=True)
class Pipeline:
    """What :func:`pipeline` declared: the steps, the glue, and the wiring between them."""

    name: str
    steps: tuple[AgentPlugin, ...]
    inputs: tuple[str, ...] = ()
    schedule: dict[str, Any] | None = None
    rules: tuple[RuleConfig, ...] = ()
    description: str = ""
    #: Where the pipeline came from, for the record.
    target: str = ""
    #: Names the rule agents go by, in order, fixed at declaration so a
    #: restart finds the same ones.
    rule_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tick_topic(self) -> str:
        """Where the schedule, if any, publishes its tick."""
        return f"pipelines/{self.name}/tick"

    @property
    def schedule_name(self) -> str:
        return f"{self.name}-schedule"

    @property
    def agent_names(self) -> tuple[str, ...]:
        """Every agent this pipeline runs, steps first."""
        names = [step.name for step in self.steps]
        if self.schedule is not None:
            names.append(self.schedule_name)
        names.extend(self.rule_names)
        return tuple(names)

    def producers(self) -> tuple[str, ...]:
        """Every topic something in this pipeline publishes to."""
        topics: list[str] = []
        if self.schedule is not None:
            topics.append(self.tick_topic)
        for step in self.steps:
            topics.extend(step.publishes)
        for rule in self.rules:
            topics.extend(rule.publishes)
        return tuple(topics)

    def record(self) -> dict[str, Any]:
        """The entry main keeps for this pipeline, as the planner keeps its own."""
        return {
            "rule_id": self.name,
            "task": self.description or f"pipeline {self.name}",
            "agents": list(self.agent_names),
            "created_at": time.time(),
            "source": "pipeline",
            "target": self.target,
        }

    def spawn_configs(self) -> list[dict[str, Any]]:
        """The pipeline as spawn configs, for a reader that wants the planner's shape."""
        configs: list[dict[str, Any]] = [
            {"name": step.name, "type": "module", "target": step.target, "pipeline": self.name}
            for step in self.steps
        ]
        if self.schedule is not None:
            configs.append(
                {
                    "name": self.schedule_name,
                    "type": "scheduled",
                    "schedule": dict(self.schedule),
                    "publish_topic": self.tick_topic,
                    "pipeline": self.name,
                }
            )
        for rule_name, rule in zip(self.rule_names, self.rules, strict=True):
            configs.append(
                {"name": rule_name, "type": "rule", **rule.to_dict(), "pipeline": self.name}
            )
        return configs


def wiring_errors(pipe: Pipeline) -> list[str]:
    """What listens on a topic nothing provides. Empty when the pipeline is wired."""
    produced = pipe.producers()
    errors: list[str] = []

    def covered(pattern: str) -> bool:
        if pattern in pipe.inputs:
            return True
        return any(topic_matches(pattern, topic) for topic in produced)

    for index, step in enumerate(pipe.steps):
        if index == 0:
            # The first step listens to the world; what it hears is the input.
            continue
        for pattern in step.subscribes:
            if not covered(pattern):
                errors.append(
                    f"step {step.name!r} listens on {pattern!r}, which nothing in the "
                    f"pipeline publishes; name it in inputs if it comes from outside"
                )
    for rule_name, rule in zip(pipe.rule_names, pipe.rules, strict=True):
        for pattern in rule.triggers:
            if not covered(pattern):
                errors.append(
                    f"rule {rule_name!r} triggers on {pattern!r}, which nothing in the "
                    f"pipeline publishes; name it in inputs if it comes from outside"
                )
    seen: set[str] = set()
    for name in pipe.agent_names:
        if name in seen:
            errors.append(f"two agents would be called {name!r}")
        seen.add(name)
    return errors


def _step_plugin(step: Any, pipeline_name: str) -> AgentPlugin:
    """The plugin for one step, registered so it may be spawned by target."""
    if isinstance(step, AgentPlugin):
        plugin = step
    elif isinstance(step, str):
        plugin = plugins.plugin_from(plugins.resolve_target(step), step)
    else:
        plugin = plugins.plugin_from(step, plugins._target_of(step))
    # Started by the pipeline, not by the plugin loop, so it is not started twice.
    plugin.autostart = False
    plugins.register_plugin(plugin)
    return plugin


def pipeline(
    name: str,
    steps: Iterable[Any] = (),
    *,
    inputs: Iterable[str] = (),
    schedule: dict[str, Any] | None = None,
    rules: Iterable[dict[str, Any] | RuleConfig] = (),
    description: str = "",
) -> Pipeline:
    """Declare a pipeline, check its wiring, and register it to be started.

    ``steps`` are decorated functions, Actor subclasses, ``AgentPlugin`` objects
    or ``package.module:attr`` targets. ``schedule`` is a scheduled agent's spec
    (``{"type": "interval", "seconds": 300}`` and the other forms) that ticks on
    ``pipelines/<name>/tick``. Each of ``rules`` is a rule config, as
    :mod:`wactorz.agents.rule_agent` describes. Raises ``ValueError`` for a
    pipeline that cannot work, saying what is wrong.
    """
    if not name or not name.strip():
        raise ValueError("a pipeline needs a name")
    step_plugins = tuple(_step_plugin(step, name) for step in steps)
    rule_configs = tuple(r if isinstance(r, RuleConfig) else RuleConfig.from_dict(r) for r in rules)
    if not step_plugins and not rule_configs:
        raise ValueError(f"pipeline {name!r} has neither steps nor rules")
    rule_names = tuple(
        f"{name}-rule-{index + 1}" if len(rule_configs) > 1 else f"{name}-rule"
        for index in range(len(rule_configs))
    )
    pipe = Pipeline(
        name=name.strip(),
        steps=step_plugins,
        inputs=tuple(str(t) for t in inputs),
        schedule=dict(schedule) if schedule else None,
        rules=rule_configs,
        description=description,
        rule_names=rule_names,
    )
    problems = wiring_errors(pipe)
    if problems:
        raise ValueError(f"pipeline {name!r} is not wired: " + "; ".join(problems))
    register(pipe)
    return pipe


#: What has been declared or found, by name. None until first asked.
_pipelines: dict[str, Pipeline] | None = None
_registered: dict[str, Pipeline] = {}


def register(pipe: Pipeline) -> Pipeline:
    """Keep ``pipe`` to be started with the system. Same name replaces."""
    _registered[pipe.name] = pipe
    if _pipelines is not None:
        _pipelines[pipe.name] = pipe
    return pipe


@contextmanager
def registered(pipes: Iterable[Pipeline]) -> Iterator[list[Pipeline]]:
    """Register ``pipes`` for the length of a ``with`` block, then put back what was there.

    The pipelines' counterpart of :func:`wactorz.plugins.registered`, for the
    pipelines :func:`wactorz.serve` is handed. One declared at module level
    with :func:`pipeline` registered itself when it was declared and stays.
    """
    global _pipelines
    before = dict(_registered)
    added: list[Pipeline] = []
    try:
        for pipe in pipes:
            added.append(register(pipe))
        yield added
    finally:
        for pipe in added:
            if pipe.name in before:
                _registered[pipe.name] = before[pipe.name]
            else:
                _registered.pop(pipe.name, None)
        _pipelines = None


def clear() -> None:
    """Forget every pipeline. For tests."""
    global _pipelines
    _pipelines = None
    _registered.clear()


def discover(*, env: str | None = None, refresh: bool = False) -> dict[str, Pipeline]:
    """Every pipeline, by name: entry points, then ``WACTORZ_PIPELINES``, then declared.

    A target names a module attribute holding a :class:`Pipeline`, which
    declaring one with :func:`pipeline` at module level produces. A target
    that cannot be loaded is reported and left out.
    """
    global _pipelines
    if _pipelines is not None and not refresh:
        return dict(_pipelines)
    found: dict[str, Pipeline] = {}
    for entry_point in _entry_points():
        try:
            found.update(_from_object(entry_point.load(), entry_point.value))
        except Exception as exc:
            logger.error(
                "[pipelines] Entry point %s could not be loaded: %s", entry_point.value, exc
            )
    if env is None:
        env = CONFIG.pipelines_env
    for target in plugins.targets_in(env):
        try:
            found.update(_from_object(plugins.resolve_target(target), target))
        except Exception as exc:
            logger.error(
                "[pipelines] %s names %s, which could not be loaded: %s. The module must be "
                "importable from where wactorz starts; set PYTHONPATH or install the package.",
                ENV_VAR,
                target,
                exc,
            )
    found.update(_registered)
    _pipelines = found
    if found:
        logger.info("[pipelines] Pipelines: %s", ", ".join(sorted(found)))
    return dict(found)


def _from_object(obj: Any, target: str) -> dict[str, Pipeline]:
    """The pipelines ``obj`` holds: one, or an iterable of them."""
    if isinstance(obj, Pipeline):
        found = {obj.name: obj}
    elif isinstance(obj, Iterable) and not isinstance(obj, (str, bytes)):
        found = {}
        for item in obj:
            if not isinstance(item, Pipeline):
                raise TypeError(f"{target} holds {item!r}, which is not a Pipeline")
            found[item.name] = item
    else:
        raise TypeError(f"{target} is not a Pipeline")
    for pipe in found.values():
        if not pipe.target:
            object.__setattr__(pipe, "target", target)
    return found


def _entry_points() -> list[Any]:
    try:
        return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # pragma: no cover - a broken installation
        logger.warning("[pipelines] Could not read %s entry points: %s", ENTRY_POINT_GROUP, exc)
        return []
