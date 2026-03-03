#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentFlow — native-binary release packager
#
# Produces:  agentflow-native-<YYYYMMDD>.tar.gz  (~20 MB vs 44 MB full zip)
#
# Contents (NO Docker image — binary is run directly on the host):
#   agentflow               stripped linux/amd64 binary
#   frontend/dist/          pre-built Vite SPA
#   infra/nginx/            nginx-native.conf (proxies to host binary)
#   infra/mosquitto/        mosquitto.conf
#   compose.native.yaml     Docker for Mosquitto + nginx only
#   systemd/agentflow.service  systemd unit template
#   scripts/build-native.sh    rebuild from source if Rust is on host
#   deploy-native.sh        deployment wizard
#   .env.example
#
# Prerequisites (build machine):
#   • Docker + buildx (to cross-compile the binary for linux/amd64)
#   • Node.js / npm
#
# On the target host you need:
#   • Docker (for Mosquitto + nginx)
#   • No Rust, no build tools — just the binary
#   • OR: bash scripts/build-native.sh  (if Rust IS installed)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."    # always run from repo root

DATE=$(date +%Y%m%d)
RELEASE_NAME="agentflow-native-${DATE}"
WORK_DIR="/tmp/${RELEASE_NAME}"
OUT_FILE="${RELEASE_NAME}.tar.gz"
BINARY="agentflow"

echo "══════════════════════════════════════════════════════"
echo " AgentFlow Native-Binary Packager"
echo " Output: ${OUT_FILE}"
echo "══════════════════════════════════════════════════════"

# ── 1. Build the frontend ─────────────────────────────────────────────────────
echo ""
echo "▶ Building frontend…"
cd frontend && npm run build && cd ..
echo "  ✓ frontend/dist/ ready"

# ── 2. Build linux/amd64 Docker image — then extract just the binary ──────────
echo ""
echo "▶ Building Docker image for linux/amd64 (to extract binary)…"
echo "  ~5-8 min on Apple Silicon."
docker buildx build \
  --platform linux/amd64 \
  --tag agentflow-server:native-extract \
  --load \
  ./rust

echo ""
echo "▶ Extracting binary from image…"
CONTAINER_ID=$(docker create --platform linux/amd64 agentflow-server:native-extract)
docker cp "${CONTAINER_ID}:/app/${BINARY}" "/tmp/${BINARY}-linux-amd64"
docker rm  "${CONTAINER_ID}" >/dev/null
docker rmi agentflow-server:native-extract --force >/dev/null 2>&1 || true
BINARY_SIZE=$(du -sh "/tmp/${BINARY}-linux-amd64" | cut -f1)
echo "  ✓ Binary extracted (${BINARY_SIZE})"

# ── 3. Stage ──────────────────────────────────────────────────────────────────
echo ""
echo "▶ Assembling staging directory…"
rm -rf "${WORK_DIR}"
mkdir -p \
  "${WORK_DIR}/frontend" \
  "${WORK_DIR}/infra/nginx" \
  "${WORK_DIR}/infra/mosquitto" \
  "${WORK_DIR}/systemd" \
  "${WORK_DIR}/scripts"

# Binary
cp "/tmp/${BINARY}-linux-amd64" "${WORK_DIR}/${BINARY}"
chmod +x "${WORK_DIR}/${BINARY}"
rm -f "/tmp/${BINARY}-linux-amd64"

# Pre-built SPA
cp -r frontend/dist "${WORK_DIR}/frontend/dist"

# Infrastructure
cp infra/nginx/nginx-native.conf      "${WORK_DIR}/infra/nginx/nginx-native.conf"
cp infra/mosquitto/mosquitto.conf     "${WORK_DIR}/infra/mosquitto/mosquitto.conf"

# Compose + systemd
cp compose.native.yaml                "${WORK_DIR}/compose.native.yaml"
cp systemd/agentflow.service          "${WORK_DIR}/systemd/agentflow.service"

# Scripts
cp scripts/build-native.sh            "${WORK_DIR}/scripts/build-native.sh"
cp scripts/mock-agents.mjs            "${WORK_DIR}/scripts/mock-agents.mjs"
cp .env.example                       "${WORK_DIR}/.env.example"
chmod +x "${WORK_DIR}/scripts/build-native.sh"

