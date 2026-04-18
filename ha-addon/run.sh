#!/usr/bin/env bashio

bashio::log.info "Starting Wactorz addon..."

# Map addon options to environment variables
export API_KEY="$(bashio::config 'api_key')"
export LLM_PROVIDER="$(bashio::config 'llm_provider')"
export LLM_MODEL="$(bashio::config 'llm_model')"
export LLM_API_KEY="$(bashio::config 'llm_api_key')"
export OLLAMA_URL="$(bashio::config 'ollama_url')"
export MQTT_HOST="$(bashio::config 'mqtt_host')"
export MQTT_PORT="$(bashio::config 'mqtt_port')"
export HA_URL="$(bashio::config 'ha_url')"
export HA_TOKEN="$(bashio::config 'ha_token')"
export FUSEKI_URL="$(bashio::config 'fuseki_url')"
export FUSEKI_DATASET="$(bashio::config 'fuseki_dataset')"
export FUSEKI_USER="$(bashio::config 'fuseki_user')"
export FUSEKI_PASSWORD="$(bashio::config 'fuseki_password')"
export DISCORD_BOT_TOKEN="$(bashio::config 'discord_bot_token')"
export TELEGRAM_BOT_TOKEN="$(bashio::config 'telegram_bot_token')"
export TELEGRAM_ALLOWED_USER_ID="$(bashio::config 'telegram_allowed_user_id')"

export INTERFACE=rest
export PORT=8000

exec wactorz
