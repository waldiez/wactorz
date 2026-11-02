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
    #: The name of one of the dashboard's icons.
    icon: str = "grid"
    #: MQTT topic filters the panel listens to, beyond what the dashboard relays.
    topics: tuple[str, ...] = ()


#: Each loaded extension's panel, by the extension's short name.
PANELS: web.AppKey[dict[str, Panel]] = web.AppKey("wactorz_panels", dict)


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
