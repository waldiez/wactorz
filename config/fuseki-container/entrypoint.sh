#!/bin/sh
# Fuseki entrypoint
#
# Responsibilities:
#   1. Write shiro.ini with ADMIN_USER / ADMIN_PASSWORD
#   2. Create persistent TDB2 datasets declared via FUSEKI_DATASET_n env vars
#   3. Exec the server (CMD or compose `command:` override)

set -e

FUSEKI_HOME="${FUSEKI_HOME:-/jena-fuseki}"
FUSEKI_BASE="${FUSEKI_BASE:-/fuseki}"

# ── 0. Ensure writable directories (volume mounts may arrive as root:root) ───
mkdir -p "${FUSEKI_BASE}/databases" "${FUSEKI_BASE}/configuration" "${FUSEKI_BASE}/logs"
chmod -R 777 "${FUSEKI_BASE}/databases" "${FUSEKI_BASE}/configuration" "${FUSEKI_BASE}/logs" 2>/dev/null || true

# ── 1. Admin credentials ──────────────────────────────────────────────────────
SHIRO="${FUSEKI_BASE}/shiro.ini"
if [ ! -f "$SHIRO" ]; then
    # Hash the password using Fuseki's built-in pw tool
    PW_HASH=$(java ${JVM_ARGS} -cp "${FUSEKI_HOME}/fuseki-server.jar" \
        org.apache.shiro.tools.HashedCredentialsGenerator \
        "$ADMIN_PASSWORD" 2>/dev/null || echo "$ADMIN_PASSWORD")

    cat > "$SHIRO" <<EOF
[main]
ssl.enabled = false

[users]
${ADMIN_USER} = ${PW_HASH}

[roles]
admin = *

[urls]
/$/metrics = anon
/$/ping    = anon
/**        = authcBasic, roles[admin]
EOF
fi

# ── 2. Create datasets (FUSEKI_CREATE_DATASETS=true) ─────────────────────────
if [ "${FUSEKI_CREATE_DATASETS}" = "true" ]; then
    for var in FUSEKI_DATASET_1 FUSEKI_DATASET_2 FUSEKI_DATASET_3; do
        DS=$(eval "echo \${$var}")
        if [ -n "$DS" ]; then
            CONF="${FUSEKI_BASE}/configuration/${DS}.ttl"
            if [ ! -f "$CONF" ]; then
                cat > "$CONF" <<EOF
@prefix fuseki:  <http://jena.apache.org/fuseki#> .
@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix tdb2:    <http://jena.apache.org/2016/tdb#> .
@prefix ja:      <http://jena.hpl.hp.com/2005/11/Assembler#> .

<#service_${DS}>
    rdf:type              fuseki:Service ;
    rdfs:label            "${DS}" ;
    fuseki:name           "${DS}" ;
    fuseki:serviceQuery   "query", "sparql" ;
    fuseki:serviceUpdate  "update" ;
    fuseki:serviceUpload  "upload" ;
    fuseki:serviceReadGraphStore  "get" ;
    fuseki:serviceReadWriteGraphStore "data" ;
    fuseki:dataset        <#dataset_${DS}> ;
    .

<#dataset_${DS}>
    rdf:type      tdb2:DatasetTDB2 ;
    tdb2:location "${FUSEKI_BASE}/databases/${DS}" ;
    .
EOF
                echo "Created dataset config: ${DS}"
            fi
        fi
    done
fi

# ── 3. Exec server ────────────────────────────────────────────────────────────
# CMD (or compose `command:` override) is the full server invocation.
# e.g. default:  /jena-fuseki/fuseki-server
#      override: /jena-fuseki/fuseki-server --update --mem /agentflow
# Fuseki's shell wrapper picks up JVM_ARGS for extra JVM flags
export JVM_ARGS
# Also set JAVA_TOOL_OPTIONS as a fallback (picked up by the JVM directly)
export JAVA_TOOL_OPTIONS="${JVM_ARGS}"
exec "$@"
