#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentFlow — FULL release packager (source + pre-built artifacts)
#
# Produces:  agentflow-full-<YYYYMMDD>.zip
#
# The zip contains BOTH:
#   1. Pre-built Vite SPA           (frontend/dist/)
#   2. Exported linux/amd64 image   (agentflow-server.tar.gz)
#   3. Full source code             (rust/, frontend/src etc.)
#
# On the SFTP/target host you can:
#   a) Quick deploy — load pre-built image, no compile needed
#   b) Build from source — `docker compose up -d --build` (requires Docker only)
#   c) Rebuild frontend — `npm ci && npm run build` then restart nginx
#
# Prerequisites (build machine):
#   • Docker + buildx with linux/amd64 QEMU support
#   • Node.js / npm
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."    # always run from repo root

DATE=$(date +%Y%m%d)
RELEASE_NAME="agentflow-full-${DATE}"
WORK_DIR="/tmp/${RELEASE_NAME}"
OUT_FILE="${RELEASE_NAME}.zip"

echo "══════════════════════════════════════════════════════"
echo " AgentFlow Full Release Packager"
echo " Output: ${OUT_FILE}"
echo "══════════════════════════════════════════════════════"

# ── 1. Build the frontend ─────────────────────────────────────────────────────
echo ""
echo "▶ Building frontend…"
cd frontend && npm run build && cd ..
echo "  ✓ frontend/dist/ ready"

# ── 2. Cross-compile Docker image for linux/amd64 ────────────────────────────
echo ""
echo "▶ Building Docker image for linux/amd64 (cross-compile via buildx)…"
echo "  ~5-8 min on Apple Silicon."
docker buildx build \
  --platform linux/amd64 \
  --tag agentflow-server:release-amd64 \
  --load \
  ./rust
echo ""
echo "▶ Exporting image…"
docker save agentflow-server:release-amd64 | gzip -9 > /tmp/agentflow-server.tar.gz
docker rmi agentflow-server:release-amd64 --force >/dev/null 2>&1 || true
echo "  ✓ Image saved ($(du -sh /tmp/agentflow-server.tar.gz | cut -f1))"

# ── 3. Stage everything ───────────────────────────────────────────────────────
echo ""
echo "▶ Assembling staging directory…"
rm -rf "${WORK_DIR}"
mkdir -p \
  "${WORK_DIR}/rust" \
  "${WORK_DIR}/frontend" \
  "${WORK_DIR}/infra/nginx" \
  "${WORK_DIR}/infra/mosquitto" \
  "${WORK_DIR}/systemd" \
  "${WORK_DIR}/scripts"

# Source trees (no build artefacts)
rsync -a --exclude='target/' \
  rust/ "${WORK_DIR}/rust/"

rsync -a \
  --exclude='node_modules/' \
  --exclude='.vite/' \
  --exclude='*.tsbuildinfo' \
  frontend/ "${WORK_DIR}/frontend/"
cp infra/nginx/nginx.conf         "${WORK_DIR}/infra/nginx/nginx.conf"
cp infra/mosquitto/mosquitto.conf "${WORK_DIR}/infra/mosquitto/mosquitto.conf"

# Scripts + env template
cp scripts/mock-agents.mjs          "${WORK_DIR}/scripts/mock-agents.mjs"
cp scripts/package-release.sh       "${WORK_DIR}/scripts/package-release.sh"
cp scripts/package-full-release.sh  "${WORK_DIR}/scripts/package-full-release.sh"
cp scripts/package-native.sh        "${WORK_DIR}/scripts/package-native.sh"
cp scripts/build-native.sh          "${WORK_DIR}/scripts/build-native.sh"
cp .env.example                     "${WORK_DIR}/.env.example"

# Native-mode infrastructure
cp compose.native.yaml                    "${WORK_DIR}/compose.native.yaml"
cp infra/nginx/nginx-native.conf          "${WORK_DIR}/infra/nginx/nginx-native.conf"
cp systemd/agentflow.service              "${WORK_DIR}/systemd/agentflow.service"

# Pre-built image
mv /tmp/agentflow-server.tar.gz "${WORK_DIR}/agentflow-server.tar.gz"

