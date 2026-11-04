"""The Air tab: a card per room, drawn from what the agents publish.

An extension whose only part is a panel: the page reads everything it shows
from MQTT, so the server has no route to add.
"""

from pathlib import Path

from aiohttp import web

from wactorz.ext import Panel


def setup(_app: web.Application) -> None:
    """Nothing to set up: the tab reads the broker, through the page."""


def panel(_app: web.Application) -> Panel:
    return Panel(
        title="Air",
        script=Path(__file__).with_name("panel.js"),
        icon="grid",
        topics=("sensors/air/#", "air/#"),
    )
