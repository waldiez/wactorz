"""
WeatherAgent - current conditions and short-term forecast via Open-Meteo.

Open-Meteo requires no API key and is free for non-commercial use. Geocoding
goes through their free geocoding endpoint. Picked specifically because it
matches Wactorz's offline-capable / edge-first philosophy: no signup, no
billing, no rate limit hassles for typical home use.

Commands (prefix @weather-agent stripped automatically):
  current [location]              current conditions
  forecast [location] [days=3]    forecast (1-7 days)
  set-default <location>          remember a default location

If no location is supplied, falls back to WEATHER_DEFAULT_LOCATION env var
(default: London) or the value set via 'set-default'.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiohttp

from ..config import CONFIG
from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT      = 15

# WMO weather interpretation codes - condensed for chat output
# https://open-meteo.com/en/docs#weathervariables
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


class WeatherAgent(Actor):
    """Open-Meteo weather lookup. No API key required."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "weather-agent")
        super().__init__(**kwargs)
        self._default_location = CONFIG.weather_default_location or "London"
        self._geo_cache: dict[str, tuple[float, float, str]] = {}

    async def on_start(self):
        stored = await self.recall("default_location") if hasattr(self, "recall") else None
        if stored:
            self._default_location = stored
        await self.publish_manifest(
            description="Current weather and short-term forecast via Open-Meteo. No API key.",
            capabilities=["weather.current", "weather.forecast"],
            input_schema={
                "action": "current | forecast | set-default",
                "location": "str - city or 'lat,lon' (optional, uses default)",
                "days":     "int - forecast horizon in days (1-7, default 3)",
            },
            output_schema={
                "location": "str", "temp_c": "float", "feels_like_c": "float",
                "condition": "str", "humidity": "int", "wind_kph": "float",
            },
        )
        logger.info(f"[{self.name}] Ready. Default location: {self._default_location}")

    # ── Chat entry point ──────────────────────────────────────────────────

    async def chat(self, message: str) -> str:
        payload = self._parse(message)
        result = await self._dispatch(payload)
        return self._format(result)

    def _parse(self, message: str) -> dict:
        text = message.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        parts = text.split(maxsplit=1)
        if not parts:
            return {"action": "current"}
        verb = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if verb in {"current", "now", "weather"}:
            return {"action": "current", "location": rest or None}
        if verb in {"forecast", "fc"}:
            days = 3
            location = rest or None
            if rest:
                rparts = rest.rsplit(maxsplit=1)
                if len(rparts) == 2 and rparts[1].isdigit():
                    days = max(1, min(7, int(rparts[1])))
                    location = rparts[0]
            return {"action": "forecast", "location": location, "days": days}
        if verb in {"set-default", "default"}:
            return {"action": "set-default", "location": rest}
        # default - treat the whole message as a location for current weather
        return {"action": "current", "location": text or None}

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload if isinstance(msg.payload, dict) else self._parse(str(msg.payload))
        result = await self._dispatch(payload)
        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    async def _dispatch(self, payload: dict) -> dict:
        action = (payload.get("action") or "current").lower()
        location = payload.get("location") or self._default_location

        if action == "set-default":
            if not location:
                return {"error": "Provide a location"}
            self._default_location = location
            try:
                await self.persist("default_location", location)
            except Exception:
                pass
            return {"status": "ok", "default_location": location}

        if action == "current":
            return await self._current(location)
        if action == "forecast":
            days = int(payload.get("days") or 3)
            return await self._forecast(location, max(1, min(7, days)))

        return {"error": f"Unknown action: {action}", "supported": ["current", "forecast", "set-default"]}

    # ── Open-Meteo calls ──────────────────────────────────────────────────

    async def _geocode(self, location: str) -> Optional[tuple[float, float, str]]:
        if not location:
            return None
        key = location.strip().lower()
        if key in self._geo_cache:
            return self._geo_cache[key]

        # Allow "lat,lon" literal
        if "," in location:
            try:
                lat_s, lon_s = location.split(",", 1)
                lat, lon = float(lat_s.strip()), float(lon_s.strip())
                resolved = (lat, lon, f"{lat:.3f},{lon:.3f}")
                self._geo_cache[key] = resolved
                return resolved
            except ValueError:
                pass

        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_GEOCODE_URL, params={"name": location, "count": 1}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        results = data.get("results") or []
        if not results:
            return None
        first = results[0]
        label = ", ".join(p for p in [first.get("name"), first.get("admin1"), first.get("country")] if p)
        resolved = (float(first["latitude"]), float(first["longitude"]), label)
        self._geo_cache[key] = resolved
        return resolved

    async def _current(self, location: str) -> dict:
        geo = await self._geocode(location)
        if not geo:
            return {"error": f"Could not geocode location: {location}"}
        lat, lon, label = geo
        params = {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "wind_speed_unit": "kmh",
        }
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_FORECAST_URL, params=params) as resp:
                if resp.status != 200:
                    return {"error": f"Open-Meteo HTTP {resp.status}"}
                data = await resp.json()
        cur = data.get("current") or {}
        code = int(cur.get("weather_code", -1))
        return {
            "location":     label,
            "temp_c":       cur.get("temperature_2m"),
            "feels_like_c": cur.get("apparent_temperature"),
            "humidity":     cur.get("relative_humidity_2m"),
            "wind_kph":     cur.get("wind_speed_10m"),
            "condition":    _WMO.get(code, f"wmo:{code}"),
            "observed_at":  cur.get("time"),
        }

    async def _forecast(self, location: str, days: int) -> dict:
        geo = await self._geocode(location)
        if not geo:
            return {"error": f"Could not geocode location: {location}"}
        lat, lon, label = geo
        params = {
            "latitude": lat, "longitude": lon, "forecast_days": days,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum",
            "timezone": "auto",
        }
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_FORECAST_URL, params=params) as resp:
                if resp.status != 200:
                    return {"error": f"Open-Meteo HTTP {resp.status}"}
                data = await resp.json()
        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        rows = []
        for i, date in enumerate(dates):
            code = int((daily.get("weather_code") or [-1])[i])
            rows.append({
                "date":          date,
                "temp_max_c":    (daily.get("temperature_2m_max") or [None])[i],
                "temp_min_c":    (daily.get("temperature_2m_min") or [None])[i],
                "precip_mm":     (daily.get("precipitation_sum") or [None])[i],
                "condition":     _WMO.get(code, f"wmo:{code}"),
            })
        return {"location": label, "days": days, "forecast": rows}

    # ── Formatting ────────────────────────────────────────────────────────

    def _format(self, result: dict) -> str:
        if "error" in result:
            return f"[error] {result['error']}"
        if "forecast" in result:
            lines = [f"Forecast for {result['location']} ({result['days']}d):"]
            for r in result["forecast"]:
                lines.append(
                    f"  {r['date']}  {r['condition']:<22} "
                    f"{r['temp_min_c']}-{r['temp_max_c']}C  "
                    f"precip {r['precip_mm']}mm"
                )
            return "\n".join(lines)
        if "temp_c" in result:
            return (
                f"{result['location']}: {result['condition']}, "
                f"{result['temp_c']}C (feels {result['feels_like_c']}C), "
                f"humidity {result['humidity']}%, wind {result['wind_kph']} kph"
            )
        if result.get("status") == "ok":
            return f"Default location set to: {result['default_location']}"
        return str(result)
