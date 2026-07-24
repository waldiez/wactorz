#!/usr/bin/with-contenv bashio
# shellcheck disable=SC1008

bashio::log.info "Starting Wactorz addon..."

# Helper to get config with multiple fallbacks:
# 1. bashio::config (API call)
# 2. /data/options.json (Direct file read via jq)
# 3. Provided default value
get_config_safe() {
    local key="$1"
    local default="$2"
    local val=""

    # Attempt 1: Direct read from options.json (FAST & SILENT)
    if [ -f /data/options.json ]; then
        val=$(jq -r ".$key" /data/options.json 2>/dev/null)
    fi

    # Attempt 2: Fallback to bashio if not in file
    if [ -z "$val" ] || [ "$val" == "null" ]; then
        if bashio::config.has_value "$key" 2>/dev/null; then
            val=$(bashio::config "$key" 2>/dev/null)
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
LLM_PROVIDER=$(get_config_safe 'llm_provider' 'anthropic')
export LLM_PROVIDER="${LLM_PROVIDER}"
LLM_MODEL=$(get_config_safe 'llm_model' 'claude-sonnet-4-6')
export LLM_MODEL="${LLM_MODEL}"
LLM_API_KEY=$(get_config_safe 'llm_api_key' '')
export LLM_API_KEY="${LLM_API_KEY}"
OLLAMA_URL=$(get_config_safe 'ollama_url' 'http://localhost:11434')
export OLLAMA_URL="${OLLAMA_URL}"
OPENAI_URL=$(get_config_safe 'openai_url' '')
export OPENAI_URL="${OPENAI_URL}"
LLM_COST_LIMIT_USD=$(get_config_safe 'llm_cost_limit_usd' '0')
export LLM_COST_LIMIT_USD="${LLM_COST_LIMIT_USD}"
LLM_COST_LIMIT_PERIOD=$(get_config_safe 'llm_cost_limit_period' 'monthly')
export LLM_COST_LIMIT_PERIOD="${LLM_COST_LIMIT_PERIOD}"

# Map generic LLM_API_KEY to provider-specific env vars expected by the Python app
case "$LLM_PROVIDER" in
    nim)      export NIM_API_KEY="$LLM_API_KEY" ;;
    openai)   export OPENAI_API_KEY="$LLM_API_KEY" ;;
    anthropic) export ANTHROPIC_API_KEY="$LLM_API_KEY" ;;
    gemini)   export GEMINI_API_KEY="$LLM_API_KEY" ;;
esac

# MQTT Config (CRITICAL: ensure never empty)
MQTT_HOST=$(get_config_safe 'mqtt_host' 'core-mosquitto')
export MQTT_HOST="${MQTT_HOST}"
MQTT_PORT=$(get_config_safe 'mqtt_port' '1883')
export MQTT_PORT="${MQTT_PORT}"
MQTT_WS_PORT=$(get_config_safe 'mqtt_ws_port' '8083')
export MQTT_WS_PORT="${MQTT_WS_PORT}"
# Optional broker credentials (blank = anonymous). Needed for the official
# Mosquitto addon and any broker with allow_anonymous false.
MQTT_USERNAME=$(get_config_safe 'mqtt_username' '')
export MQTT_USERNAME="${MQTT_USERNAME}"
MQTT_PASSWORD=$(get_config_safe 'mqtt_password' '')
export MQTT_PASSWORD="${MQTT_PASSWORD}"

# Home Assistant Config
HA_URL=$(get_config_safe 'ha_url' '')
HA_TOKEN=$(get_config_safe 'ha_token' '')
# Must match the ha_url default in config.yaml:
HA_DEFAULT_URL="http://homeassistant.local:8123"

# Exactly two combinations are valid:
#   supervisor | http://supervisor/core | injected SUPERVISOR_TOKEN
#   custom     | your URL/IP (:8123)    | your long-lived token
# The Supervisor token ONLY authenticates against http://supervisor/core — a
# normal HA URL (:8123) rejects it.
# ha_connection: auto (default) infers the mode from token presence, exactly as
# before this option existed; supervisor/custom are explicit escape hatches.
HA_MODE=$(get_config_safe 'ha_connection' 'auto')
if [ "$HA_MODE" == "auto" ]; then
    if [ -z "$HA_TOKEN" ] || [ "$HA_TOKEN" == "null" ]; then
        HA_MODE="supervisor"
    else
        HA_MODE="custom"
    fi
fi