# ── 4. compose.yaml — BUILD FROM SOURCE (default, for target rebuilds) ───────
cp compose.yaml     "${WORK_DIR}/compose.yaml"
cp compose.dev.yaml "${WORK_DIR}/compose.dev.yaml"

# ── 5. compose.release.yaml — USE PRE-BUILT IMAGE (quick deploy, no compile) ─
cat > "${WORK_DIR}/compose.release.yaml" << 'COMPOSEYAML'
# AgentFlow — quick deploy (pre-built linux/amd64 image, no Rust compile)
#
# 1. docker load < agentflow-server.tar.gz
# 2. cp .env.example .env  &&  edit .env
# 3. docker compose -f compose.release.yaml up -d
#
name: agentflow

services:

  mosquitto:
    image: eclipse-mosquitto:2.0
    container_name: agentflow-mosquitto
    restart: unless-stopped
    hostname: mosquitto
    ports:
      - "${MQTT_EXTERNAL_PORT:-1883}:1883"
      - "${MQTT_WS_EXTERNAL_PORT:-9001}:9001"
    volumes:
      - ./infra/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf
      - mosquitto-data:/mosquitto/data
      - mosquitto-logs:/mosquitto/log
    networks:
      - agentflow-net
    healthcheck:
      test: ["CMD", "mosquitto_sub", "-t", "$$SYS/#", "-C", "1", "-i", "hc", "-W", "3"]
      interval: 10s
      timeout: 5s
      retries: 5

  agentflow:
    image: agentflow-server:latest
    platform: linux/amd64
    container_name: agentflow-server
    restart: unless-stopped
    ports:
      - "${API_EXTERNAL_PORT:-8080}:8080"
      - "${WS_EXTERNAL_PORT:-8081}:8081"
    environment:
      MQTT_HOST: mosquitto
      MQTT_PORT: 1883
      API_ADDR: "0.0.0.0:8080"
      WS_ADDR: "0.0.0.0:8081"
      LLM_PROVIDER: "${LLM_PROVIDER:-anthropic}"
      LLM_MODEL: "${LLM_MODEL:-claude-sonnet-4-6}"
      LLM_API_KEY: "${LLM_API_KEY}"
      RUST_LOG: "${RUST_LOG:-agentflow=info,tower_http=warn}"
      NO_CLI: "true"
    networks:
      - agentflow-net
    depends_on:
      mosquitto:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  dashboard:
    image: nginx:1.27-alpine
    container_name: agentflow-dashboard
    restart: unless-stopped
    ports:
      - "${DASHBOARD_EXTERNAL_PORT:-80}:80"
    volumes:
      - ./frontend/dist:/usr/share/nginx/html:ro
      - ./infra/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    networks:
      - agentflow-net
    depends_on:
      agentflow:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost/"]
      interval: 10s
      timeout: 3s
      retries: 3

networks:
  agentflow-net:

volumes:
  mosquitto-data:
  mosquitto-logs:
COMPOSEYAML

