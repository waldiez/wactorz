"""
WeatherAgent — current weather via wttr.in (no API key required).

Usage:
  @weather-agent [location]   fetch weather for location
  @weather-agent help         show usage

Environment:
  WEATHER_DEFAULT_LOCATION   default location (fallback: London)
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse

from ..config import CONFIG

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_DEFAULT_LOCATION = CONFIG.weather_default_location
_USER_AGENT       = "AgentFlow-WeatherAgent/1.0"
_TIMEOUT_SEC      = 10


def _url_encode(location: str) -> str:
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.,'"
    out = []
    for ch in location:
        if ch == " ":
            out.append("+")
        elif ch in safe:
            out.append(ch)
        else:
            out.append(urllib.parse.quote(ch, safe=""))
    return "".join(out)


class WeatherAgent(Actor):
    """Real-time weather actor using wttr.in."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "weather-agent")
        super().__init__(**kwargs)
        self.protected = False
        self._default_location = _DEFAULT_LOCATION

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "data",
                "timestamp": time.time(),
            },
        )
        logger.info(f"[{self.name}] started — default location: {self._default_location}")

    async def handle_message(self, msg: Message):
        if msg.type not in (MessageType.TASK, MessageType.RESULT):
            return

        payload = msg.payload or {}
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("content") or "")
        else:
            text = str(payload)

        for prefix in ("@weather-agent", "@weather_agent"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].lstrip()
                break

        if text.lower() == "help":
            default = self._default_location
            await self._reply(
                f"**WeatherAgent** — current conditions via wttr.in (no API key needed)\n\n"
                f"```\n"
                f"@weather-agent              # {default} (default)\n"
                f"@weather-agent Tokyo\n"
                f"@weather-agent New York\n"
                f"@weather-agent 48.8566,2.3522  # coordinates\n"
                f"```\n"
                f"Set `WEATHER_DEFAULT_LOCATION` in `.env` to change the default."
            )
            return

        location = text.strip() or self._default_location
        await self._reply(f"🌦 Fetching weather for **{location}**...")
        result = await self._fetch(location)
        await self._reply(result)
        self.metrics.tasks_completed += 1

    async def _fetch(self, location: str) -> str:
        try:
            import aiohttp
        except ImportError:
            return "Error: `aiohttp` is not installed. Cannot fetch weather."

        encoded = _url_encode(location)
        url     = f"https://wttr.in/{encoded}?format=j1"
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SEC)
        headers = {"User-Agent": _USER_AGENT}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        # Fallback to one-line format
                        url2 = f"https://wttr.in/{encoded}?format=3"
                        async with session.get(url2) as r2:
                            return (await r2.text()).strip()
                    data = await resp.json(content_type=None)
                    return self._format(data, location, encoded)
        except Exception as exc:
            logger.warning(f"[{self.name}] weather fetch error: {exc}")
            return f"⚠ Could not fetch weather for '{location}': {exc}"

    @staticmethod
    def _format(data: dict, location: str, encoded: str) -> str:
        try:
            cc   = data["current_condition"][0]
            desc = cc.get("weatherDesc", [{}])[0].get("value", "N/A")
            area = (
                data.get("nearest_area", [{}])[0]
                    .get("areaName", [{}])[0]
                    .get("value", location)
            )
            return (
                f"**Weather in {area}**\n\n"
                f"🌡 **{cc.get('temp_C', '?')}°C / {cc.get('temp_F', '?')}°F**"
                f" (feels like {cc.get('FeelsLikeC', '?')}°C)\n"
                f"☁ {desc}\n"
                f"💧 Humidity: {cc.get('humidity', '?')}%\n"
                f"💨 Wind: {cc.get('windspeedKmph', '?')} km/h {cc.get('winddir16Point', '?')}\n"
                f"👁 Visibility: {cc.get('visibility', '?')} km\n"
                f"☀ UV index: {cc.get('uvIndex', '?')}\n\n"
                f"*Data: [wttr.in](https://wttr.in/{encoded})*"
            )
        except (KeyError, IndexError):
            return f"Received data for **{location}** but could not parse it."

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )
