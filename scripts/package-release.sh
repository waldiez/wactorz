#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Wactorz — release packager
#
# Produces:  wactorz-release-<YYYYMMDD>.tar.gz
#
# The archive is self-contained:
#   • Pre-built Vite SPA  (static/app/)
#   • Exported Docker image  (wactorz-server.tar.gz inside the archive)
#   • All infra configs  (nginx, mosquitto)
#   • A deploy.sh wizard  (set env → docker load → docker compose up)
#
# Prerequisites (run on the build machine):
#   • Docker running and wactorz-server:latest already built
#   • Node.js / npm  (for `npm run build`)
#   • The current directory must be the repo root
#
# Usage:
#   cd /path/to/wactorz
#   bash scripts/package-release.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."   # always run from repo root

DATE=$(date +%Y%m%d)
RELEASE_NAME="wactorz-release-${DATE}"
WORK_DIR="/tmp/${RELEASE_NAME}"
OUT_FILE="${RELEASE_NAME}.tar.gz"

echo "══════════════════════════════════════════════"
echo " Wactorz Release Packager"
echo " Output: ${OUT_FILE}"
echo "══════════════════════════════════════════════"

# ── 1. Build the frontend ─────────────────────────────────────────────────────
echo ""
echo "▶ Building frontend (npm run build)…"
cd frontend
npm run build
cd ..
echo "  ✓ static/app/ ready"

# ── 2. Build + export the Docker image for linux/amd64 ───────────────────────
# Always target linux/amd64 — the most common server architecture.
# On Apple Silicon this cross-compiles via QEMU (~5-8 min for Rust).
echo ""
echo "▶ Building Docker image for linux/amd64 (cross-compile via buildx)…"
echo "  This takes ~5-8 min on Apple Silicon — Rust compiles under QEMU."
docker buildx build \
  --platform linux/amd64 \
  --tag wactorz-server:release-amd64 \
  --load \
  ./rust
echo ""
echo "▶ Exporting linux/amd64 image…"
docker save wactorz-server:release-amd64 | gzip -9 > /tmp/wactorz-server.tar.gz
# Clean up the extra tag
docker rmi wactorz-server:release-amd64 --force >/dev/null 2>&1 || true
echo "  ✓ Image saved ($(du -sh /tmp/wactorz-server.tar.gz | cut -f1))"

# ── 3. Build staging directory ────────────────────────────────────────────────
echo ""
echo "▶ Assembling release directory…"
rm -rf "${WORK_DIR}"
mkdir -p \
  "${WORK_DIR}/frontend" \
  "${WORK_DIR}/infra/nginx" \
  "${WORK_DIR}/infra/mosquitto" \
  "${WORK_DIR}/scripts"

# Pre-built SPA
cp -r static/app "${WORK_DIR}/static/app"

# Infrastructure configs
cp infra/nginx/nginx.conf        "${WORK_DIR}/infra/nginx/nginx.conf"
cp infra/mosquitto/mosquitto.conf "${WORK_DIR}/infra/mosquitto/mosquitto.conf"

# Dev tools
cp scripts/mock-agents.mjs "${WORK_DIR}/scripts/mock-agents.mjs"

# Env template
cp .env.example "${WORK_DIR}/.env.example"

# Exported image
mv /tmp/wactorz-server.tar.gz "${WORK_DIR}/wactorz-server.tar.gz"

# ── 4. Write compose.yaml (deploy-mode: no build, image already loaded) ───────
cat > "${WORK_DIR}/compose.yaml" << 'COMPOSEYAML'
# Wactorz — production deploy compose
# The wactorz-server image is already loaded from wactorz-server.tar.gz.
# Run:  docker compose up -d

name: wactorz

services:

  mosquitto:
    image: eclipse-mosquitto:2.0
    container_name: wactorz-mosquitto
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
      - wactorz-net
    healthcheck:
      test: ["CMD", "mosquitto_sub", "-t", "$$SYS/#", "-C", "1", "-i", "hc", "-W", "3"]
      interval: 10s
      timeout: 5s
      retries: 5

  wactorz:
    image: wactorz-server:latest
    platform: linux/amd64
    container_name: wactorz-server
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
      RUST_LOG: "${RUST_LOG:-wactorz=info,tower_http=warn}"
      NO_CLI: "true"
    networks:
      - wactorz-net
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
    container_name: wactorz-dashboard
    restart: unless-stopped
    ports:
      - "${DASHBOARD_EXTERNAL_PORT:-80}:80"
    volumes:
      - ./static/app:/usr/share/nginx/html:ro
      - ./infra/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    networks:
      - wactorz-net
    depends_on:
      wactorz:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost/"]
      interval: 10s
      timeout: 3s
      retries: 3

