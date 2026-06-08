#!/usr/bin/with-contenv bashio

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
export LLM_PROVIDER=$(get_config_safe 'llm_provider' 'anthropic')
export LLM_MODEL=$(get_config_safe 'llm_model' 'claude-sonnet-4-6')
export LLM_API_KEY=$(get_config_safe 'llm_api_key' '')
export OLLAMA_URL=$(get_config_safe 'ollama_url' 'http://localhost:11434')
export LLM_COST_LIMIT_USD=$(get_config_safe 'llm_cost_limit_usd' '0')
export LLM_COST_LIMIT_PERIOD=$(get_config_safe 'llm_cost_limit_period' 'monthly')

# Map generic LLM_API_KEY to provider-specific env vars expected by the Python app
case "$LLM_PROVIDER" in
    nim)      export NIM_API_KEY="$LLM_API_KEY" ;;
    openai)   export OPENAI_API_KEY="$LLM_API_KEY" ;;
    anthropic) export ANTHROPIC_API_KEY="$LLM_API_KEY" ;;
    gemini)   export GEMINI_API_KEY="$LLM_API_KEY" ;;
esac

# MQTT Config (CRITICAL: ensure never empty)
export MQTT_HOST=$(get_config_safe 'mqtt_host' 'core-mosquitto')
export MQTT_PORT=$(get_config_safe 'mqtt_port' '1883')
export MQTT_WS_PORT=$(get_config_safe 'mqtt_ws_port' '8083')

# Home Assistant Config
HA_URL=$(get_config_safe 'ha_url' '')
HA_TOKEN=$(get_config_safe 'ha_token' '')

# If no token is provided, we MUST use the supervisor proxy with the supervisor token.
# Port 8123 (the normal HA URL) will NOT accept the supervisor token.
if [ -z "$HA_TOKEN" ] || [ "$HA_TOKEN" == "null" ]; then
    export HA_URL="http://supervisor/core"
    export HA_TOKEN="${SUPERVISOR_TOKEN:-}"
    bashio::log.info "Using internal Supervisor proxy for Home Assistant connection."
else
    # User provided a custom token, so we can use their URL or fallback to the standard one.
    export HA_URL="${HA_URL:-http://homeassistant:8123}"
    export HA_TOKEN="$HA_TOKEN"
    bashio::log.info "Using custom Home Assistant URL: ${HA_URL}"
fi
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

OTEL_ENDPOINT=$(get_config_safe 'otel_endpoint' '')
if [ -n "$OTEL_ENDPOINT" ]; then
    export OTEL_EXPORTER_OTLP_ENDPOINT="$OTEL_ENDPOINT"
    export OTEL_SERVICE_NAME=$(get_config_safe 'otel_service_name' 'wactorz')
fi

INFLUX_URL=$(get_config_safe 'influx_url' '')
if [ -n "$INFLUX_URL" ]; then
    export INFLUX_URL="$INFLUX_URL"
    export INFLUX_TOKEN=$(get_config_safe 'influx_token' '')
    export INFLUX_ORG=$(get_config_safe 'influx_org' 'wactorz')
    export INFLUX_BUCKET=$(get_config_safe 'influx_bucket' 'wactorz')
fi

# Embedded services
MOSQUITTO_EMBEDDED=$(get_config_safe 'mosquitto_embedded' 'false')
FUSEKI_EMBEDDED=$(get_config_safe 'fuseki_embedded' 'false')

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

    # Override wactorz MQTT config to use the local broker
    export MQTT_HOST="localhost"
    export MQTT_PORT="1883"
    export MQTT_WS_PORT="8083"

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

# ── Embedded Fuseki ───────────────────────────────────────────────────────────
if [ "$FUSEKI_EMBEDDED" = "true" ]; then
    bashio::log.info "Starting embedded Apache Jena Fuseki on :3030 (dataset: ${FUSEKI_DATASET})..."

    export FUSEKI_HOME=/opt/fuseki
    export FUSEKI_BASE=/share/fuseki

    mkdir -p "${FUSEKI_BASE}/databases/${FUSEKI_DATASET}" \
             "${FUSEKI_BASE}/configuration" \
             "${FUSEKI_BASE}/logs"

    # Write shiro.ini every boot so credential changes take effect on restart
    cat > "${FUSEKI_BASE}/shiro.ini" << EOF
[main]
ssl.enabled = false
credentialsMatcher = org.apache.shiro.authc.credential.SimpleCredentialsMatcher
iniRealm.credentialsMatcher = \$credentialsMatcher

[users]
${FUSEKI_USER} = ${FUSEKI_PASSWORD}, admin

[roles]
admin = *

[urls]
/\$/metrics = anon
/\$/ping    = anon
/**        = authcBasic, roles[admin]
EOF

    # Write TDB2 dataset config on first boot
    CONF="${FUSEKI_BASE}/configuration/${FUSEKI_DATASET}.ttl"
    if [ ! -f "$CONF" ]; then
        cat > "$CONF" << EOF
@prefix fuseki:  <http://jena.apache.org/fuseki#> .
@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix tdb2:    <http://jena.apache.org/2016/tdb#> .
@prefix ja:      <http://jena.hpl.hp.com/2005/11/Assembler#> .

<#service_${FUSEKI_DATASET}>
    rdf:type              fuseki:Service ;
    rdfs:label            "${FUSEKI_DATASET}" ;
    fuseki:name           "${FUSEKI_DATASET}" ;
    fuseki:serviceQuery   "query", "sparql" ;
    fuseki:serviceUpdate  "update" ;
    fuseki:serviceUpload  "upload" ;
    fuseki:serviceReadGraphStore  "get" ;
    fuseki:serviceReadWriteGraphStore "data" ;
    fuseki:dataset        <#dataset_${FUSEKI_DATASET}> ;
    .

<#dataset_${FUSEKI_DATASET}>
    rdf:type      tdb2:DatasetTDB2 ;
    tdb2:location "${FUSEKI_BASE}/databases/${FUSEKI_DATASET}" ;
    .
EOF
    fi

    # Let Fuseki find java via PATH (apk puts it at /usr/bin/java)
    export JVM_ARGS="-Xmx512m"
    export JAVA_TOOL_OPTIONS="${JVM_ARGS}"

    "${FUSEKI_HOME}/fuseki-server" --port=3030 &

    # Override wactorz to use local Fuseki
    export FUSEKI_URL="http://localhost:3030"

    # Wait for Fuseki to accept requests (Java startup can take 10-15 s)
    bashio::log.info "Waiting for Fuseki to be ready..."
    i=0
    while [ $i -lt 60 ]; do
        if curl -sf "http://localhost:3030/\$/ping" > /dev/null 2>&1; then
            bashio::log.info "Fuseki ready (dataset: ${FUSEKI_DATASET}, data: ${FUSEKI_BASE}/databases/${FUSEKI_DATASET})"
            break
        fi
        sleep 1
        i=$((i+1))
    done
fi

if [ -d /data ]; then
    cd /data || exit 1
fi
exec wactorz
