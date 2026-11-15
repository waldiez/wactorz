# SPDX-License-Identifier: Apache-2.0
"""What an extension declares to add a tab to the dashboard.

A package's dashboard tab is a script it ships, which the page loads when the tab
opens. The script runs in the page with the signed-in session, so a panel is as
trusted as the extension's own code on the server, and it is offered only for an
extension that loaded, which for a package means one named in
``WACTORZ_EXTENSIONS``.

An extension declares one with an optional hook, read after its ``setup()``
succeeds::

    def panel(app) -> Panel:
        return Panel(title="MLOps", script=Path(__file__).parent / "panel.js")

Its icon is one of the dashboard's by name, or an ``.svg`` file the package
ships. A file is read once, when the extension loads, and the page keeps only
its shapes, drawn as the dashboard's own icons are: lines in the text's colour
on a 24 by 24 grid. A file that cannot be used leaves the tab in place, on the
default icon, and the log says why.

A panel may also declare settings, which the dashboard draws as a section of
its Settings, named after the tab and laid out as its own sections are::

    Panel(
        title="Air",
        script=HERE / "panel.js",
        settings=(
            PanelSetting(
                "temperature",
                "Temperature",
                kind="choice",
                choices=(("c", "°C"), ("f", "°F")),
                default="c",
            ),
        ),
    )

The server only describes them: each browser keeps its own values, and the
panel's script reads them from the dashboard. A setting that cannot be drawn is
left out, with the others in place, and the log says why.
"""

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from aiohttp import web

#: What a view can be called: the dashboard's address reads the same pattern.
_VIEW_NAME = re.compile(r"^[a-z][a-z0-9-]*$")

#: The dashboard's own views, which an extension's tab cannot take the place of.
RESERVED_VIEWS = frozenset({"overview", "feed", "chat", "models", "inbox", "settings"})


#: The controls a setting can be drawn as.
SETTING_KINDS = ("switch", "choice", "number")


@dataclass(frozen=True)
class PanelSetting:
    """One value a panel lets each browser choose, drawn as a row of the panel's Settings section."""

    #: What the panel's script asks for it by: lower-case letters, digits and hyphens.
    key: str
    #: The row's name.
    label: str
    #: The control: an on/off ``switch``, a ``choice`` among a few, or a ``number``.
    kind: Literal["switch", "choice", "number"] = "switch"
    #: The value until the browser chooses one: a bool, one of the choices' values, or a number.
    default: bool | str | float = False
    #: One line under the name, on what the setting does.
    hint: str = ""
    #: For a ``choice``: each value and what it says, in the order drawn.
    choices: tuple[tuple[str, str], ...] = ()
    #: For a ``number``: the least it may be, when there is one.
    minimum: float | None = None
    #: For a ``number``: the most it may be, when there is one.
    maximum: float | None = None
    #: For a ``number``: the distance between the values offered, when it matters.
    step: float | None = None


@dataclass(frozen=True)
class Panel:
    """A dashboard tab an extension adds."""

    #: The tab's label.
    title: str
    #: The ES module the page loads, in the extension's package.
    script: Path
    #: The name of one of the dashboard's icons, or an ``.svg`` file in the package.
    icon: str | Path = "grid"
    #: MQTT topic filters the panel listens to, beyond what the dashboard relays.
    topics: tuple[str, ...] = ()
    #: What each browser may choose for the panel, drawn as a section of Settings.
    settings: tuple[PanelSetting, ...] = ()


#: Each loaded extension's panel, by the extension's short name.
PANELS: web.AppKey[dict[str, Panel]] = web.AppKey("wactorz_panels", dict)
#: The text of each panel's icon file, by the extension's short name, for a panel that ships one.
PANEL_ICONS: web.AppKey[dict[str, str]] = web.AppKey("wactorz_panel_icons", dict)

#: The icon a page shows for a panel whose own icon is a file, should it not read the file.
DEFAULT_ICON = "grid"
#: The most an icon file may hold: a line icon is a handful of shapes.
ICON_MAX_BYTES = 16 * 1024


class PanelIconError(ValueError):
    """A panel's icon file that cannot be shown."""

    @classmethod
    def not_svg(cls, path: Path) -> "PanelIconError":
        return cls(f"its icon {path} is not an .svg file")

    @classmethod
    def too_large(cls, path: Path) -> "PanelIconError":
        return cls(f"its icon {path} is larger than {ICON_MAX_BYTES} bytes")

    @classmethod
    def unreadable(cls, path: Path, reason: Exception) -> "PanelIconError":
        return cls(f"its icon {path} cannot be read: {reason}")

    @classmethod
    def no_drawing(cls, path: Path) -> "PanelIconError":
        return cls(f"its icon {path} holds no <svg> element")


