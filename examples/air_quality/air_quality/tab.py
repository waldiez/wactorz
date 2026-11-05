"""The Air tab: a card per room, drawn from what the agents publish.

An extension whose only part is a panel: the page reads everything it shows
from MQTT, so the server has no route to add. Its icon is the package's own
file, drawn on the dashboard's grid.
"""

from pathlib import Path

from aiohttp import web

from wactorz.ext import Panel

HERE = Path(__file__).parent


def setup(_app: web.Application) -> None:
    """Nothing to set up: the tab reads the broker, through the page."""


def panel(_app: web.Application) -> Panel:
    return Panel(
        title="Air",
        script=HERE / "panel.js",
        icon=HERE / "icon.svg",
        topics=("sensors/air/#", "air/#"),
    )