if [ "$HA_MODE" == "supervisor" ]; then
    # Supervisor mode MUST use the proxy URL + injected token; a custom ha_url
    # or ha_token is ignored (loudly, not silently).
    if [ -n "$HA_URL" ] && [ "$HA_URL" != "$HA_DEFAULT_URL" ]; then
        bashio::log.warning "ha_url='${HA_URL}' is IGNORED: mode=supervisor uses the Supervisor proxy (http://supervisor/core), which only accepts the Supervisor token. To use your own URL, set ha_connection: custom and a long-lived ha_token."
    fi
    if [ -n "$HA_TOKEN" ] && [ "$HA_TOKEN" != "null" ]; then
        bashio::log.warning "ha_token is IGNORED: mode=supervisor uses the injected Supervisor token."
    fi
    export HA_URL="http://supervisor/core"
    export HA_TOKEN="${SUPERVISOR_TOKEN:-}"
    bashio::log.info "Using internal Supervisor proxy for Home Assistant connection."
else
    # Custom mode: the user's URL (or the standard fallback) + their long-lived token.
    export HA_URL="${HA_URL:-$HA_DEFAULT_URL}"
    if [ -z "$HA_TOKEN" ] || [ "$HA_TOKEN" == "null" ]; then
        bashio::log.warning "ha_connection=custom but ha_token is empty — HA calls will fail auth. Set a long-lived token, or use ha_connection: auto / supervisor."
        export HA_TOKEN=""
    else
        export HA_TOKEN="$HA_TOKEN"
    fi
    if [ "$HA_URL" == "http://supervisor/core" ] || [ "$HA_URL" == "http://supervisor/core/" ]; then
        bashio::log.warning "ha_url points at the Supervisor proxy, which only accepts the injected Supervisor token — it will not work with mode=custom. Use ha_connection: supervisor (or auto with an empty ha_token)."
    fi
    bashio::log.info "Using custom Home Assistant URL: ${HA_URL}"
fi
export HOME_ASSISTANT_URL="$HA_URL"
export HOME_ASSISTANT_TOKEN="$HA_TOKEN"

# Startup auth probe: one deterministic line stating mode + URL + auth result.
# Non-fatal — HA may still be booting, and the agents already no-op on bad config.
ha_probe=$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
    -H "Authorization: Bearer ${HA_TOKEN}" "${HA_URL}/api/" 2>/dev/null || echo 000)
case "$ha_probe" in
    200)     bashio::log.info "HA connection OK — mode=${HA_MODE} url=${HA_URL} (auth 200)";;
    401|403) bashio::log.warning "HA auth FAILED (${ha_probe}) — mode=${HA_MODE} url=${HA_URL}. Check ha_token: long-lived token for a custom URL, empty for supervisor mode.";;
    000)     bashio::log.warning "HA unreachable — mode=${HA_MODE} url=${HA_URL} (HA may still be booting; wactorz keeps retrying).";;
    *)       bashio::log.warning "HA probe returned ${ha_probe} — mode=${HA_MODE} url=${HA_URL}.";;
esac

# Other Integrations
API_KEY=$(get_config_safe 'api_key' '')
export API_KEY="${API_KEY}"

DISCORD_BOT_TOKEN=$(get_config_safe 'discord_bot_token' '')
export DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN}"
TELEGRAM_BOT_TOKEN=$(get_config_safe 'telegram_bot_token' '')
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"
TELEGRAM_ALLOWED_USER_ID=$(get_config_safe 'telegram_allowed_user_id' '0')
export TELEGRAM_ALLOWED_USER_ID="${TELEGRAM_ALLOWED_USER_ID}"

OTEL_ENDPOINT=$(get_config_safe 'otel_endpoint' '')
if [ -n "$OTEL_ENDPOINT" ]; then
    export OTEL_EXPORTER_OTLP_ENDPOINT="$OTEL_ENDPOINT"
    OTEL_SERVICE_NAME=$(get_config_safe 'otel_service_name' 'wactorz')
    export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME}"
fi

INFLUX_URL=$(get_config_safe 'influx_url' '')
if [ -n "$INFLUX_URL" ]; then
    export INFLUX_URL="$INFLUX_URL"
    INFLUX_TOKEN=$(get_config_safe 'influx_token' '')
    export INFLUX_TOKEN="${INFLUX_TOKEN}"
    INFLUX_ORG=$(get_config_safe 'influx_org' 'wactorz')
    export INFLUX_ORG="${INFLUX_ORG}"
    INFLUX_BUCKET=$(get_config_safe 'influx_bucket' 'wactorz')
    export INFLUX_BUCKET="${INFLUX_BUCKET}"
