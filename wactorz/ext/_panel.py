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
"""

import re
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

#: What a view can be called: the dashboard's address reads the same pattern.
_VIEW_NAME = re.compile(r"^[a-z][a-z0-9-]*$")

#: The dashboard's own views, which an extension's tab cannot take the place of.
RESERVED_VIEWS = frozenset({"overview", "feed", "chat", "models", "inbox", "settings"})


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