def panel_problem(name: str, panel: Panel) -> str | None:
    """Why the extension ``name`` cannot have ``panel``, or None if it can."""
    if not _VIEW_NAME.match(name):
        return "its name cannot be a dashboard address"
    if name in RESERVED_VIEWS:
        return "the dashboard has a view of that name"
    if not panel.title.strip():
        return "it has no title"
    if panel.script.suffix != ".js" or not panel.script.is_file():
        return f"{panel.script} is not a .js file"
    return None


def icon_file_text(panel: Panel) -> str | None:
    """The text of ``panel``'s icon file, or None when its icon is one of the dashboard's.

    Raises:
        PanelIconError: the file is not an SVG the page could draw.
    """
    if not isinstance(panel.icon, Path):
        return None
    path = panel.icon
    if path.suffix.lower() != ".svg" or not path.is_file():
        raise PanelIconError.not_svg(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PanelIconError.unreadable(path, exc) from exc
    if len(data) > ICON_MAX_BYTES:
        raise PanelIconError.too_large(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PanelIconError.unreadable(path, exc) from exc
    if "<svg" not in text:
        raise PanelIconError.no_drawing(path)
    return text


def _is_number(value: object) -> bool:
    """Whether ``value`` is a number a page could show: a bool is not one, nor is infinity."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _choice_problem(setting: PanelSetting) -> str | None:
    """Why ``setting`` cannot be drawn as a choice, or None if it can."""
    if not setting.choices:
        return "it has no choices"
    values: list[str] = []
    for choice in setting.choices:
        if (
            not isinstance(choice, tuple)
            or len(choice) != 2
            or not all(isinstance(part, str) and part.strip() for part in choice)
        ):
            return "each choice is a (value, label) pair of words"
        values.append(choice[0])
    if len(set(values)) != len(values):
        return "two of its choices have the same value"
    if setting.default not in values:
        return "its default is not one of its choices"
    return None


def _number_problem(setting: PanelSetting) -> str | None:
    """Why ``setting`` cannot be drawn as a number, or None if it can."""
    default = setting.default
    if isinstance(default, (bool, str)) or not _is_number(default):
        return "its default is not a number"
    for name, bound in (("minimum", setting.minimum), ("maximum", setting.maximum)):
        if bound is not None and not _is_number(bound):
            return f"its {name} is not a number"
    if setting.step is not None and not (_is_number(setting.step) and setting.step > 0):
        return "its step is not a number above zero"
    low = -math.inf if setting.minimum is None else setting.minimum
    high = math.inf if setting.maximum is None else setting.maximum
    if low > high:
        return "its minimum is above its maximum"
    if not low <= default <= high:
        return "its default is outside its minimum and maximum"
    return None


def setting_problem(setting: PanelSetting) -> str | None:
    """Why ``setting`` cannot be drawn, or None if it can."""
    if not isinstance(setting.key, str) or not _VIEW_NAME.match(setting.key):
        return "its key is not lower-case letters, digits and hyphens, a letter first"
    if not isinstance(setting.label, str) or not setting.label.strip():
        return "it has no label"
    if not isinstance(setting.hint, str):
        return "its hint is not text"
    if setting.kind == "switch":
        return None if isinstance(setting.default, bool) else "its default is not True or False"
    if setting.kind == "choice":
        return _choice_problem(setting)
    if setting.kind == "number":
        return _number_problem(setting)
    return f"its kind is none of {', '.join(SETTING_KINDS)}"


def usable_settings(panel: Panel) -> tuple[tuple[PanelSetting, ...], list[str]]:
    """The settings of ``panel`` that can be drawn, and a line on each that cannot.

    A key is one setting's: a second setting with a key already taken is left out.
    """
    kept: list[PanelSetting] = []
    problems: list[str] = []
    for setting in panel.settings:
        if not isinstance(setting, PanelSetting):
            problems.append(f"{setting!r} is not a PanelSetting")
            continue
        problem = setting_problem(setting)
        if problem is None and any(other.key == setting.key for other in kept):
            problem = "another setting has its key"
        if problem is None:
            kept.append(setting)
        else:
            problems.append(f"setting {setting.key!r}: {problem}")
    return tuple(kept), problems


def setting_entry(setting: PanelSetting) -> dict[str, Any]:
    """``setting`` as the page reads it: what its kind does not use is left out."""
    entry: dict[str, Any] = {
        "key": setting.key,
        "label": setting.label,
        "hint": setting.hint,
        "kind": setting.kind,
        "default": setting.default,
    }
    if setting.kind == "choice":
        entry["choices"] = [{"value": value, "label": label} for value, label in setting.choices]
    if setting.kind == "number":
        bounds = (("min", setting.minimum), ("max", setting.maximum), ("step", setting.step))
        entry.update({name: bound for name, bound in bounds if bound is not None})
    return entry