fi

# Embedded services
MOSQUITTO_EMBEDDED=$(get_config_safe 'mosquitto_embedded' 'false')

# Application Settings
export INTERFACE=rest
export PORT=8000

# Durable state directory. /data is addon-private and survives addon updates,
# so chat history / pickle / SQLite state is not lost on rebuild. Pinned here
# as an absolute path instead of relying on CWD.
export WACTORZ_STATE_DIR=/data/state
mkdir -p "$WACTORZ_STATE_DIR"

# Logging
if [ -n "$HA_TOKEN" ]; then ha_token_state="set"; else ha_token_state="empty"; fi
bashio::log.info "Configured: mqtt_host='${MQTT_HOST}' mqtt_port='${MQTT_PORT}' ha_url='${HA_URL}' ha_token=${ha_token_state}"

# Final safety check: if LLM provider is missing, default it here
if [ -z "$LLM_PROVIDER" ] || [ "$LLM_PROVIDER" == "null" ]; then
    export LLM_PROVIDER="anthropic"
fi

# ── Embedded Mosquitto ────────────────────────────────────────────────────────
if [ "$MOSQUITTO_EMBEDDED" = "true" ]; then
    bashio::log.info "Starting embedded Mosquitto MQTT broker..."

    # Persist retained messages under /data so the live overview (agents grid,
    # cost, state) survives addon restarts AND updates. /data is addon-private
    # and preserved across image rebuilds.
    mkdir -p /data/mosquitto

    cat > /tmp/mosquitto.conf << 'MQTTEOF'
# Stay as root so the broker can write the persistence DB under /data
# (started as root, mosquitto otherwise drops to the unprivileged
# "mosquitto" user, which cannot write the root-owned /data/mosquitto).
user root

# TCP listener
listener 1883
allow_anonymous true

# WebSocket listener (used by the wactorz frontend via /mqtt proxy)
listener 8083
protocol websockets
allow_anonymous true

persistence true
persistence_location /data/mosquitto/
autosave_interval 30
MQTTEOF

    mosquitto -c /tmp/mosquitto.conf &

    # Override wactorz MQTT config to use the local broker (anonymous)
    export MQTT_HOST="localhost"
    export MQTT_PORT="1883"
    export MQTT_WS_PORT="8083"
    export MQTT_USERNAME=""
    export MQTT_PASSWORD=""

    # Wait until Mosquitto is accepting connections (up to 15 s)
    i=0
    while [ $i -lt 15 ]; do
        if mosquitto_pub -h localhost -p 1883 -t "wactorz/probe" -m "" -q 0 2>/dev/null; then
            break
        fi
        sleep 1
        i=$((i+1))
    done
    bashio::log.info "Embedded Mosquitto ready on 1883/8083"
fi

# ── External broker readiness (non-embedded) ─────────────────────────────────
# The embedded paths above already wait for their local broker. When pointing at
# an EXTERNAL broker there was no wait, so wactorz could launch into a broker
# that wasn't reachable yet (or rejected its anonymous connect) and stall agent
# startup — which left the addon serving a blank page on boot. Probe briefly so
# agents connect cleanly. Bounded: we proceed regardless (wactorz retries MQTT).
if [ "$MOSQUITTO_EMBEDDED" != "true" ]; then
    mqtt_auth=()
    if [ -n "${MQTT_USERNAME:-}" ]; then
        mqtt_auth=(-u "$MQTT_USERNAME" -P "$MQTT_PASSWORD")
    fi
    bashio::log.info "Waiting for MQTT broker at ${MQTT_HOST}:${MQTT_PORT} (up to 15s)..."
    i=0
    while [ $i -lt 15 ]; do
        if mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" "${mqtt_auth[@]}" \
               -t "wactorz/probe" -m "" -q 0 2>/dev/null; then
            bashio::log.info "MQTT broker reachable at ${MQTT_HOST}:${MQTT_PORT}"
            break
        fi
        sleep 1
        i=$((i+1))
    done
    if [ $i -ge 15 ]; then
        bashio::log.warning "MQTT broker ${MQTT_HOST}:${MQTT_PORT} not reachable after 15s — starting anyway (wactorz keeps retrying; the UI is up regardless)."
    fi
fi

if [ -d /data ]; then
    cd /data || exit 1
fi
exec wactorz
