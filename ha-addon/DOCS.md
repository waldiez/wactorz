# Wactorz — Home Assistant Addon

Actor-model multi-agent AI framework. Spawn, coordinate, and monitor AI agents that can read and control your Home Assistant.

## Setup

1. **Install the addon** and start it.
2. Open the Web UI (port 8000) from the addon page.
3. Configure your LLM key under **Options** (see below).

## Options

| Option | Default | Description |
|---|---|---|
| `api_key` | *(blank)* | Shared secret for the Wactorz REST API. Leave blank to disable auth. |
| `llm_provider` | `anthropic` | LLM backend: `anthropic`, `openai`, `google`, `ollama`, `nim`, `nvidia` |
| `llm_model` | `claude-sonnet-4-6` | Model name for the chosen provider |
| `llm_api_key` | *(blank)* | API key for the chosen provider |
| `ollama_url` | `http://localhost:11434` | Ollama base URL (only used when `llm_provider: ollama`) |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname — use `core-mosquitto` for the official Mosquitto addon |
| `mqtt_port` | `1883` | MQTT broker port |
| `ha_url` | `http://homeassistant:8123` | Home Assistant base URL seen from inside the addon container |
| `ha_token` | *(blank)* | Long-lived access token (HA → Profile → Security → Long-Lived Access Tokens) |
| `fuseki_url` | `http://localhost:3030` | Apache Jena Fuseki SPARQL endpoint (optional) |
| `fuseki_dataset` | `wactorz` | Fuseki dataset name |
| `fuseki_user` | `admin` | Fuseki username |
| `fuseki_password` | `admin` | Fuseki password |
| `discord_bot_token` | *(blank)* | Discord bot token (optional) |
| `telegram_bot_token` | *(blank)* | Telegram bot token (optional) |
| `telegram_allowed_user_id` | `0` | Telegram user ID allowed to send commands (0 = disabled) |

## MQTT

If you have the **Mosquitto** addon installed, set `mqtt_host` to `core-mosquitto` and leave the port as `1883`. No additional broker configuration is needed.

## Home Assistant integration

Set `ha_url` to `http://homeassistant:8123` (the default) and generate a long-lived access token in HA → Profile → Security → Long-Lived Access Tokens, then paste it into `ha_token`.

## Support

- Documentation: https://waldiez.github.io/wactorz/
- Issues: https://github.com/waldiez/wactorz/issues
