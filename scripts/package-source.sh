#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentFlow — source package builder
#
# Produces:  agentflow-src-<YYYYMMDD>.tar.gz  (~2-4 MB)
#
# Contents (NO pre-built binary — the target host builds it from source):
#   rust/                   Rust workspace (full source)
#   frontend/dist/          Pre-built Vite SPA  (built locally, saves Node on host)
#   infra/                  nginx + mosquitto configs
#   systemd/                systemd unit template
#   scripts/                build-native.sh, mock-agents.mjs
#   compose.native.yaml     Mosquitto-only Docker support
#   .env.example
#   setup.sh                One-shot build + deploy script for the target host
#
# Prerequisites (LOCAL build machine):
#   • Node.js / npm  (for `npm run build` — frontend only, fast ~30s)
#
# Prerequisites (TARGET host):
#   • Rust ≥ 1.93  (for `cargo build --release`)
#   • Docker + Compose plugin  (for Mosquitto support service)
#
# Install Rust on the target if needed:
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
#   source ~/.cargo/env
#
# Usage:
#   bash scripts/package-source.sh
#   scp agentflow-src-*.tar.gz user@host:~/
#   ssh user@host 'tar xzf agentflow-src-*.tar.gz && cd agentflow-src-*/ && bash setup.sh'
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."   # always run from repo root

DATE=$(date +%Y%m%d)
RELEASE_NAME="agentflow-src-${DATE}"
WORK_DIR="/tmp/${RELEASE_NAME}"
OUT_FILE="${RELEASE_NAME}.tar.gz"

BOLD=$'\e[1m'; RESET=$'\e[0m'; GREEN=$'\e[32m'; CYAN=$'\e[36m'; DIM=$'\e[2m'
echo ""
echo "${BOLD}══════════════════════════════════════════════════════"
echo "  AgentFlow — Source Packager"
echo "  Output: ${OUT_FILE}"
echo "══════════════════════════════════════════════════════${RESET}"

# ── 1. Build the frontend (locally — saves node+npm install on the target) ───
echo ""
echo "${BOLD}▶ Building frontend…${RESET}"
cd frontend
npm run build
cd ..
echo "  ${GREEN}✓ frontend/dist/ ready${RESET}"

# ── 2. Stage ─────────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}▶ Assembling source archive…${RESET}"
rm -rf "${WORK_DIR}"
mkdir -p \
    "${WORK_DIR}/frontend" \
    "${WORK_DIR}/infra/nginx" \
    "${WORK_DIR}/infra/mosquitto" \
    "${WORK_DIR}/systemd" \
    "${WORK_DIR}/scripts"

# Source (no build artefacts, no secrets)
cp -r rust "${WORK_DIR}/rust"

# Pre-built SPA (so target host needs no Node.js)
cp -r frontend/dist "${WORK_DIR}/frontend/dist"

# Infrastructure
cp infra/nginx/nginx-native.conf      "${WORK_DIR}/infra/nginx/nginx-native.conf"
cp infra/nginx/agentflow-snippet.conf "${WORK_DIR}/infra/nginx/agentflow-snippet.conf"
cp infra/mosquitto/mosquitto.conf      "${WORK_DIR}/infra/mosquitto/mosquitto.conf"

# Compose, systemd, env template, helpers
cp compose.native.yaml                "${WORK_DIR}/compose.native.yaml"
cp systemd/agentflow.service          "${WORK_DIR}/systemd/agentflow.service"
cp .env.example                       "${WORK_DIR}/.env.example"
cp scripts/build-native.sh            "${WORK_DIR}/scripts/build-native.sh"
cp scripts/mock-agents.mjs            "${WORK_DIR}/scripts/mock-agents.mjs"
chmod +x "${WORK_DIR}/scripts/build-native.sh"