# ── 4. deploy-native.sh ───────────────────────────────────────────────────────
cat > "${WORK_DIR}/deploy-native.sh" << 'DEPLOYSCRIPT'
#!/usr/bin/env bash
# AgentFlow — native-binary deployment wizard
# Run once after extracting the archive on the target host.
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\e[1m'; RESET=$'\e[0m'; GREEN=$'\e[32m'; CYAN=$'\e[36m'
YELLOW=$'\e[33m'; RED=$'\e[31m'; DIM=$'\e[2m'

banner() { echo ""; echo "${BOLD}${GREEN}▶ $*${RESET}"; }
info()   { echo "  ${CYAN}$*${RESET}"; }
warn()   { echo "  ${YELLOW}⚠  $*${RESET}"; }
die()    { echo "  ${RED}✗  $*${RESET}"; exit 1; }

echo ""
echo "${BOLD}╔══════════════════════════════════════════════╗"
echo "║   AgentFlow — Native Deploy Wizard           ║"
echo "╚══════════════════════════════════════════════╝${RESET}"

# ── Prerequisites ─────────────────────────────────────────────────────────────
banner "Checking prerequisites…"
command -v docker >/dev/null 2>&1 || die "Docker not found."
COMPOSE="docker compose"
$COMPOSE version >/dev/null 2>&1 || { COMPOSE="docker-compose"; $COMPOSE version >/dev/null 2>&1 || die "Docker Compose not found."; }
echo "  Docker:   $(docker --version)"
echo "  Compose:  $($COMPOSE version 2>/dev/null | head -1)"

[ -f "./agentflow" ] || die "agentflow binary not found. Run: bash scripts/build-native.sh"
chmod +x ./agentflow
ARCH=$(file ./agentflow | grep -o 'x86-64\|aarch64\|ARM' | head -1 || echo "unknown")
info "Binary: ./agentflow  (arch: ${ARCH})"

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
    read -rp "  LLM_API_KEY: " LLM_KEY
    set_env LLM_API_KEY "${LLM_KEY}"
fi

DASH_PORT=$(get_env DASHBOARD_EXTERNAL_PORT); DASH_PORT=${DASH_PORT:-80}
read -rp "  Dashboard port [${DASH_PORT}]: " P
[ -n "${P:-}" ] && { set_env DASHBOARD_EXTERNAL_PORT "${P}"; DASH_PORT="${P}"; }

# ── Start support services (Mosquitto + nginx) ────────────────────────────────
banner "Starting support services (mosquitto + nginx)…"
$COMPOSE -f compose.native.yaml up -d
echo "  ✓ Mosquitto and nginx started."

# ── systemd install (optional) ────────────────────────────────────────────────
banner "Install as systemd service?"
echo "  ${BOLD}1)${RESET} Install systemd service (runs on boot, managed by journalctl)"
echo "  ${BOLD}2)${RESET} Run in foreground now (Ctrl-C to stop)"
echo ""
read -rp "  Choice [2]: " INSTALL_MODE
INSTALL_MODE=${INSTALL_MODE:-2}

INSTALL_DIR="$(pwd)"

if [ "${INSTALL_MODE}" = "1" ]; then
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found — falling back to foreground mode."
        INSTALL_MODE=2
    else
        SERVICE_FILE="/etc/systemd/system/agentflow.service"
        # Patch the service file with the actual install path and current user
        CURRENT_USER=$(whoami)
        sed \
            -e "s|WorkingDirectory=.*|WorkingDirectory=${INSTALL_DIR}|" \
            -e "s|EnvironmentFile=.*|EnvironmentFile=${INSTALL_DIR}/.env|" \
            -e "s|ExecStart=.*|ExecStart=${INSTALL_DIR}/agentflow --no-cli|" \
            -e "s|User=%i|User=${CURRENT_USER}|" \
            systemd/agentflow.service > /tmp/agentflow.service.patched
        sudo cp /tmp/agentflow.service.patched "${SERVICE_FILE}"
        rm /tmp/agentflow.service.patched
        sudo systemctl daemon-reload
        sudo systemctl enable --now agentflow
        echo "  ✓ Systemd service installed and started."
    fi
fi

if [ "${INSTALL_MODE}" = "2" ]; then
    # ── Run in foreground ─────────────────────────────────────────────────────
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    echo ""
    echo "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
    printf  "║  ✓  Dashboard   http://%-22s║\n" "${HOST_IP}:${DASH_PORT}/"
    printf  "║     MQTT TCP    %s:%-21s║\n"      "${HOST_IP}" "$(get_env MQTT_EXTERNAL_PORT || echo 1883)"
    printf  "║     API         http://%s:8080          ║\n" "${HOST_IP}"
    echo    "╚══════════════════════════════════════════════╝${RESET}"
    echo ""
    echo "${DIM}Press Ctrl-C to stop. For background: nohup ./agentflow --no-cli &${RESET}"
    echo ""

    # Export env vars from .env before starting
    set -a; source .env; set +a
    exec ./agentflow --no-cli
fi

# ── Done (systemd mode) ───────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
printf  "║  ✓  Dashboard   http://%-22s║\n" "${HOST_IP}:${DASH_PORT}/"
printf  "║     MQTT TCP    %s:%-21s║\n"      "${HOST_IP}" "$(get_env MQTT_EXTERNAL_PORT || echo 1883)"
echo    "╠══════════════════════════════════════════════╣"
printf  "║  ${DIM}journalctl -u agentflow -f${RESET}${BOLD}${GREEN}                ║\n"
printf  "║  ${DIM}systemctl status agentflow${RESET}${BOLD}${GREEN}                ║\n"
echo    "╚══════════════════════════════════════════════╝${RESET}"
DEPLOYSCRIPT

chmod +x "${WORK_DIR}/deploy-native.sh"

# ── 5. DEPLOY-NATIVE.md ───────────────────────────────────────────────────────
cat > "${WORK_DIR}/DEPLOY-NATIVE.md" << 'MDOC'
# AgentFlow — Native Binary Package

Runs **agentflow-server** directly on the host OS.
Docker is used only for Mosquitto (MQTT broker) and nginx (reverse proxy).

## Why native?

| | Containerised | Native binary |
|---|---|---|
| Artifact size | 39 MB Docker image | ~12 MB binary |
| SSH key access (NautilusAgent) | Needs volume mounts / secrets | Works automatically via `~/.ssh/` |
| First-run startup | Container pull + init | `<100 ms` |
| Build on target host | Docker build (~5 min) | `cargo build --release` (needs Rust) |
| Cross-compile | buildx + QEMU | `cargo build --target x86_64-unknown-linux-gnu` |

## Quick start

```bash
tar xzf agentflow-native-*.tar.gz
cd agentflow-native-*/
bash deploy-native.sh
```

The wizard:
1. Starts Mosquitto + nginx in Docker
2. Prompts for LLM API key + dashboard port
3. Either installs a **systemd service** or runs the binary in the **foreground**

## Manual steps

```bash
# 1. Start support services
docker compose -f compose.native.yaml up -d

# 2. Configure environment
cp .env.example .env
nano .env   # set LLM_API_KEY at minimum

# 3. Run agentflow
source .env
./agentflow --no-cli
```

## Build from source (if Rust is installed on the host)

```bash
# Much faster than cross-compiling on a Mac — native Rust, no QEMU
bash scripts/build-native.sh
# → produces ./agentflow
```

## systemd (persistent, starts on boot)

```bash
sudo cp systemd/agentflow.service /etc/systemd/system/
# Edit WorkingDirectory, EnvironmentFile, ExecStart, User to match your paths:
sudo nano /etc/systemd/system/agentflow.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentflow
journalctl -u agentflow -f
```

## NautilusAgent SSH/rsync

When running natively, NautilusAgent uses the system user's `~/.ssh/` automatically:

```
@nautilus-agent ping user@remote-host
@nautilus-agent exec user@remote-host df -h
@nautilus-agent sync user@remote-host:/var/data /mnt/local
@nautilus-agent push ./dist/ user@remote-host:/var/www/html/
```

No Docker secrets, no volume mounts, no extra config.
Set `NAUTILUS_SSH_KEY=/path/to/key` in `.env` to override the default key.

## Environment variables (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `ollama` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Any model ID |
| `LLM_API_KEY` | _(required)_ | API key |
| `MQTT_HOST` | `localhost` | Mosquitto is on the host; use `localhost` |
| `MQTT_PORT` | `1883` | Default Mosquitto port |
| `API_ADDR` | `0.0.0.0:8080` | agentflow REST API listen address |
| `WS_ADDR` | `0.0.0.0:8081` | agentflow WS bridge listen address |
| `DASHBOARD_EXTERNAL_PORT` | `80` | nginx host port |
| `NAUTILUS_SSH_KEY` | _(default key)_ | Path to SSH private key |
| `NAUTILUS_STRICT_HOST_KEYS` | `0` | Set `1` to enforce strict host-key checking |
| `RUST_LOG` | `agentflow=info` | Logging filter |
MDOC

# ── 6. Create the archive ─────────────────────────────────────────────────────
echo ""
echo "▶ Creating archive…"
cd /tmp
tar czf "${OLDPWD}/${OUT_FILE}" "${RELEASE_NAME}/"
cd - >/dev/null
rm -rf "${WORK_DIR}"

SIZE=$(du -sh "${OUT_FILE}" | cut -f1)
echo ""
echo "══════════════════════════════════════════════════════"
echo " ✓  ${OUT_FILE}  (${SIZE})"
echo ""
echo " Transfer + deploy:"
echo "   scp ${OUT_FILE} user@host:~/"
echo "   ssh user@host 'tar xzf ${OUT_FILE} && cd ${RELEASE_NAME} && bash deploy-native.sh'"
echo "══════════════════════════════════════════════════════"
