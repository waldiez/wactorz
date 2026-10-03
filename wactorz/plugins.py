"""The agents a deployment brings of its own, found and built in one place.

A developer with their own actor class or a function declared with
:func:`wactorz.agent` makes it part of the system without forking ``app.py``:

- as a ``wactorz.agents`` entry point in their package, so ``pip install``
  is the whole of the registration;
- named in ``WACTORZ_AGENTS``, a comma-separated list of ``package.module:attr``
  targets, for a deployment configured rather than packaged;
- passed to :func:`wactorz.run`, for a script.

Each resolves to an :class:`AgentPlugin` that knows how to build its actor the
way ``build_system`` and the catalogue build the built-in ones. The targets
registered here are also the only ones a ``type: "module"`` spawn may name:
a spawn config can be written by the model, and resolving an arbitrary import
path from it would run whatever that path reached.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any

from .agents.function_agent import AgentSpec, agent_name_from, spec_of
from .config import CONFIG
from .core.actor import Actor

logger = logging.getLogger(__name__)

#: The entry-point group a package lists its agents under.
ENTRY_POINT_GROUP = "wactorz.agents"
#: The environment variable naming targets to load, comma or space separated.
ENV_VAR = "WACTORZ_AGENTS"


@dataclass
class AgentPlugin:
    """One agent a deployment brings: where it came from and how to build it."""

    name: str
    #: ``package.module:attr``, or ``""`` for an object registered in code.
    target: str
    #: ``"function"`` for a decorated function, ``"actor"`` for an Actor subclass.
    kind: str
    obj: Any
    description: str = ""
    capabilities: tuple[str, ...] = ()
    requires: dict[str, Any] = field(default_factory=dict)
    autostart: bool = True
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    def build(
        self,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        llm_provider: Any = None,
        options: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> Actor:
        """The actor, built the way the catalogue builds a native one.

        Keyword-only and tolerant of extras, because every caller that spawns
        by factory -- ``Actor.spawn``, the supervisor, the catalogue -- passes
        what it has, and an actor class is told only what it accepts.
        """
        if self.kind == "function":
            spec: AgentSpec = self.obj
            return spec.build(
                name=name,
                persistence_dir=persistence_dir,
                llm_provider=llm_provider,
                options=options,
            )
        cls: type[Actor] = self.obj
        kwargs: dict[str, Any] = {"name": name or self.name}
        if persistence_dir is not None:
            kwargs["persistence_dir"] = persistence_dir
        accepted = _accepted_parameters(cls)
        if llm_provider is not None and (accepted is None or "llm_provider" in accepted):
            kwargs["llm_provider"] = llm_provider
        for key, value in (options or {}).items():
            if accepted is None or key in accepted:
                kwargs[key] = value
        return cls(**kwargs)

    def recipe(self) -> dict[str, Any]:
        """This plugin as a catalogue entry, so ``@catalog`` lists and spawns it."""
        return {
            "name": self.name,
            "type": "native",
            "factory": self.build,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "requires": dict(self.requires),
            "plugin": self.target,
        }


def _accepted_parameters(cls: type) -> set[str] | None:
    """The keyword names ``cls.__init__`` takes, or None when it takes anything."""
    try:
        params = inspect.signature(cls).parameters.values()
    except (TypeError, ValueError):
        return None
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return None
    return {p.name for p in params}


def _first_line(text: str | None) -> str:
    return (text or "").strip().split("\n")[0].strip()


def plugin_from(obj: Any, target: str = "") -> AgentPlugin:
    """The plugin for a decorated function or an Actor subclass.

    Raises ``TypeError`` for anything else, naming the target, so a mistyped
    entry is reported rather than silently started as nothing.
    """
    spec = spec_of(obj)
    if spec is not None:
        return AgentPlugin(
            name=spec.name,
            target=target,
            kind="function",
            obj=spec,
            description=spec.description,
            capabilities=spec.capabilities,
            requires=dict(spec.requires),
            autostart=spec.autostart,
            input_schema=dict(spec.input_schema),
            output_schema=dict(spec.output_schema),
        )
    if inspect.isclass(obj) and issubclass(obj, Actor):
        name = getattr(obj, "AGENT_NAME", None) or agent_name_from(obj.__name__)
        return AgentPlugin(
            name=str(name),
            target=target,
            kind="actor",
            obj=obj,
            description=str(getattr(obj, "DESCRIPTION", "") or _first_line(obj.__doc__)),
            capabilities=tuple(getattr(obj, "CAPABILITIES", ()) or ()),
            requires=dict(getattr(obj, "REQUIRES", {}) or {}),
            autostart=bool(getattr(obj, "AUTOSTART", True)),
            input_schema=dict(getattr(obj, "INPUT_SCHEMA", {}) or {}),
            output_schema=dict(getattr(obj, "OUTPUT_SCHEMA", {}) or {}),
        )
    raise TypeError(
        f"{target or obj!r} is neither a function declared with @wactorz.agent "
        "nor an Actor subclass"
    )


def resolve_target(target: str) -> Any:
    """The object ``package.module:attr`` names. Raises what the import raises."""
    module_name, sep, attr = target.strip().partition(":")
    if not module_name or not sep or not attr:
        raise ValueError(f"{target!r} is not of the form package.module:attr")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def targets_in(value: str) -> list[str]:
    """The targets named in a ``WACTORZ_AGENTS`` value, in order, once each."""
    found: list[str] = []
    for raw in value.replace(",", " ").split():
        if raw and raw not in found:
            found.append(raw)
    return found


#: What has been found or registered, by agent name. None until first asked.
_plugins: dict[str, AgentPlugin] | None = None
#: Plugins registered in code, kept across a refresh of the discovered ones.
_registered: dict[str, AgentPlugin] = {}


def register(obj: Any, *, target: str = "") -> AgentPlugin:
    """Make ``obj`` -- a decorated function or an Actor subclass -- a plugin now.

    What :func:`wactorz.run` does with each agent it is given. A plugin under
    the same name replaces the earlier one.
    """
    plugin = plugin_from(obj, target or _target_of(obj))
    _registered[plugin.name] = plugin
    if _plugins is not None:
        _plugins[plugin.name] = plugin
    return plugin


def _target_of(obj: Any) -> str:
    """``module:qualname`` for an object that has them, for the record."""
    module = getattr(obj, "__module__", "") or ""
    qualname = getattr(obj, "__qualname__", "") or getattr(obj, "__name__", "") or ""
    return f"{module}:{qualname}" if module and qualname else ""


def clear() -> None:
    """Forget everything found or registered. For tests, and for a fresh run."""
    global _plugins
    _plugins = None
    _registered.clear()


def discover(*, env: str | None = None, refresh: bool = False) -> dict[str, AgentPlugin]:
    """Every plugin, by name: entry points, then ``WACTORZ_AGENTS``, then registered.

    Found once and kept; ``refresh`` looks again. A target that cannot be
    imported or is not an agent is logged and left out, so one bad entry does
    not keep the rest of the system from starting.
    """
    global _plugins
    if _plugins is not None and not refresh:
        return dict(_plugins)
    found: dict[str, AgentPlugin] = {}
    for entry_point in _entry_points():
        try:
            plugin = plugin_from(entry_point.load(), entry_point.value)
        except Exception as exc:
            logger.warning("[plugins] Skipping entry point %s: %s", entry_point.value, exc)
            continue
        found[plugin.name] = plugin
    if env is None:
        env = CONFIG.agents_env
    for target in targets_in(env):
        try:
            plugin = plugin_from(resolve_target(target), target)
        except Exception as exc:
            # An error, not a warning: the deployment named this agent and will
            # look for it on the dashboard. Said with the usual cause, since a
            # module beside a script is not on the path of a `wactorz` start.
            logger.error(
                "[plugins] %s names %s, which could not be loaded: %s. The module must be "
                "importable from where wactorz starts; set PYTHONPATH or install the package.",
                ENV_VAR,
                target,
                exc,
            )
            continue
        found[plugin.name] = plugin
    found.update(_registered)
    _plugins = found
    if found:
        logger.info("[plugins] Agents: %s", ", ".join(sorted(found)))
    return dict(found)


def _entry_points() -> list[Any]:
    try:
        return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # pragma: no cover - a broken installation
        logger.warning("[plugins] Could not read %s entry points: %s", ENTRY_POINT_GROUP, exc)
        return []


def for_name(name: str) -> AgentPlugin | None:
    """The plugin called ``name``, or None."""
    return discover().get(name)


def for_target(target: str) -> AgentPlugin | None:
    """The plugin registered for ``target``, or None: only these may be spawned by path."""
    wanted = target.strip()
    if not wanted:
        return None
    for plugin in discover().values():
        if plugin.target == wanted:
            return plugin
    return None
