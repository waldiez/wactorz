# SPDX-License-Identifier: Apache-2.0
"""The Air tab: a card per room, drawn from what the agents publish.

An extension whose only part is a panel: the page reads everything it shows
from MQTT, so the server has no route to add. Its icon is the package's own
file, drawn on the dashboard's grid. Its one setting, the unit temperatures
read in, is each browser's own choice, in the dashboard's Settings; the readings
stay in °C whatever is chosen.
"""

from pathlib import Path

from aiohttp import web

from wactorz.ext import Panel, PanelSetting

HERE = Path(__file__).parent


def setup(_app: web.Application) -> None:
    """Nothing to set up: the tab reads the broker, through the page."""


def panel(_app: web.Application) -> Panel:
    return Panel(
        title="Air",
        script=HERE / "panel.js",
        icon=HERE / "icon.svg",
        topics=("sensors/air/#", "air/#"),
        settings=(
            PanelSetting(
                "temperature",
                "Temperature",
                kind="choice",
                choices=(("c", "°C"), ("f", "°F")),
                default="c",
                hint="The unit the rooms' temperatures read in.",
            ),
        ),
    )