networks:
  wactorz-net:

volumes:
  mosquitto-data:
  mosquitto-logs:
COMPOSEYAML

# ── 5. Write compose.dev.yaml (mock only, no LLM required) ───────────────────
cat > "${WORK_DIR}/compose.dev.yaml" << 'DEVYAML'
# Wactorz — dev/demo mode (mock agents, no LLM required)
# Run:  docker compose -f compose.dev.yaml up -d
# Then open http://localhost:9000 (or serve static/app/ with any static server)

name: wactorz-dev

services:
  mosquitto:
    image: eclipse-mosquitto:2.0
    container_name: wactorz-dev-mosquitto
    restart: unless-stopped
    hostname: mosquitto
    ports:
      - "1883:1883"
      - "9001:9001"
    volumes:
      - ./infra/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf
    healthcheck:
      test: ["CMD", "mosquitto_sub", "-t", "$$SYS/#", "-C", "1", "-i", "hc", "-W", "3"]
      interval: 10s
      timeout: 5s
      retries: 5

  mock-agents:
    image: node:22-alpine
    container_name: wactorz-dev-mock
    restart: unless-stopped
    working_dir: /app
    volumes:
      - ./scripts:/app
    command: node mock-agents.mjs
    environment:
      MQTT_HOST: mosquitto
      MQTT_PORT: "1883"
    depends_on:
      mosquitto:
        condition: service_healthy

  dashboard:
    image: nginx:1.27-alpine
    container_name: wactorz-dev-dashboard
    restart: unless-stopped
    ports:
      - "9000:80"
    volumes:
      - ./static/app:/usr/share/nginx/html:ro
      - ./infra/nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - mock-agents
DEVYAML

