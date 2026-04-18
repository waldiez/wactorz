#!/usr/bin/env bashio

bashio::log.info "Starting Wactorz addon..."

# Helper to get config with multiple fallbacks:
# 1. bashio::config (API call)
# 2. /data/options.json (Direct file read via jq)
# 3. Provided default value
get_config_safe() {
    local key="$1"
    local default="$2"
    local val=""

    # Attempt 1: bashio (may fail with "forbidden")
    if bashio::config.has_value "$key"; then
        val=$(bashio::config "$key")
    fi

    # Attempt 2: Direct read from options.json if val is still empty/null
    if [ -z "$val" ] || [ "$val" == "null" ]; then
        if [ -f /data/options.json ]; then
            val=$(jq -r ".$key" /data/options.json 2>/dev/null)
        fi
    fi

    # Fallback to default
    if [ -z "$val" ] || [ "$val" == "null" ]; then
        echo "$default"
    else
        echo "$val"
    fi
}

# --- Export Environment Variables ---

# LLM Config
export LLM_PROVIDER=$(get_config_safe 'llm_provider' 'anthropic')
export LLM_MODEL=$(get_config_safe 'llm_model' 'claude-sonnet-4-6')
export LLM_API_KEY=$(get_config_safe 'llm_api_key' '')
export OLLAMA_URL=$(get_config_safe 'ollama_url' 'http://localhost:11434')

# MQTT Config (CRITICAL: ensure never empty)
export MQTT_HOST=$(get_config_safe 'mqtt_host' 'core-mosquitto')
export MQTT_PORT=$(get_config_safe 'mqtt_port' '1883')

# Home Assistant Config
HA_URL=$(get_config_safe 'ha_url' 'http://supervisor/core')
HA_TOKEN=$(get_config_safe 'ha_token' "${SUPERVISOR_TOKEN:-}")

# If we still don't have a token, warn the user
if [ -z "$HA_TOKEN" ] || [ "$HA_TOKEN" == "null" ]; then
    bashio::log.warning "SUPERVISOR_TOKEN is missing. Ensure 'hassio_api: true' is in config.yaml and the addon was reinstalled."
fi

export HA_URL="${HA_URL:-http://supervisor/core}"
export HA_TOKEN="${HA_TOKEN:-${SUPERVISOR_TOKEN:-}}"
export HOME_ASSISTANT_URL="$HA_URL"
export HOME_ASSISTANT_TOKEN="$HA_TOKEN"

# Other Integrations
export API_KEY=$(get_config_safe 'api_key' '')
export FUSEKI_URL=$(get_config_safe 'fuseki_url' 'http://localhost:3030')
export FUSEKI_DATASET=$(get_config_safe 'fuseki_dataset' 'wactorz')
export FUSEKI_USER=$(get_config_safe 'fuseki_user' 'admin')
export FUSEKI_PASSWORD=$(get_config_safe 'fuseki_password' 'admin')

export DISCORD_BOT_TOKEN=$(get_config_safe 'discord_bot_token' '')
export TELEGRAM_BOT_TOKEN=$(get_config_safe 'telegram_bot_token' '')
export TELEGRAM_ALLOWED_USER_ID=$(get_config_safe 'telegram_allowed_user_id' '0')

# Application Settings
export INTERFACE=rest
export PORT=8000

# Logging
if [ -n "$HA_TOKEN" ]; then ha_token_state="set"; else ha_token_state="empty"; fi
bashio::log.info "Configured: mqtt_host='${MQTT_HOST}' mqtt_port='${MQTT_PORT}' ha_url='${HA_URL}' ha_token=${ha_token_state}"

# Final safety check: if LLM provider is missing, default it here
if [ -z "$LLM_PROVIDER" ] || [ "$LLM_PROVIDER" == "null" ]; then
    export LLM_PROVIDER="anthropic"
fi

exec wactorz
