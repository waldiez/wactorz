#!/usr/bin/env bashio

bashio::log.info "Starting Wactorz addon..."

# Map addon options to environment variables using bashio.
# These will use the values from /data/options.json (populated by HA)
# or fallback to the defaults defined in config.yaml.

export API_KEY=$(bashio::config 'api_key')
export LLM_PROVIDER=$(bashio::config 'llm_provider')
export LLM_MODEL=$(bashio::config 'llm_model')
export LLM_API_KEY=$(bashio::config 'llm_api_key')
export OLLAMA_URL=$(bashio::config 'ollama_url')

export MQTT_HOST=$(bashio::config 'mqtt_host')
export MQTT_PORT=$(bashio::config 'mqtt_port')

# Home Assistant connection
# If the user provided explicit URL/Token, use them; 
# otherwise fallback to the supervisor-injected ones.
HA_URL=$(bashio::config 'ha_url')
HA_TOKEN=$(bashio::config 'ha_token')

if [ -z "$HA_URL" ] || [ "$HA_URL" == "null" ]; then
    export HA_URL="http://supervisor/core"
fi

if [ -z "$HA_TOKEN" ] || [ "$HA_TOKEN" == "null" ]; then
    export HA_TOKEN="${SUPERVISOR_TOKEN}"
fi

export HOME_ASSISTANT_URL="$HA_URL"
export HOME_ASSISTANT_TOKEN="$HA_TOKEN"

# Other integrations
export FUSEKI_URL=$(bashio::config 'fuseki_url')
export FUSEKI_DATASET=$(bashio::config 'fuseki_dataset')
export FUSEKI_USER=$(bashio::config 'fuseki_user')
export FUSEKI_PASSWORD=$(bashio::config 'fuseki_password')

export DISCORD_BOT_TOKEN=$(bashio::config 'discord_bot_token')
export TELEGRAM_BOT_TOKEN=$(bashio::config 'telegram_bot_token')
export TELEGRAM_ALLOWED_USER_ID=$(bashio::config 'telegram_allowed_user_id')

# Application settings
export INTERFACE=rest
export PORT=8000

# Log the configuration for debugging (masking tokens)
if [ -n "$HA_TOKEN" ]; then ha_token_state="set"; else ha_token_state="empty"; fi
if [ -n "$LLM_API_KEY" ]; then llm_key_state="set"; else llm_key_state="empty"; fi

bashio::log.info "Configured: mqtt_host='${MQTT_HOST}' mqtt_port='${MQTT_PORT}' ha_url='${HA_URL}' ha_token=${ha_token_state} llm_provider='${LLM_PROVIDER}' (key: ${llm_key_state})"

exec wactorz