# ── 6. Write deploy.sh ────────────────────────────────────────────────────────
cat > "${WORK_DIR}/deploy.sh" << 'DEPLOYSCRIPT'
#!/usr/bin/env bash
# Wactorz — deployment wizard
# Run once after extracting the archive on the target host.
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\e[1m'; RESET=$'\e[0m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'

banner() { echo ""; echo "${BOLD}${GREEN}▶ $*${RESET}"; }
warn()   { echo "${YELLOW}  ⚠  $*${RESET}"; }
die()    { echo "${RED}  ✗  $*${RESET}"; exit 1; }

echo ""
echo "${BOLD}══════════════════════════════════════════════"
echo "  Wactorz — Deploy Wizard"
echo "══════════════════════════════════════════════${RESET}"

# ── Dependency checks ─────────────────────────────────────────────────────────
banner "Checking prerequisites…"

command -v docker >/dev/null 2>&1 || die "Docker not found — install Docker Engine first."
docker compose version >/dev/null 2>&1 || \
  docker-compose version >/dev/null 2>&1 || \
  die "Docker Compose V2 not found — run: apt install docker-compose-plugin"

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"

echo "  Docker:          $(docker --version)"
echo "  Docker Compose:  $($COMPOSE version --short 2>/dev/null || $COMPOSE version)"

# ── Load Docker image ─────────────────────────────────────────────────────────
banner "Loading wactorz-server Docker image…"
if docker image inspect wactorz-server:latest >/dev/null 2>&1; then
    echo "  Image already present — skipping load."
else
    [ -f wactorz-server.tar.gz ] || die "wactorz-server.tar.gz not found next to deploy.sh"
    LOADED=$(docker load < wactorz-server.tar.gz | grep "Loaded image" | awk '{print $NF}')
    # Normalize tag to :latest regardless of what was saved
    if [ -n "${LOADED}" ] && [ "${LOADED}" != "wactorz-server:latest" ]; then
        docker tag "${LOADED}" wactorz-server:latest
    fi
    echo "  ✓ Image loaded."
fi

# ── Environment setup ─────────────────────────────────────────────────────────
banner "Configuring environment…"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from template."
fi

# Helper: read current value from .env
get_env() { grep -E "^${1}=" .env 2>/dev/null | cut -d= -f2- || true; }

# Helper: set/replace a key in .env (Linux sed -i, works in-place)
set_env() {
    local key="$1" val="$2"
    if grep -qE "^${key}=" .env 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" .env
    else
        echo "${key}=${val}" >> .env
    fi
}

# Prompt for LLM API key if not set
LLM_KEY=$(get_env LLM_API_KEY)
if [ -z "${LLM_KEY}" ]; then
    echo ""
    echo "  ${BOLD}LLM API key${RESET} (Anthropic claude-sonnet-4-6 by default)."
    echo "  Leave blank to use Ollama (set LLM_PROVIDER=ollama in .env)."
    read -rp "  LLM_API_KEY: " LLM_KEY
    set_env LLM_API_KEY "${LLM_KEY}"
fi

# Prompt for dashboard port
DASH_PORT=$(get_env DASHBOARD_EXTERNAL_PORT)
DASH_PORT=${DASH_PORT:-80}
read -rp "  Dashboard port [${DASH_PORT}]: " p
[ -n "${p}" ] && set_env DASHBOARD_EXTERNAL_PORT "${p}" && DASH_PORT="${p}"

# ── Start stack ───────────────────────────────────────────────────────────────
banner "Starting Wactorz stack…"
$COMPOSE up -d

# ── Health check ──────────────────────────────────────────────────────────────
banner "Waiting for health checks (up to 60s)…"
TIMEOUT=60; ELAPSED=0
until docker inspect --format='{{.State.Health.Status}}' wactorz-server 2>/dev/null | grep -q healthy; do
    sleep 3; ELAPSED=$((ELAPSED+3))
    [ $ELAPSED -ge $TIMEOUT ] && { warn "wactorz-server did not become healthy in ${TIMEOUT}s — check logs: docker logs wactorz-server"; break; }
    echo -n "."
done
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "${BOLD}${GREEN}══════════════════════════════════════════════"
echo "  ✓  Wactorz deployed!"
echo ""
echo "  Dashboard:  http://${HOST_IP}:${DASH_PORT}/"
echo "  MQTT TCP:   ${HOST_IP}:$(get_env MQTT_EXTERNAL_PORT || echo 1883)"
echo ""
echo "  Manage:  docker compose ps"
echo "  Logs:    docker compose logs -f wactorz"
echo "  Stop:    docker compose down"
echo "══════════════════════════════════════════════${RESET}"
DEPLOYSCRIPT

chmod +x "${WORK_DIR}/deploy.sh"

# ── 7. Write a quick-start README ─────────────────────────────────────────────
cat > "${WORK_DIR}/DEPLOY.md" << 'READMEDOC'
# Wactorz — Quick Deploy

## Requirements
- Linux host (x86_64)
- Docker Engine ≥ 24 + Docker Compose V2 (`docker compose version`)
- Port 80 available (or set `DASHBOARD_EXTERNAL_PORT` in `.env`)
- Port 1883 available (MQTT TCP, for IoT devices)

## Steps

```bash
# 1. Extract
tar xzf wactorz-release-*.tar.gz
cd wactorz-release-*/

# 2. Run the wizard (sets env, loads image, starts stack)
bash deploy.sh

# 3. Open the dashboard
http://<your-host-ip>/
```

## Manual setup (skip the wizard)

```bash
cp .env.example .env
# Edit .env — set LLM_API_KEY at minimum

docker load < wactorz-server.tar.gz
docker compose up -d
```

## Dev / demo mode (no LLM required)

```bash
# Starts mock agents only — no real AI responses
docker compose -f compose.dev.yaml up -d
# Dashboard served at http://localhost:9000/
```

## Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `ollama` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model ID |
| `LLM_API_KEY` | _(required)_ | API key for Anthropic or OpenAI |
| `DASHBOARD_EXTERNAL_PORT` | `80` | Host port for the web dashboard |
| `MQTT_EXTERNAL_PORT` | `1883` | Host port for MQTT TCP |

## Notes

- **Google AI key** (for agent portrait photos): baked into the frontend bundle
  at build time. To change it, rebuild the frontend and re-package.
- **Home Assistant** and **Fuseki** are optional; start with `--profile full`.
- **Logs**: `docker compose logs -f wactorz`
- **Upgrade**: run `bash deploy.sh` again with a new archive — existing volumes
  (MQTT data) are preserved.
READMEDOC

# ── 8. Create the tarball ─────────────────────────────────────────────────────
echo ""
echo "▶ Creating archive…"
cd /tmp
tar czf "${OLDPWD}/${OUT_FILE}" "${RELEASE_NAME}/"
cd - >/dev/null
rm -rf "${WORK_DIR}"

SIZE=$(du -sh "${OUT_FILE}" | cut -f1)
echo ""
echo "══════════════════════════════════════════════"
echo " ✓  ${OUT_FILE}  (${SIZE})"
echo ""
echo " Deploy on target host:"
echo "   scp ${OUT_FILE} user@host:~/"
echo "   ssh user@host 'tar xzf ${OUT_FILE} && cd ${RELEASE_NAME} && bash deploy.sh'"
echo "══════════════════════════════════════════════"
