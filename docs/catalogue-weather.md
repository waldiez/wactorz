# Weather agent

`weather-agent` — natural-language weather: current conditions, forecast, and history via
[Open-Meteo](https://open-meteo.com/). **No API key required.**

## Spawn

```text
@catalog spawn weather-agent
```

## Usage

```text
what's the weather in Athens?
5-day forecast for Berlin
what was the weather yesterday in London?
set my default location to Athens
```

With a default location set, you can drop the place name ("what's the weather?").

## Structured operations

| Field | Meaning |
|-------|---------|
| `action` | `current`, `forecast`, `history`, `set-default` |
| `location` | city name or `lat,lon` (optional; falls back to the default) |
| `days` | forecast horizon 1–16 (forecast only, default 3) |
| `date` | ISO date or `yesterday` (history only) |

Returns `location`, `temp_c`, `feels_like_c`, `condition`, `humidity`, and `wind_kph`.
