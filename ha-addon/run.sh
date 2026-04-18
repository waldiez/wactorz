#!/usr/bin/env bashio

bashio::log.info "Starting Wactorz addon..."

OPTIONS_FILE="${OPTIONS_PATH:-/data/options.json}"

get_option() {
  local key="$1"
  local default_value="${2:-}"
  local value

  value="$(python3 - "$OPTIONS_FILE" "$key" "$default_value" <<'PY'
import json
import sys
from pathlib import Path

options_file, key, default = sys.argv[1:4]

try:
    data = json.loads(Path(options_file).read_text())
except Exception:
    print(default)
    raise SystemExit(0)

value = data.get(key, default)
if value is None:
    value = default
print(value)
PY
)"

  printf '%s' "$value"
}

# Map addon options to environment variables from the local options file.
export API_KEY="$(get_option 'api_key')"
export LLM_PROVIDER="$(get_option 'llm_provider' 'anthropic')"
export LLM_MODEL="$(get_option 'llm_model' 'claude-sonnet-4-6')"
export LLM_API_KEY="$(get_option 'llm_api_key')"
export OLLAMA_URL="$(get_option 'ollama_url' 'http://localhost:11434')"
export MQTT_HOST="$(get_option 'mqtt_host' 'core-mosquitto')"
MQTT_PORT="$(get_option 'mqtt_port' '1883')"
export MQTT_PORT="${MQTT_PORT:-1883}"
export HA_URL="$(get_option 'ha_url' 'http://homeassistant:8123')"
export HA_TOKEN="$(get_option 'ha_token')"
export HOME_ASSISTANT_URL="$HA_URL"
export HOME_ASSISTANT_TOKEN="$HA_TOKEN"
export FUSEKI_URL="$(get_option 'fuseki_url' 'http://localhost:3030')"
export FUSEKI_DATASET="$(get_option 'fuseki_dataset' 'wactorz')"
export FUSEKI_USER="$(get_option 'fuseki_user' 'admin')"
export FUSEKI_PASSWORD="$(get_option 'fuseki_password' 'admin')"
export DISCORD_BOT_TOKEN="$(get_option 'discord_bot_token')"
export TELEGRAM_BOT_TOKEN="$(get_option 'telegram_bot_token')"
export TELEGRAM_ALLOWED_USER_ID="$(get_option 'telegram_allowed_user_id' '0')"

export INTERFACE=rest
export PORT=8000

if [ -n "$HA_TOKEN" ]; then
  ha_token_state="set"
else
  ha_token_state="empty"
fi

bashio::log.info "Resolved config: mqtt_host='${MQTT_HOST}' mqtt_port='${MQTT_PORT}' ha_url='${HA_URL}' ha_token=${ha_token_state} llm_provider='${LLM_PROVIDER}'"

exec wactorz
