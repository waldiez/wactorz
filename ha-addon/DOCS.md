# Wactorz — Home Assistant Addon

Actor-model multi-agent AI framework. Spawn, coordinate, and monitor AI agents that can read and control your Home Assistant.

> **Requires Home Assistant OS or Supervised.**
> The Supervisor (which runs addons) is not available on Home Assistant Container or Core installs.
> If you are running Home Assistant in Docker, use the [Docker deployment](https://hub.docker.com/r/waldiez/wactorz) instead.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Click **⋮ (menu) → Repositories** and add `https://github.com/waldiez/wactorz`, then click **Add**.
3. Find **Wactorz** in the store and click **Install**.
4. Start the addon and open the Web UI from the addon page.
5. Configure your LLM key under **Options** (see below).

## Options

| Option | Default | Description |
|---|---|---|
| `api_key` | *(blank)* | Shared secret for the Wactorz REST API. Leave blank to disable auth. |
| `llm_provider` | `anthropic` | LLM backend: `anthropic`, `openai`, `gemini`, `ollama`, `nim` |
| `llm_model` | `claude-sonnet-4-6` | Model name for the chosen provider |
| `llm_api_key` | *(blank)* | API key for the chosen provider |
| `llm_cost_limit_usd` | `0` | Spend cap in USD. `0` disables enforcement; resets automatically each period. |
| `llm_cost_limit_period` | `monthly` | Period for the spend cap: `daily`, `weekly`, or `monthly`. |
| `ollama_url` | `http://localhost:11434` | Ollama base URL (only used when `llm_provider: ollama`) |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname — use `core-mosquitto` for the official Mosquitto addon |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_ws_port` | `8083` | MQTT WebSocket broker port |
| `mosquitto_embedded` | `false` | Start a bundled Mosquitto broker inside the addon (no external addon needed) |
| `ha_url` | `http://homeassistant.local:8123` | Home Assistant base URL seen from inside the addon container |
| `ha_token` | *(blank)* | Long-lived access token (HA → Profile → Security → Long-Lived Access Tokens) |
| `fuseki_url` | `http://localhost:3030` | Apache Jena Fuseki SPARQL endpoint (leave at default when `fuseki_embedded: true`) |
| `fuseki_dataset` | `wactorz` | Fuseki dataset name |
| `fuseki_user` | `admin` | Fuseki username |
| `fuseki_password` | `admin` | Fuseki password |
| `fuseki_embedded` | `false` | Start a bundled Apache Jena Fuseki inside the addon (no external service needed) |
| `discord_bot_token` | *(blank)* | Discord bot token (optional) |
| `telegram_bot_token` | *(blank)* | Telegram bot token (optional) |
| `telegram_allowed_user_id` | `0` | Telegram user ID allowed to send commands (0 = disabled) |
| `otel_endpoint` | *(blank)* | OTLP HTTP collector URL (e.g. `http://192.168.1.10:4318`). Leave blank to disable OpenTelemetry. |
| `otel_service_name` | `wactorz` | Service name reported to the OTLP collector. |
| `influx_url` | *(blank)* | InfluxDB 2.x base URL (e.g. `http://homeassistant:8086`). Leave blank to disable. `wactorz[influx]` is installed automatically when set. |
| `influx_token` | *(blank)* | InfluxDB API token. |
| `influx_org` | `wactorz` | InfluxDB organisation name. |
| `influx_bucket` | `wactorz` | InfluxDB bucket name. |

## MQTT

**Option A — use the official Mosquitto addon:**
Install the [Mosquitto broker addon](https://github.com/home-assistant/addons/tree/master/mosquitto), leave `mqtt_host` as `core-mosquitto` and `mqtt_port` as `1883`.

**Option B — embedded broker (no extra addon):**
Set `mosquitto_embedded: true`. Wactorz starts its own Mosquitto instance inside the container. Change `mqtt_host` to `localhost`. MQTT data is persisted to `/share/mosquitto`.

## Embedded services

Setting `mosquitto_embedded` or `fuseki_embedded` to `true` bundles those services inside the Wactorz container — no separate addons required.

| Option | Port | Data path |
|---|---|---|
| `mosquitto_embedded: true` | `1883` TCP (exposed as addon port) | `/share/mosquitto` |
| `fuseki_embedded: true` | `3030` (exposed as addon port) | `/share/fuseki` |

> Fuseki auth credentials are regenerated from `fuseki_user` / `fuseki_password` on every boot, so credential changes take effect automatically.

## Home Assistant integration

Set `ha_url` to `http://homeassistant.local:8123` (the default) and generate a long-lived access token in HA → Profile → Security → Long-Lived Access Tokens, then paste it into `ha_token`.

## Support

- Documentation: https://docs.waldiez.io/wactorz/
- Issues: https://github.com/waldiez/wactorz/issues
