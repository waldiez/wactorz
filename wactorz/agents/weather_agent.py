"""
WeatherAgent - natural-language weather via Open-Meteo (no API key).

Built for the way ordinary people actually ask:

  "what's the weather"                      → current, default location
  "weather in athens gr"                    → current, Athens
  "what's the weather tomorrow"             → tomorrow's forecast, default
  "will it rain in london tomorrow?"        → tomorrow, with a rain verdict
  "how hot will it be in rome this weekend" → Sat + Sun outlook
  "do i need an umbrella on friday"         → Friday, rain verdict
  "weather in nyc next 5 days"              → 5-day outlook
  "what was the weather yesterday"          → yesterday's history

Parsing is deterministic-first (fast, free, predictable). An LLM, when
available, is only consulted as a last resort to recover a location the
deterministic parser could not extract — its answer is still validated by
geocoding, so a bad guess can never make things worse.

Structured commands are still accepted for programmatic callers:
  current [location]
  forecast [location] [days=3]
  history [location] [date]        e.g. "yesterday", "2026-05-31"
  set-default <location>
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import aiohttp

from ..config import CONFIG
from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
_TIMEOUT      = 15
_MAX_FORECAST_DAYS = 16   # Open-Meteo free tier horizon

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

# Conditions that mean "rain" / "snow" for verdict questions.
_RAINY = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
_SNOWY = {71, 73, 75, 77, 85, 86}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

# ── Vocabulary for stripping a location out of a free-text question ──────────
# Question / filler words that are never part of a place name.
_FILLER = {
    "whats", "what's", "what", "hows", "how's", "how", "is", "are", "it", "its",
    "it's", "the", "a", "an", "please", "tell", "me", "show", "give", "us",
    "can", "you", "could", "would", "do", "does", "did", "i", "we", "need",
    "know", "hey", "hi", "hello", "ok", "okay", "so", "just", "about", "of",
    "gonna", "going", "to", "be", "will", "wont", "won't", "there", "much",
    "get", "getting", "look", "looks", "looking", "like", "out", "right",
    "currently", "today", "now", "moment", "at", "in", "for", "near", "around",
    "over", "across", "on", "this", "next", "later", "expect", "expected",
    "supposed", "predicted", "any", "some", "weather", "wether", "wheather",
    "was", "were", "ago", "fahrenheit", "celsius", "forecast", "current",
    "history", "historical", "past", "jacket", "coat", "sweater", "boots",
    "shorts", "sunglasses", "sunscreen", "wear", "location", "default",
    "good", "morning", "afternoon", "evening", "night", "thanks", "thank",
    "pls", "plz", "here", "local", "my",
    # modal/auxiliary verbs — never place names
    "should", "shall", "must", "might", "may", "let", "lets", "let's",
    # corrections / chat filler that leak through
    "im", "told", "said", "think", "thought", "say", "know", "knew",
    "tf", "wtf", "lol", "lmao", "fr", "bro", "brother", "dude", "man",
    "stunned", "speak", "spoke", "speechless",
    # informal pronouns / contractions — geocoded to obscure places without this
    "youre", "you're", "theyre", "they're", "weve", "we've", "id", "i'd",
    # conversational verbs and adjectives — not place names
    "saying", "heavy", "light", "nice", "bad", "terrible", "awful", "great",
    # sentence connectors / discourse markers
    "then", "though", "still", "yet", "anyway", "actually", "basically",
    "literally", "honestly", "exactly", "totally", "definitely", "clearly",
    # internet slang
    "uwu", "owo", "omg", "idk", "imo", "tbh", "ngl", "smh", "brb",
}
# Weather-topic words that aren't locations.
_WEATHER_WORDS = {
    "weather", "forecast", "forcast", "temperature", "temp", "temperatures",
    "climate", "conditions", "condition", "rain", "rains", "raining", "rainy",
    "snow", "snows", "snowing", "snowy", "sun", "sunny", "sunshine", "hot",
    "cold", "warm", "cool", "chilly", "freezing", "windy", "wind", "humid",
    "humidity", "storm", "stormy", "storms", "umbrella", "degrees", "degree",
    "wet", "dry", "cloudy", "clouds", "cloud", "clear", "precipitation",
    "outside", "out",
}
_NOISE = _FILLER | _WEEKDAYS.keys() | _WEATHER_WORDS

# Temporal phrases stripped from a candidate location (broad, order matters).
_STRIP_TIME = re.compile(
    r"\b("
    r"the\s+day\s+after\s+tomorrow|day\s+after\s+tomorrow|"
    r"over\s+the\s+weekend|this\s+coming\s+weekend|this\s+weekend|next\s+weekend|the\s+weekend|weekend|"
    r"this\s+week|next\s+week|the\s+week|coming\s+week|rest\s+of\s+the\s+week|later\s+this\s+week|"
    r"next\s+few\s+days|coming\s+days|few\s+days|"
    r"in\s+\d+\s+days?|\d+\s+days?(?:\s+from\s+now|\s+ahead|\s+out)?|"
    r"next\s+\d+\s+days?|"
    r"the\s+next\s+\w+|this\s+(?:morning|afternoon|evening|month)|"
    r"days?\s+ago|last\s+night|last\s+week|last\s+weekend|"
    r"tomorrow|tonight|tonite|today|yesterday|now|currently|"
    r"on\s+\w+day|\w+day|mon|tue|tues|wed|thu|thurs|fri|sat|sun"
    r")\b",
    re.IGNORECASE,
)

_PREP = re.compile(r"\b(?:in|at|for|near|around|over|across)\s+([a-zA-Z][\w\s,]*?)(?:\s*[?!.,;]|\s*$)", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Keywords that mark a message as weather-related.  If NONE of these appear
# and there is no temporal cue, the message is not a weather query.
_WEATHER_VOCAB = re.compile(
    r"\b(weather|wether|wheather|forecast|forcast|temperature|temp\b|rain|snow|"
    r"sun|sunny|cloud|wind|humid|storm|hot|cold|warm|cool|chilly|freez|"
    r"umbrella|jacket|coat|sunscreen|shorts|sunglasses|outside|degrees?|"
    r"feel.*like|feels?\s+like|uv|heatwave)\b",
    re.IGNORECASE,
)
_TEMPORAL_VOCAB = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|weekend|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|this\s+week|next\s+week|\d+\s+days?)\b",
    re.IGNORECASE,
)


# ── Pure parsing helpers (network-free, unit-testable) ───────────────────────

def _label_for(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 0 < delta < 7:
        return d.strftime("%A")
    return d.isoformat()


def _next_weekday(today: date, target: int, force_next: bool = False) -> date:
    """
    Date of weekday `target` (0=Mon). The upcoming occurrence (today counts);
    force_next ("next friday") jumps to that weekday in the *following* week.
    """
    if force_next:
        next_week_monday = today + timedelta(days=(7 - today.weekday()))
        return next_week_monday + timedelta(days=target)
    return today + timedelta(days=(target - today.weekday()) % 7)


_FULL_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _resolve_when(low: str, today: date) -> dict:
    """Decide action + date window + concern from temporal/topic keywords."""
    concern = None
    if re.search(r"\brain|umbrella|drizzl|shower|wet\b", low):
        concern = "rain"
    elif re.search(r"\bsnow\b", low):
        concern = "snow"

    units = "fahrenheit" if re.search(r"fahrenheit|°f|\bin f\b|deg f", low) else "celsius"

    out = {"action": "current", "concern": concern, "units": units}

    # ── History (past) ──────────────────────────────────────────────
    iso = _ISO_DATE.search(low)
    if iso:
        d = datetime.strptime(iso.group(1), "%Y-%m-%d").date()
        if d < today:
            return {**out, "action": "history", "date_from": d, "date_to": d}
    if re.search(r"\byesterday\b", low):
        d = today - timedelta(days=1)
        return {**out, "action": "history", "date_from": d, "date_to": d}
    m = re.search(r"\b(\d{1,2})\s+days?\s+ago\b", low)
    if m:
        d = today - timedelta(days=int(m.group(1)))
        return {**out, "action": "history", "date_from": d, "date_to": d}
    if re.search(r"\bwas\b.*\bweather\b|\bweather\b.*\bwas\b|last\s+night|last\s+week", low):
        d = today - timedelta(days=1)
        return {**out, "action": "history", "date_from": d, "date_to": d}

    # ── Forecast (future, targeted) ─────────────────────────────────
    if re.search(r"\bday\s+after\s+tomorrow\b", low):
        d = today + timedelta(days=2)
        return {**out, "action": "forecast", "date_from": d, "date_to": d}
    if re.search(r"\btomorrow\b", low):
        d = today + timedelta(days=1)
        return {**out, "action": "forecast", "date_from": d, "date_to": d}
    if re.search(r"\bweekend\b", low):
        sat = _next_weekday(today, 5)
        return {**out, "action": "forecast", "date_from": sat, "date_to": sat + timedelta(days=1)}
    if re.search(r"\bnext\s+week\b", low):
        mon = _next_weekday(today, 0, force_next=True)
        return {**out, "action": "forecast", "date_from": mon, "date_to": mon + timedelta(days=6)}
    if re.search(r"\b(this\s+week|coming\s+days|next\s+few\s+days|few\s+days|rest\s+of\s+the\s+week|later\s+this\s+week)\b", low):
        return {**out, "action": "forecast", "days": 7}
    m = re.search(r"\b(?:in\s+)?(\d{1,2})\s+days?\b", low)
    if m:
        n = max(1, min(_MAX_FORECAST_DAYS, int(m.group(1))))
        return {**out, "action": "forecast", "days": n}
    # weekday name ("on friday", "this saturday") — full names only to avoid
    # matching "sun" inside "is the sun out".
    for word, idx in _FULL_WEEKDAYS.items():
        if re.search(rf"\b{word}\b", low):
            force = bool(re.search(rf"\bnext\s+{word}\b", low))
            d = _next_weekday(today, idx, force_next=force)
            return {**out, "action": "forecast", "date_from": d, "date_to": d}

    # ── Current (explicit "now" cues, or default) ───────────────────
    if re.search(r"\b(right\s+now|currently|at\s+the\s+moment|outside|now|today|this\s+(?:morning|afternoon|evening))\b", low):
        # "today" → current conditions are the most useful answer
        return out
    # Generic forecast verbs with no day → short outlook
    if re.search(r"\b(forecast|forcast|going\s+to|gonna|will\s+it|expected|upcoming|later)\b", low):
        return {**out, "action": "forecast", "days": 3}

    return out


def _clean_location(cand: str) -> Optional[str]:
    """Strip temporal/topic/filler words, leaving a plausible place name."""
    if not cand:
        return None
    s = cand.strip()
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", s)   # drop ISO dates
    s = _STRIP_TIME.sub(" ", s)
    s = re.sub(r"[?!.;]+", " ", s)
    # token filter
    kept = []
    for tok in s.split():
        bare = tok.strip(",").lower()
        if not bare:
            continue
        if bare in _NOISE:
            continue
        kept.append(tok.strip("?!."))
    out = " ".join(kept).strip(" ,")
    out = re.sub(r"\s+", " ", out)
    # drop dangling commas
    out = re.sub(r"\s*,\s*$", "", out).strip()
    return out or None


def _extract_location(raw: str) -> Optional[str]:
    """Pull a location out of free text. Returns None → use default."""
    # Take the LAST prep phrase (e.g. "i was in uk? im in Athens" → "Athens")
    matches = list(_PREP.finditer(raw))
    if matches:
        for m in reversed(matches):
            loc = _clean_location(m.group(1))
            if loc:
                return loc
    return _clean_location(raw)


def _is_weather_query(text: str) -> bool:
    """True if the message is plausibly asking about weather."""
    return bool(_WEATHER_VOCAB.search(text) or _TEMPORAL_VOCAB.search(text))


def parse_query(raw: str, today: Optional[date] = None) -> dict:
    """Deterministic natural-language → intent payload. Pure / testable."""
    today = today or date.today()
    text = (raw or "").strip()
    if not text:
        return {"action": "current"}

    # Structured JSON passthrough
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    low = text.lower()

    # set-default / "change my default location to X"
    if re.match(r"(set[-\s]?default|change\s+(?:the\s+|my\s+)?default|my\s+default)\b", low) \
            or low.startswith("default "):
        tail = re.sub(r"^.*?default\b", "", text, flags=re.IGNORECASE)
        return {"action": "set-default", "location": _clean_location(tail)}

    # If the message has no weather vocabulary, no temporal cues, and no
    # explicit "in/at/for X" prep phrase, it's probably not a weather query.
    # BUT let through: ISO dates (history commands), bare city-name inputs
    # (single non-noise token), and set-default already handled above.
    if not _is_weather_query(text) and not _PREP.search(text):
        has_iso = bool(_ISO_DATE.search(text))
        # Is it just a city name (≥1 non-noise token, no sentence structure)?
        leftover = _clean_location(text)
        looks_like_place = bool(leftover) and len(text.split()) <= 4
        if not has_iso and not looks_like_place:
            return {"action": "not_weather", "original": text}

    when = _resolve_when(low, today)
    location = _extract_location(text)

    payload: dict = {"action": when["action"]}
    if location:
        payload["location"] = location
    if when.get("concern"):
        payload["concern"] = when["concern"]
    if when.get("units") and when["units"] != "celsius":
        payload["units"] = when["units"]

    if when["action"] == "forecast":
        if "date_from" in when:
            payload["date_from"] = when["date_from"].isoformat()
            payload["date_to"] = when["date_to"].isoformat()
        else:
            payload["days"] = when.get("days", 3)
    elif when["action"] == "history":
        payload["date"] = when["date_from"].isoformat()
    return payload


class WeatherAgent(Actor):
    """Open-Meteo weather lookup with robust natural-language parsing."""

    def __init__(self, llm_provider=None, **kwargs):
        kwargs.setdefault("name", "weather-agent")
        super().__init__(**kwargs)
        self._llm = llm_provider
        self._default_location = CONFIG.weather_default_location or "London"
        self._geo_cache: dict[str, tuple[float, float, str]] = {}

    async def on_start(self):
        stored = self.recall("default_location") if hasattr(self, "recall") else None
        if stored:
            self._default_location = stored
        await self.publish_manifest(
            description="Natural-language weather: current, forecast, and historical data via Open-Meteo. No API key.",
            capabilities=["weather.current", "weather.forecast", "weather.history"],
            input_schema={
                "action":   "current | forecast | history | set-default",
                "location": "str - city or 'lat,lon' (optional, uses default)",
                "days":     "int - forecast horizon 1-16 (forecast only, default 3)",
                "date":     "str - ISO date or 'yesterday' (history only)",
            },
            output_schema={
                "location": "str", "temp_c": "float", "feels_like_c": "float",
                "condition": "str", "humidity": "int", "wind_kph": "float",
            },
        )
        logger.info(f"[{self.name}] Ready (LLM={'yes' if self._llm else 'no'}). Default: {self._default_location}")

    # ── Entry points ──────────────────────────────────────────────────────

    async def chat(self, message: str) -> str:
        payload = await self._parse_smart(message)
        result  = await self._handle_cmd(payload)
        return self._format(result)

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return
        raw = msg.payload
        if isinstance(raw, dict):
            # Already-structured intent passthrough
            if "action" in raw:
                parsed = raw
            else:
                # Accept every key that could carry a natural-language query or location.
                # Main sends {"city": "Athens"} or {"text": "..."} or {"query": "..."};
                # all map to a free-text parse so the NL pipeline handles them uniformly.
                text = (raw.get("text") or raw.get("content") or
                        raw.get("query") or raw.get("city") or
                        raw.get("location") or "")
                parsed = await self._parse_smart(text) if text else {"action": "current"}
        else:
            parsed = await self._parse_smart(str(raw))
        result = await self._handle_cmd(parsed)
        result["result"] = self._format(result)
        if isinstance(raw, dict):
            for key in ("_task_id", "task"):
                if key in raw:
                    result[key] = raw[key]
        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    # ── Parsing ───────────────────────────────────────────────────────────

    async def _parse_smart(self, message: str) -> dict:
        """Deterministic parse first; LLM only to recover a missing location."""
        payload = parse_query(message)

        needs_loc = (
            payload.get("action") in {"current", "forecast", "history"}
            and not payload.get("location")
            and self._llm is not None
            and self._mentions_place(message)
        )
        if needs_loc:
            loc = await self._llm_location(message)
            if loc:
                payload["location"] = loc
        return payload

    @staticmethod
    def _mentions_place(message: str) -> bool:
        """Heuristic: are there leftover tokens that might be an unrecognised place?"""
        leftover = _clean_location(message)
        return bool(leftover)

    async def _llm_location(self, message: str) -> Optional[str]:
        """Ask the LLM only for a location string; validate via geocoding."""
        try:
            reply, _ = await self._llm.complete(
                [{"role": "user", "content": message}],
                system=("Extract ONLY the place/city name the user is asking about. "
                        "Reply with the bare location, nothing else. "
                        "If there is no location, reply exactly: NONE"),
            )
            loc = (reply or "").strip().strip('"').splitlines()[0].strip()
            if not loc or loc.upper() == "NONE" or len(loc) > 80:
                return None
            return loc if await self._geocode(loc) else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{self.name}] LLM location recovery failed: {e}")
            return None

    # ── Command handler ───────────────────────────────────────────────────

    async def _handle_cmd(self, payload: dict) -> dict:
        action   = (payload.get("action") or "current").lower()
        location = payload.get("location") or self._default_location
        concern  = payload.get("concern")
        units    = payload.get("units", "celsius")
        used_default = not payload.get("location")

        if action == "not_weather":
            return {"error": (
                "I'm a weather agent — I can only help with weather questions. "
                "Try: 'weather in Athens', 'will it rain tomorrow?', "
                "'should I bring a jacket in Oslo?'"
            )}

        if action == "set-default":
            loc = payload.get("location")
            if not loc:
                return {"error": "Tell me which location to set as default, e.g. 'set-default Athens'."}
            self._default_location = loc
            try:
                await self.persist("default_location", loc)
            except Exception:
                pass
            return {"status": "ok", "default_location": loc}

        if action == "current":
            res = await self._current(location, units)
        elif action == "forecast":
            if payload.get("date_from"):
                res = await self._forecast(location, units,
                                           date_from=payload["date_from"],
                                           date_to=payload.get("date_to") or payload["date_from"])
            else:
                res = await self._forecast(location, units,
                                           days=max(1, min(_MAX_FORECAST_DAYS, int(payload.get("days") or 3))))
        elif action == "history":
            date_str = payload.get("date") or (date.today() - timedelta(days=1)).isoformat()
            res = await self._history(location, date_str, units)
        else:
            return {"error": f"I didn't understand that. Try 'weather in <city>' or 'forecast <city> tomorrow'."}

        if isinstance(res, dict) and "error" not in res:
            res["concern"] = concern
            res["units"] = units
            res["used_default"] = used_default
        return res

    # ── Open-Meteo calls ──────────────────────────────────────────────────

    async def _get_json(self, url: str, params: dict) -> Optional[dict]:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"[{self.name}] {url} → HTTP {resp.status}")
                        return None
                    return await resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.name}] request to {url} failed: {e}")
            return None

    async def _geocode(self, location: str) -> Optional[tuple[float, float, str]]:
        if not location:
            return None
        key = location.strip().lower()
        if key in self._geo_cache:
            return self._geo_cache[key]
        # explicit "lat,lon"
        if "," in location:
            try:
                lat_s, lon_s = location.split(",", 1)
                lat, lon = float(lat_s.strip()), float(lon_s.strip())
                resolved = (lat, lon, f"{lat:.3f},{lon:.3f}")
                self._geo_cache[key] = resolved
                return resolved
            except ValueError:
                pass
        # try the full string, then progressively drop trailing tokens
        # ("paris france" → "paris"), so qualifier words never block a hit.
        candidates = [location]
        toks = location.split()
        if len(toks) > 1:
            candidates.append(" ".join(toks[:-1]))
            candidates.append(toks[0])
        seen = set()
        for cand in candidates:
            cand = cand.strip(" ,")
            if not cand or cand.lower() in seen:
                continue
            seen.add(cand.lower())
            data = await self._get_json(_GEOCODE_URL, {"name": cand, "count": 1})
            results = (data or {}).get("results") or []
            if results:
                first = results[0]
                label = ", ".join(p for p in [first.get("name"), first.get("admin1"),
                                              first.get("country")] if p)
                resolved = (float(first["latitude"]), float(first["longitude"]), label)
                self._geo_cache[key] = resolved
                return resolved
        return None

    @staticmethod
    def _units_params(units: str) -> dict:
        if units == "fahrenheit":
            return {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph"}
        return {"wind_speed_unit": "kmh"}

    async def _current(self, location: str, units: str = "celsius") -> dict:
        geo = await self._geocode(location)
        if not geo:
            return {"error": f"I couldn't find a place called '{location}'. Try adding a country, e.g. 'Athens, Greece'."}
        lat, lon, label = geo
        params = {
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weather_code,wind_speed_10m,precipitation",
            "timezone": "auto",
            **self._units_params(units),
        }
        data = await self._get_json(_FORECAST_URL, params)
        if not data:
            return {"error": "Weather service is unreachable right now — try again in a moment."}
        cur = data.get("current") or {}
        code = int(cur.get("weather_code", -1))
        return {
            "kind":         "current",
            "location":     label,
            "temp":         cur.get("temperature_2m"),
            "feels_like":   cur.get("apparent_temperature"),
            "humidity":     cur.get("relative_humidity_2m"),
            "wind":         cur.get("wind_speed_10m"),
            "precip":       cur.get("precipitation"),
            "code":         code,
            "condition":    _WMO.get(code, f"wmo:{code}"),
            "observed_at":  cur.get("time"),
        }

    async def _forecast(self, location: str, units: str = "celsius", *,
                        days: Optional[int] = None,
                        date_from: Optional[str] = None,
                        date_to: Optional[str] = None) -> dict:
        geo = await self._geocode(location)
        if not geo:
            return {"error": f"I couldn't find a place called '{location}'. Try adding a country, e.g. 'Athens, Greece'."}
        lat, lon, label = geo

        today = date.today()
        if date_to:
            horizon = (datetime.strptime(date_to, "%Y-%m-%d").date() - today).days + 1
            fdays = max(1, min(_MAX_FORECAST_DAYS, horizon))
        else:
            fdays = max(1, min(_MAX_FORECAST_DAYS, days or 3))

        params = {
            "latitude": lat, "longitude": lon, "forecast_days": fdays,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,"
                     "precipitation_sum,precipitation_probability_max",
            "timezone": "auto",
            **self._units_params(units),
        }
        data = await self._get_json(_FORECAST_URL, params)
        if not data:
            return {"error": "Weather service is unreachable right now — try again in a moment."}
        rows = self._daily_rows(data.get("daily") or {})
        if date_from:
            rows = [r for r in rows if date_from <= r["date"] <= (date_to or date_from)]
            if not rows:
                return {"error": f"That date is beyond the {_MAX_FORECAST_DAYS}-day forecast range."}
        return {"kind": "forecast", "location": label, "forecast": rows}

    async def _history(self, location: str, date_str: str, units: str = "celsius") -> dict:
        geo = await self._geocode(location)
        if not geo:
            return {"error": f"I couldn't find a place called '{location}'. Try adding a country, e.g. 'Athens, Greece'."}
        lat, lon, label = geo
        if date_str.lower() == "yesterday":
            date_str = (date.today() - timedelta(days=1)).isoformat()

        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        delta = (date.today() - target).days
        daily = ("temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum")
        # The archive API lags ~5 days; for recent days the forecast API's
        # past_days window is the reliable source.
        if 0 < delta <= 5:
            params = {"latitude": lat, "longitude": lon, "past_days": min(7, delta + 1),
                      "forecast_days": 1, "daily": daily, "timezone": "auto",
                      **self._units_params(units)}
            data = await self._get_json(_FORECAST_URL, params)
        else:
            params = {"latitude": lat, "longitude": lon, "start_date": date_str,
                      "end_date": date_str, "daily": daily, "timezone": "auto",
                      **self._units_params(units)}
            data = await self._get_json(_ARCHIVE_URL, params)

        if not data:
            return {"error": "Weather history is unreachable right now — try again in a moment."}
        rows = [r for r in self._daily_rows(data.get("daily") or {}) if r["date"] == date_str]
        if not rows:
            return {"error": f"No weather record found for {date_str}."}
        r = rows[0]
        return {"kind": "history", "location": label, **r}

    @staticmethod
    def _daily_rows(daily: dict) -> list[dict]:
        dates = daily.get("time") or []
        rows = []
        for i, d in enumerate(dates):
            code = int((daily.get("weather_code") or [-1])[i] or -1)
            rows.append({
                "date":       d,
                "temp_max":   _idx(daily.get("temperature_2m_max"), i),
                "temp_min":   _idx(daily.get("temperature_2m_min"), i),
                "precip_mm":  _idx(daily.get("precipitation_sum"), i),
                "precip_prob": _idx(daily.get("precipitation_probability_max"), i),
                "code":       code,
                "condition":  _WMO.get(code, f"wmo:{code}"),
            })
        return rows

    # ── Formatting ────────────────────────────────────────────────────────

    def _format(self, result: dict) -> str:
        if "error" in result:
            return f"{result['error']}"

        units = result.get("units", "celsius")
        deg = "°F" if units == "fahrenheit" else "°C"
        ws = "mph" if units == "fahrenheit" else "km/h"
        concern = result.get("concern")
        kind = result.get("kind")

        if result.get("status") == "ok":
            return f"Default location set to {result['default_location']}."

        if kind == "current":
            t, fl = _r(result["temp"]), _r(result["feels_like"])
            base = (f"In {result['location']} it's currently {result['condition']}, "
                    f"{t}{deg}")
            if fl is not None and fl != t:
                base += f" (feels like {fl}{deg})"
            base += f", humidity {_r(result['humidity'])}%, wind {_r(result['wind'])} {ws}."
            verdict = self._verdict_now(result, concern)
            suffix = (f" (using default location — say 'weather in <city>' to specify one)"
                      if result.get("used_default") else "")
            return (f"{verdict} {base}".strip() + suffix)

        if kind == "history":
            return (f"On {result['date']}, {result['location']} saw {result['condition']}, "
                    f"{_r(result['temp_min'])}–{_r(result['temp_max'])}{deg}"
                    f"{self._precip_str(result)}.")

        if kind == "forecast":
            rows = result["forecast"]
            today = date.today()
            if len(rows) == 1:
                r = rows[0]
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
                lbl = _label_for(d, today)
                lead = self._verdict_day(r, concern, result["location"], lbl)
                body = (f"{result['location']} {lbl} ({r['date']}): {r['condition']}, "
                        f"{_r(r['temp_min'])}–{_r(r['temp_max'])}{deg}"
                        f"{self._precip_str(r)}.")
                return f"{lead} {body}".strip()
            lines = [f"Forecast for {result['location']}:"]
            for r in rows:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
                lbl = _label_for(d, today)
                lines.append(
                    f"  {lbl:<9} {r['condition']:<22} "
                    f"{_r(r['temp_min'])}–{_r(r['temp_max'])}{deg}{self._precip_str(r)}"
                )
            if concern in {"rain", "snow"}:
                lines.insert(1, "  " + self._verdict_range(rows, concern))
            return "\n".join(lines)

        return str(result)

    # ── Verdict helpers (answer "will it rain?" style questions) ────────────

    @staticmethod
    def _precip_str(r: dict) -> str:
        prob = r.get("precip_prob")
        mm = r.get("precip_mm")
        bits = []
        if mm:
            bits.append(f"{_r(mm)}mm")
        if prob is not None:
            bits.append(f"{_r(prob)}% chance")
        return f", precip {' / '.join(bits)}" if bits else ""

    @staticmethod
    def _verdict_now(result: dict, concern: Optional[str]) -> str:
        if concern == "rain":
            if result.get("code") in _RAINY or (result.get("precip") or 0) > 0:
                return "Yes — it's raining."
            return "No — it's dry right now."
        if concern == "snow":
            return "Yes — it's snowing." if result.get("code") in _SNOWY else "No — no snow right now."
        return ""

    @staticmethod
    def _verdict_day(r: dict, concern: Optional[str], location: str, lbl: str) -> str:
        if concern == "rain":
            prob, mm, code = r.get("precip_prob"), r.get("precip_mm") or 0, r.get("code")
            likely = code in _RAINY or (prob is not None and prob >= 50) or mm >= 1.0
            maybe = (prob is not None and 25 <= prob < 50) or (0 < mm < 1.0)
            if likely:
                return f"Yes — pack an umbrella, rain is likely {lbl}."
            if maybe:
                return f"Maybe — there's a chance of rain {lbl}."
            return f"No — it should stay dry {lbl}."
        if concern == "snow":
            return (f"Yes — snow is expected {lbl}." if r.get("code") in _SNOWY
                    else f"No — no snow expected {lbl}.")
        return ""

    @staticmethod
    def _verdict_range(rows: list[dict], concern: Optional[str]) -> str:
        if concern == "snow":
            days = [r for r in rows if r.get("code") in _SNOWY]
            if not days:
                return "No snow expected over this period."
            return "Snow expected on: " + ", ".join(_short(r["date"]) for r in days) + "."
        # rain
        wet = [r for r in rows
               if r.get("code") in _RAINY
               or (r.get("precip_prob") or 0) >= 50
               or (r.get("precip_mm") or 0) >= 1.0]
        if not wet:
            return "Looks dry across this period — no umbrella needed."
        return "Rain likely on: " + ", ".join(_short(r["date"]) for r in wet) + "."


def _idx(arr, i):
    try:
        return arr[i]
    except (TypeError, IndexError):
        return None


def _r(v):
    """Round floats for display; pass through ints/None."""
    if isinstance(v, float):
        return round(v, 1)
    return v


def _short(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a")
    except ValueError:
        return iso