# ── 6. deploy.sh — interactive wizard ────────────────────────────────────────
cat > "${WORK_DIR}/deploy.sh" << 'DEPLOYSCRIPT'
#!/usr/bin/env bash
# AgentFlow — deployment wizard (source build  OR  pre-built image)
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\e[1m'; RESET=$'\e[0m'; GREEN=$'\e[32m'; CYAN=$'\e[36m'
YELLOW=$'\e[33m'; RED=$'\e[31m'; DIM=$'\e[2m'

banner()  { echo ""; echo "${BOLD}${GREEN}▶ $*${RESET}"; }
info()    { echo "  ${CYAN}$*${RESET}"; }
warn()    { echo "  ${YELLOW}⚠  $*${RESET}"; }
die()     { echo "  ${RED}✗  $*${RESET}"; exit 1; }
prompt()  { read -rp "  ${BOLD}$1${RESET} " "$2"; }

echo ""
echo "${BOLD}╔══════════════════════════════════════════════╗"
echo "║   AgentFlow — Deploy Wizard                  ║"
echo "╚══════════════════════════════════════════════╝${RESET}"

# ── Dependency check ──────────────────────────────────────────────────────────
banner "Checking prerequisites…"
command -v docker >/dev/null 2>&1 || die "Docker not found."
COMPOSE="docker compose"
$COMPOSE version >/dev/null 2>&1 || \
  { COMPOSE="docker-compose"; $COMPOSE version >/dev/null 2>&1 || die "Docker Compose not found."; }
echo "  Docker:   $(docker --version)"
echo "  Compose:  $($COMPOSE version 2>/dev/null | head -1)"

# ── Choose deploy mode ────────────────────────────────────────────────────────
banner "Deploy mode"
echo ""
echo "  ${BOLD}1)${RESET} ${GREEN}Quick deploy${RESET}   — load pre-built linux/amd64 image  (no compile, ~30s)"
echo "  ${BOLD}2)${RESET} ${CYAN}Build from source${RESET} — docker compose --build              (~5 min, any arch)"
echo ""
prompt "Choice [1]:" MODE
MODE=${MODE:-1}

COMPOSE_FILE="compose.release.yaml"
NEED_BUILD=false
if [ "${MODE}" = "2" ]; then
    COMPOSE_FILE="compose.yaml"
    NEED_BUILD=true
fi

# ── Load pre-built image (mode 1) ─────────────────────────────────────────────
if [ "${MODE}" != "2" ]; then
    banner "Loading pre-built image…"
    if docker image inspect agentflow-server:latest >/dev/null 2>&1; then
        echo "  Image already present — skipping load."
    else
        [ -f agentflow-server.tar.gz ] || die "agentflow-server.tar.gz not found."
        LOADED=$(docker load < agentflow-server.tar.gz | grep "Loaded image" | awk '{print $NF}')
        if [ -n "${LOADED}" ] && [ "${LOADED}" != "agentflow-server:latest" ]; then
            docker tag "${LOADED}" agentflow-server:latest
        fi
        echo "  ✓ Image loaded."
    fi
fi

# ── Environment ───────────────────────────────────────────────────────────────
banner "Configuring environment…"
[ -f .env ] || { cp .env.example .env; echo "  Created .env from template."; }

get_env() { grep -E "^${1}=" .env 2>/dev/null | cut -d= -f2- || true; }
set_env() {
    local k="$1" v="$2"
    if grep -qE "^${k}=" .env 2>/dev/null; then sed -i "s|^${k}=.*|${k}=${v}|" .env
    else echo "${k}=${v}" >> .env; fi
}

LLM_KEY=$(get_env LLM_API_KEY)
if [ -z "${LLM_KEY}" ]; then
    echo ""
    info "LLM_API_KEY — Anthropic, OpenAI, or leave blank for Ollama."
    prompt "LLM_API_KEY:" LLM_KEY
    set_env LLM_API_KEY "${LLM_KEY}"
fi

DASH_PORT=$(get_env DASHBOARD_EXTERNAL_PORT); DASH_PORT=${DASH_PORT:-80}
prompt "Dashboard port [${DASH_PORT}]:" P
[ -n "${P:-}" ] && { set_env DASHBOARD_EXTERNAL_PORT "${P}"; DASH_PORT="${P}"; }

# ── Start stack ───────────────────────────────────────────────────────────────
banner "Starting stack (${COMPOSE_FILE})…"
if $NEED_BUILD; then
    $COMPOSE -f "${COMPOSE_FILE}" up -d --build
else
    $COMPOSE -f "${COMPOSE_FILE}" up -d
fi

# ── Wait for health ───────────────────────────────────────────────────────────
banner "Waiting for health checks (up to 90s)…"
TIMEOUT=90; ELAPSED=0
until docker inspect --format='{{.State.Health.Status}}' agentflow-server 2>/dev/null | grep -q healthy; do
    sleep 3; ELAPSED=$((ELAPSED+3)); echo -n "."
    [ $ELAPSED -ge $TIMEOUT ] && {
        warn "Server not healthy after ${TIMEOUT}s — check: docker logs agentflow-server"; break
    }
done
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
printf "║  ✓  Dashboard   http://%-22s║\n" "${HOST_IP}:${DASH_PORT}/"
printf "║     MQTT TCP    %-1s:%-21s║\n" "${HOST_IP}" "$(get_env MQTT_EXTERNAL_PORT || echo 1883)"
echo "╠══════════════════════════════════════════════╣"
printf "║  ${DIM}docker compose -f ${COMPOSE_FILE} ps${RESET}${BOLD}${GREEN}               ║\n"
printf "║  ${DIM}docker compose -f ${COMPOSE_FILE} logs -f${RESET}${BOLD}${GREEN}           ║\n"
echo "╚══════════════════════════════════════════════╝${RESET}"

echo ""
echo "${DIM}Tip: to rebuild after code changes:"
echo "  docker compose -f compose.yaml up -d --build${RESET}"
DEPLOYSCRIPT

chmod +x "${WORK_DIR}/deploy.sh"

# ── 7. DEPLOY.md ──────────────────────────────────────────────────────────────
cat > "${WORK_DIR}/DEPLOY.md" << 'MDOC'
# AgentFlow — Full Release Package

This archive contains both **pre-built artifacts** and **full source code**.

## Quick start

```bash
unzip agentflow-full-*.zip
cd agentflow-full-*/
bash deploy.sh
```

The wizard will ask:
- **Mode 1** — load pre-built `linux/amd64` image → running in ~30 s
- **Mode 2** — build from source via Docker → ~5 min (any architecture)

---

## Manual paths

### A) Quick deploy (pre-built image)

```bash
docker load < agentflow-server.tar.gz
cp .env.example .env && nano .env     # set LLM_API_KEY
docker compose -f compose.release.yaml up -d
```

### B) Build from source

```bash
cp .env.example .env && nano .env
docker compose up -d --build          # uses compose.yaml with build: directives
```

### C) Dev / mock mode (no LLM)

```bash
docker compose -f compose.dev.yaml up -d
# Dashboard served at :9000 (nginx) with mock agent data
```

### D) Rebuild frontend only (e.g. after editing src/)

```bash
cd frontend
npm ci
npm run build
# nginx picks up frontend/dist/ automatically (volume mount)
docker exec agentflow-dashboard nginx -s reload
```

---

## Directory layout

```
.
├── compose.yaml           # build-from-source (default dev/prod)
├── compose.release.yaml   # pre-built image (fastest deploy)
├── compose.dev.yaml       # mock-only, no LLM
├── .env.example           # copy to .env and fill secrets
├── agentflow-server.tar.gz   # linux/amd64 Docker image
├── deploy.sh              # interactive wizard
├── rust/                  # Rust source (agentflow-server)
│   ├── Dockerfile
│   ├── Cargo.toml / Cargo.lock
│   └── crates/
├── frontend/
│   ├── dist/              # pre-built SPA (nginx serves this)
│   ├── src/               # TypeScript source
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
├── infra/
│   ├── nginx/nginx.conf
│   └── mosquitto/mosquitto.conf
└── scripts/
    └── mock-agents.mjs
```

## Environment variables

| Key | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `ollama` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Any model ID |
| `LLM_API_KEY` | _(required)_ | API key |
| `DASHBOARD_EXTERNAL_PORT` | `80` | nginx host port |
| `MQTT_EXTERNAL_PORT` | `1883` | MQTT TCP port |
| `VITE_GOOGLE_AI_KEY` | _(baked into dist/)_ | Rebuild frontend to change |
MDOC

# ── 8. Zip it ──────────────────────────────────────────────────────────────────
echo ""
echo "▶ Creating zip archive…"
cd /tmp
zip -r "${OLDPWD}/${OUT_FILE}" "${RELEASE_NAME}/" -x "*.DS_Store" -x "__MACOSX/*"
cd - >/dev/null
rm -rf "${WORK_DIR}"

SIZE=$(du -sh "${OUT_FILE}" | cut -f1)
echo ""
echo "══════════════════════════════════════════════════════"
echo " ✓  ${OUT_FILE}  (${SIZE})"
echo ""
echo " Transfer + deploy:"
echo "   sftp> put ${OUT_FILE}"
echo "   ssh> unzip ${OUT_FILE} && cd ${RELEASE_NAME} && bash deploy.sh"
echo "══════════════════════════════════════════════════════"