# ── 3. Write setup.sh ─────────────────────────────────────────────────────────
cat > "${WORK_DIR}/setup.sh" << 'SETUP'
#!/usr/bin/env bash
# AgentFlow — one-shot build + deploy script
# Run on the target host after extracting the archive.
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\e[1m'; RESET=$'\e[0m'; GREEN=$'\e[32m'; CYAN=$'\e[36m'
YELLOW=$'\e[33m'; RED=$'\e[31m'; DIM=$'\e[2m'
banner() { echo ""; echo "${BOLD}${GREEN}▶ $*${RESET}"; }
info()   { echo "  ${CYAN}$*${RESET}"; }
ok()     { echo "  ${GREEN}✓ $*${RESET}"; }
warn()   { echo "  ${YELLOW}⚠  $*${RESET}"; }
die()    { echo "  ${RED}✗  $*${RESET}"; exit 1; }

echo ""
echo "${BOLD}╔══════════════════════════════════════════════════════╗"
echo "║   AgentFlow — Setup Wizard                          ║"
echo "╚══════════════════════════════════════════════════════╝${RESET}"

# ── Prerequisites ─────────────────────────────────────────────────────────────
banner "Checking prerequisites…"

# Rust
if ! command -v cargo >/dev/null 2>&1; then
    warn "Rust not found.  Installing via rustup…"
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
    source "$HOME/.cargo/env"
fi
ok "Rust: $(cargo --version)"

# Docker (for Mosquitto)
if command -v docker >/dev/null 2>&1; then
    ok "Docker: $(docker --version | head -c 50)"
    HAVE_DOCKER=1
else
    warn "Docker not found — will skip Mosquitto container."
    warn "Install with: curl -fsSL https://get.docker.com | sh"
    HAVE_DOCKER=0
fi

# Docker Compose
COMPOSE_CMD=""
if [ "${HAVE_DOCKER}" = "1" ]; then
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD="docker-compose"
    fi
fi

# ── Environment ───────────────────────────────────────────────────────────────
banner "Configuring environment…"

if [ ! -f .env ]; then
    cp .env.example .env
    info "Created .env from template."
fi

get_env() { grep -E "^${1}=" .env 2>/dev/null | cut -d= -f2- || true; }
set_env() {
    local k="$1" v="$2"
    if grep -qE "^${k}=" .env 2>/dev/null; then
        sed -i "s|^${k}=.*|${k}=${v}|" .env
    else
        echo "${k}=${v}" >> .env
    fi
}

# Always use localhost for native binary mode
set_env MQTT_HOST localhost

INSTALL_DIR="$(pwd)"
LLM_KEY=$(get_env LLM_API_KEY)
if [ -z "${LLM_KEY}" ]; then
    echo ""
    info "Enter your LLM API key (Anthropic / OpenAI)."
    info "Leave blank to use Ollama (set LLM_PROVIDER=ollama in .env)."
    read -rp "  LLM_API_KEY: " LLM_KEY
    set_env LLM_API_KEY "${LLM_KEY}"
fi

echo ""
NGINX_MODE=""
info "Does this server have nginx already running (certbot/SSL)?"
echo "  ${BOLD}1)${RESET} Yes — existing nginx (certbot/SSL already set up)"
echo "  ${BOLD}2)${RESET} No  — start Docker nginx on port 80"
read -rp "  Choice [1]: " NGINX_MODE
NGINX_MODE="${NGINX_MODE:-1}"
if [ "${NGINX_MODE}" = "1" ]; then
    set_env DEPLOY_NGINX_MODE existing
else
    set_env DEPLOY_NGINX_MODE docker
fi

# ── Build binary ──────────────────────────────────────────────────────────────
banner "Building agentflow binary from source…"
info "This takes ~3-5 min on first build (Rust + deps), ~30s on rebuilds."
cd rust
cargo build --release --bin agentflow
cd ..
cp rust/target/release/agentflow ./agentflow
chmod +x ./agentflow
ok "Binary ready: ./agentflow  ($(du -sh agentflow | cut -f1))"

# ── Start Mosquitto ───────────────────────────────────────────────────────────
if [ "${HAVE_DOCKER}" = "1" ] && [ -n "${COMPOSE_CMD}" ]; then
    banner "Starting Mosquitto (MQTT broker)…"
    ${COMPOSE_CMD} -f compose.native.yaml up -d mosquitto
    ok "Mosquitto running (TCP :1883, WS :9001)"
else
    warn "Skipping Mosquitto — install Docker and run:"
    info "  docker compose -f compose.native.yaml up -d mosquitto"
fi

# ── nginx configuration ───────────────────────────────────────────────────────
NGINX_CONF_PATH="/etc/nginx/conf.d/agentflow.conf"
NGINX_CONF_PATH=$(get_env DEPLOY_NGINX_CONF 2>/dev/null || echo "${NGINX_CONF_PATH}")
NGINX_CONF_PATH="${NGINX_CONF_PATH:-/etc/nginx/conf.d/agentflow.conf}"

if [ "${NGINX_MODE}" = "1" ]; then
    banner "Configuring existing nginx…"
    # Patch the snippet's root path to match the install directory
    sed "s|/opt/agentflow|${INSTALL_DIR}|g" \
        infra/nginx/agentflow-snippet.conf > /tmp/agentflow-nginx-snippet.tmp
    sudo mv /tmp/agentflow-nginx-snippet.tmp "${NGINX_CONF_PATH}"
    ok "Snippet deployed: ${NGINX_CONF_PATH}"
    echo ""
    warn "Add the following to your SSL server block (once, if not already there):"
    echo ""
    echo "    ${DIM}include ${NGINX_CONF_PATH};${RESET}"
    echo ""
    read -rp "  Press Enter after updating nginx config, then we'll reload nginx… "
    if sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx
        ok "nginx reloaded"
    else
        warn "nginx -t failed — check your config before reloading."
    fi
elif [ "${NGINX_MODE}" = "2" ]; then
    banner "Starting Docker nginx…"
    if [ "${HAVE_DOCKER}" = "1" ] && [ -n "${COMPOSE_CMD}" ]; then
        ${COMPOSE_CMD} -f compose.native.yaml up -d dashboard
        ok "Docker nginx running on port :80"
    else
        warn "Docker not available — serve frontend/dist/ with your own web server."
    fi
fi

# ── systemd service ───────────────────────────────────────────────────────────
banner "Install systemd service?"
echo "  ${BOLD}1)${RESET} Install systemd service (starts on boot, managed by journalctl)"
echo "  ${BOLD}2)${RESET} Run in foreground now"
read -rp "  Choice [1]: " SVC_MODE
SVC_MODE="${SVC_MODE:-1}"
CURRENT_USER="$(whoami)"

if [ "${SVC_MODE}" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    PATCHED="/tmp/agentflow.service.setup"
    sed \
        -e "s|WorkingDirectory=.*|WorkingDirectory=${INSTALL_DIR}|" \
        -e "s|EnvironmentFile=.*|EnvironmentFile=${INSTALL_DIR}/.env|" \
        -e "s|ExecStart=.*|ExecStart=${INSTALL_DIR}/agentflow --no-cli|" \
        -e "s|User=%i|User=${CURRENT_USER}|" \
        systemd/agentflow.service > "${PATCHED}"
    sudo cp "${PATCHED}" /etc/systemd/system/agentflow.service
    rm -f "${PATCHED}"
    sudo systemctl daemon-reload
    if systemctl is-active --quiet agentflow 2>/dev/null; then
        sudo systemctl restart agentflow
        ok "Service restarted"
    else
        sudo systemctl enable --now agentflow
        ok "Service enabled and started"
    fi
    sleep 2
    if systemctl is-active --quiet agentflow; then
        ok "agentflow is running"
    else
        warn "agentflow didn't start cleanly — check: journalctl -u agentflow -n 30"
    fi
else
    banner "Running agentflow in the foreground…"
    set -a; source .env; set +a
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    echo ""
    echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
    printf  "║  Dashboard  http://%-32s║\n" "${HOST_IP}/"
    echo    "║  Ctrl-C to stop                                      ║"
    echo    "╚══════════════════════════════════════════════════════╝${RESET}"
    exec ./agentflow --no-cli
fi

# ── Done ─────────────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
printf  "║  ✓  Dashboard  http://%-30s║\n" "${HOST_IP}/"
echo    "╠══════════════════════════════════════════════════════╣"
printf  "║  ${DIM}journalctl -u agentflow -f${RESET}${BOLD}${GREEN}                        ║\n"
printf  "║  ${DIM}systemctl status agentflow${RESET}${BOLD}${GREEN}                        ║\n"
echo    "╚══════════════════════════════════════════════════════╝${RESET}"
SETUP

chmod +x "${WORK_DIR}/setup.sh"

# ── 4. Write a quick README ───────────────────────────────────────────────────
cat > "${WORK_DIR}/README.md" << 'README'
# AgentFlow — Source Package

Build + deploy on the target host.  No pre-built binary — Rust compiles natively.

## Quick start

```bash
tar xzf agentflow-src-*.tar.gz
cd agentflow-src-*/
bash setup.sh
```

The wizard will:
1. Install Rust (via rustup) if not present
2. Build the agentflow binary with `cargo build --release`
3. Ask about nginx mode (existing SSL or fresh Docker nginx)
4. Start Mosquitto via Docker
5. Install + start the systemd service

## Prerequisites on the target host

| Requirement | Install |
|---|---|
| Rust ≥ 1.93 | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| Docker + Compose | `curl -fsSL https://get.docker.com \| sh` |
| git (optional) | `apt install git` |

## Manual steps (skip the wizard)

```bash
# 1. Configure env
cp .env.example .env
nano .env   # set LLM_API_KEY; MQTT_HOST must be 'localhost'

# 2. Build
cd rust && cargo build --release --bin agentflow && cd ..
cp rust/target/release/agentflow .

# 3. Start Mosquitto
docker compose -f compose.native.yaml up -d mosquitto

# 4. Configure nginx
#   Existing nginx (certbot/SSL):
sudo cp infra/nginx/agentflow-snippet.conf /etc/nginx/conf.d/agentflow.conf
# then add:  include /etc/nginx/conf.d/agentflow.conf;  to your server block
sudo nginx -t && sudo systemctl reload nginx

#   Fresh Docker nginx (port 80):
docker compose -f compose.native.yaml up -d

# 5. Start agentflow
source .env && ./agentflow --no-cli
# or install systemd service — see systemd/agentflow.service
```

## Updating

```bash
# Frontend already pre-built in this package.
# To rebuild from new source, clone the repo and re-package.

# Update binary only (if Rust is already installed):
cd rust && cargo build --release --bin agentflow && cd ..
cp rust/target/release/agentflow .
sudo systemctl restart agentflow
```

## MQTT_HOST reminder

When running natively, the binary connects to Mosquitto on **localhost**.
Make sure `.env` has:
```
MQTT_HOST=localhost
```
(not `mosquitto` — that only resolves inside Docker containers)
README

# ── 5. Strip Rust build artefacts ────────────────────────────────────────────
# Keep source only — target/ can be hundreds of MB
rm -rf "${WORK_DIR}/rust/target"
# Remove Cargo.lock (let the target resolve fresh) — or keep it for reproducibility
# (keeping it — prevents surprise dep upgrades)

SIZE_BEFORE=$(du -sh "${WORK_DIR}" | cut -f1)
echo "  ${DIM}Staged size: ${SIZE_BEFORE}${RESET}"

# ── 6. Create the archive ─────────────────────────────────────────────────────
echo ""
echo "${BOLD}▶ Creating archive…${RESET}"
cd /tmp
tar czf "${OLDPWD}/${OUT_FILE}" "${RELEASE_NAME}/"
cd - >/dev/null
rm -rf "${WORK_DIR}"

SIZE=$(du -sh "${OUT_FILE}" | cut -f1)
echo ""
echo "${BOLD}${GREEN}══════════════════════════════════════════════════════"
echo "  ✓  ${OUT_FILE}  (${SIZE})"
echo ""
echo "  Transfer + deploy:"
echo "    scp ${OUT_FILE} user@host:~/"
echo "    ssh user@host"
echo "    tar xzf ${OUT_FILE} && cd ${RELEASE_NAME} && bash setup.sh"
echo "══════════════════════════════════════════════════════${RESET}"
