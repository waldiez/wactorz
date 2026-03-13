#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentFlow — deploy.sh
#
# Full bootstrap deploy AND incremental redeploy in one script.
#
# What it does:
#   1.  SSH key check / auto-generate
#   2.  Build frontend  (npm run build)
#   3.  Build Rust binary  (cargo | Docker buildx cross-compile)
#   4.  rsync static/app/, binary, infra files to the remote
#   5.  Start Mosquitto + nginx via docker compose (support services)
#   6.  Patch & install systemd unit — enable + start on first run,
#       restart on subsequent runs
#
# After the first deploy you can redeploy directly from the AgentFlow
# dashboard using NautilusAgent:
#
#   @nautilus-agent push ./static/app/ user@host:/opt/agentflow/static/app/
#   @nautilus-agent exec user@host sudo systemctl restart agentflow
#
# ── Configuration (read from .env or environment) ─────────────────────────────
#
#   DEPLOY_HOST              user@hostname of the target (required)
#   DEPLOY_PATH              remote base directory    (default: /opt/agentflow)
#   DEPLOY_SSH_PORT          SSH port                 (default: 22)
#   DEPLOY_RESTART_CMD       remote restart command   (default: systemctl restart agentflow)
#   DEPLOY_SKIP_BINARY       set to 1 to skip binary build/deploy (frontend-only redeploy)
#   CARGO_BUILD_TARGET       cross-compile target     (e.g. x86_64-unknown-linux-gnu)
#   DEPLOY_NGINX_MODE        docker   = start the Docker nginx container (default)
#                            existing = host nginx already running (certbot/SSL);
#                                       deploy snippet to DEPLOY_NGINX_CONF and reload
#
#   NAUTILUS_SSH_KEY         path to SSH private key  (auto-generated if absent)
#   NAUTILUS_STRICT_HOST_KEYS  0 = accept-new (default)  1 = strict
#   NAUTILUS_CONNECT_TIMEOUT   seconds                (default: 10)
#
# ── Prerequisites (build machine) ─────────────────────────────────────────────
#   • Node.js + npm  (frontend build)
#   • cargo          (binary build)  OR  Docker + buildx  (cross-compile)
#   • rsync + ssh    (always required)
#
# ── Prerequisites (remote host) ───────────────────────────────────────────────
#   • Docker + Compose plugin  (for Mosquitto + nginx support services)
#   • systemd                  (for the agentflow service unit)
#   • sudo access              (to install the unit and restart the service)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."   # always run from repo root

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD=$'\e[1m'; RESET=$'\e[0m'
GREEN=$'\e[32m'; CYAN=$'\e[36m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; DIM=$'\e[2m'

banner() { echo ""; echo "${BOLD}${GREEN}▶  $*${RESET}"; }
info()   { echo "   ${CYAN}$*${RESET}"; }
warn()   { echo "   ${YELLOW}⚠  $*${RESET}"; }
ok()     { echo "   ${GREEN}✓  $*${RESET}"; }
die()    { echo "   ${RED}✗  $*${RESET}"; exit 1; }

echo ""
echo "${BOLD}╔══════════════════════════════════════════════╗"
echo "║   AgentFlow — Deploy Wizard                  ║"
echo "╚══════════════════════════════════════════════╝${RESET}"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [ -f .env ]; then
    set -a; source .env; set +a
    info "Loaded .env"
elif [ -f .env.example ]; then
    warn ".env not found — loading .env.example (no secrets will be set)"
    set -a; source .env.example; set +a
else
    warn "No .env file found — all values must be set in the environment."
fi

# ── Required: DEPLOY_HOST ─────────────────────────────────────────────────────
DEPLOY_HOST="${DEPLOY_HOST:-}"
if [ -z "$DEPLOY_HOST" ]; then
    echo ""
    read -rp "   Target host [user@hostname]: " DEPLOY_HOST
    [ -z "$DEPLOY_HOST" ] && die "DEPLOY_HOST is required. Set it in .env or pass it as an env var."
fi

DEPLOY_PATH="${DEPLOY_PATH:-/opt/agentflow}"
DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-22}"
DEPLOY_RESTART_CMD="${DEPLOY_RESTART_CMD:-systemctl restart agentflow}"
DEPLOY_SKIP_BINARY="${DEPLOY_SKIP_BINARY:-0}"
DEPLOY_NGINX_MODE="${DEPLOY_NGINX_MODE:-docker}"
# Path on the remote where the snippet will be dropped (existing nginx mode).
# Adjust to match your nginx include pattern.
DEPLOY_NGINX_CONF="${DEPLOY_NGINX_CONF:-/etc/nginx/conf.d/agentflow.conf}"
NAUTILUS_STRICT_HOST_KEYS="${NAUTILUS_STRICT_HOST_KEYS:-0}"
NAUTILUS_CONNECT_TIMEOUT="${NAUTILUS_CONNECT_TIMEOUT:-10}"

info "Target  : ${DEPLOY_HOST}:${DEPLOY_PATH}"
info "SSH port: ${DEPLOY_SSH_PORT}"
info "nginx   : ${DEPLOY_NGINX_MODE}"

# ── SSH key setup ─────────────────────────────────────────────────────────────
banner "SSH key"
DEFAULT_KEY_PATH="$HOME/.ssh/agentflow_deploy"
DEPLOY_KEY="${NAUTILUS_SSH_KEY:-}"

if [ -z "$DEPLOY_KEY" ]; then
    if [ -f "$DEFAULT_KEY_PATH" ]; then
        DEPLOY_KEY="$DEFAULT_KEY_PATH"
        ok "Using existing deploy key: $DEPLOY_KEY"
    else
        echo ""
        echo "   No SSH key configured (NAUTILUS_SSH_KEY unset).  Choose:"
        echo "   ${BOLD}1)${RESET} Generate a new dedicated deploy key  →  ${DEFAULT_KEY_PATH}"
        echo "   ${BOLD}2)${RESET} Use SSH default key search order  (~/.ssh/id_ed25519 etc.)"
        echo "   ${BOLD}3)${RESET} Enter path manually"
        echo ""
        read -rp "   Choice [1]: " KEY_CHOICE
        KEY_CHOICE="${KEY_CHOICE:-1}"

        case "$KEY_CHOICE" in
            1)
                ssh-keygen -t ed25519 -C "agentflow-deploy-$(date +%Y%m%d)" \
                    -f "$DEFAULT_KEY_PATH" -N ""
                DEPLOY_KEY="$DEFAULT_KEY_PATH"
                ok "Generated: ${DEFAULT_KEY_PATH}  (${DEFAULT_KEY_PATH}.pub)"
                echo ""
                echo "   ${BOLD}Authorise the key on the remote host:${RESET}"
                echo "   ${DIM}ssh-copy-id -i ${DEFAULT_KEY_PATH}.pub -p ${DEPLOY_SSH_PORT} ${DEPLOY_HOST}${RESET}"
                echo ""
                echo "   ${DIM}Or manually:${RESET}"
                echo "   ${DIM}cat ${DEFAULT_KEY_PATH}.pub | ssh -p ${DEPLOY_SSH_PORT} ${DEPLOY_HOST} \\${RESET}"
                echo "   ${DIM}  'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'${RESET}"
                echo ""
                read -rp "   Press Enter once the public key is authorised on the remote host…"
                ;;
            2)
                DEPLOY_KEY=""
                info "Using default SSH key"
                ;;
            3)
                read -rp "   Key path: " DEPLOY_KEY
                [ -f "$DEPLOY_KEY" ] || die "Key not found: $DEPLOY_KEY"
                ;;
        esac
    fi
fi

# Build reusable SSH + rsync option strings
_SSH_OPTS="-p ${DEPLOY_SSH_PORT} -o ConnectTimeout=${NAUTILUS_CONNECT_TIMEOUT}"
[ "${NAUTILUS_STRICT_HOST_KEYS}" = "1" ] \
    || _SSH_OPTS="${_SSH_OPTS} -o StrictHostKeyChecking=accept-new"
[ -n "$DEPLOY_KEY" ] && _SSH_OPTS="${_SSH_OPTS} -i ${DEPLOY_KEY}"
RSYNC_SSH_E="ssh ${_SSH_OPTS}"

# Helper: run a command over SSH
ssh_run() { ssh ${_SSH_OPTS} "$DEPLOY_HOST" "$@"; }

# Derive the remote username (used for the systemd User= field)
if echo "$DEPLOY_HOST" | grep -q '@'; then
    REMOTE_USER="${DEPLOY_HOST%%@*}"
else
    REMOTE_USER="$(ssh_run whoami 2>/dev/null || echo "$USER")"
fi

# ── Test connectivity ─────────────────────────────────────────────────────────
banner "Testing SSH connectivity to ${DEPLOY_HOST}…"
if ssh_run exit 2>/dev/null; then
    ok "Connected  (remote user: ${REMOTE_USER})"
else
    die "Cannot connect to ${DEPLOY_HOST}. Check the host name, port, and key."
fi

# ── Build frontend ────────────────────────────────────────────────────────────
banner "Building frontend…"
[ -d frontend ] || die "frontend/ not found — run from repo root."
(cd frontend && npm install --silent && npm run build)
ok "static/app/ ready"

# ── Build Rust binary (optional) ─────────────────────────────────────────────
BINARY_SRC=""
if [ "${DEPLOY_SKIP_BINARY}" != "1" ]; then
    banner "Building Rust binary…"
    TARGET="${CARGO_BUILD_TARGET:-}"

    if command -v cargo >/dev/null 2>&1; then
        if [ -n "$TARGET" ]; then
            cargo build --release --bin agentflow \
                --target "$TARGET" \
                --manifest-path rust/Cargo.toml
            BINARY_SRC="rust/target/${TARGET}/release/agentflow"
        else
            cargo build --release --bin agentflow \
                --manifest-path rust/Cargo.toml
            BINARY_SRC="rust/target/release/agentflow"
        fi
        ok "Binary: ${BINARY_SRC}"

    elif command -v docker >/dev/null 2>&1; then
        warn "cargo not found — cross-building via Docker (linux/amd64)…"
        warn "This takes ~5–8 min on Apple Silicon."
        docker buildx build \
            --platform linux/amd64 \
            --tag agentflow-deploy-extract:tmp \
            --load ./rust
        CTNR=$(docker create --platform linux/amd64 agentflow-deploy-extract:tmp)
        docker cp "${CTNR}:/app/agentflow" /tmp/agentflow-deploy-bin
        docker rm "$CTNR" >/dev/null
        docker rmi agentflow-deploy-extract:tmp --force >/dev/null 2>&1 || true
        BINARY_SRC="/tmp/agentflow-deploy-bin"
        ok "Binary extracted (linux/amd64): ${BINARY_SRC}"

    else
        warn "Neither cargo nor docker found — skipping binary deploy."
        warn "Set DEPLOY_SKIP_BINARY=1 to suppress this warning, or install cargo/docker."
    fi
fi

# ── Prepare remote directories ────────────────────────────────────────────────
banner "Preparing ${DEPLOY_HOST}:${DEPLOY_PATH}…"
ssh_run "mkdir -p \
    ${DEPLOY_PATH}/frontend \
    ${DEPLOY_PATH}/infra/nginx \
    ${DEPLOY_PATH}/infra/mosquitto"
ok "Remote directories ready"

# ── Sync frontend ─────────────────────────────────────────────────────────────
banner "Syncing static/app/ → ${DEPLOY_HOST}:${DEPLOY_PATH}/static/app/"
rsync -az --delete \
    -e "${RSYNC_SSH_E}" \
    static/app/ \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/static/app/"
ok "Frontend synced"

# ── Deploy binary ─────────────────────────────────────────────────────────────
if [ -n "$BINARY_SRC" ] && [ -f "$BINARY_SRC" ]; then
    banner "Deploying binary → ${DEPLOY_HOST}:${DEPLOY_PATH}/agentflow"
    rsync -az \
        -e "${RSYNC_SSH_E}" \
        "$BINARY_SRC" \
        "${DEPLOY_HOST}:${DEPLOY_PATH}/agentflow"
    ssh_run "chmod +x ${DEPLOY_PATH}/agentflow"
    ok "Binary deployed"
fi

# ── Sync infra files ──────────────────────────────────────────────────────────
banner "Syncing infrastructure files…"

rsync -az \
    -e "${RSYNC_SSH_E}" \
    compose.native.yaml \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/compose.native.yaml"
ok "compose.native.yaml"

rsync -az \
    -e "${RSYNC_SSH_E}" \
    infra/nginx/nginx-native.conf \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/infra/nginx/nginx-native.conf"
ok "infra/nginx/nginx-native.conf"

rsync -az \
    -e "${RSYNC_SSH_E}" \
    infra/mosquitto/mosquitto.conf \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/infra/mosquitto/mosquitto.conf"
ok "infra/mosquitto/mosquitto.conf"

# ── Sync .env.example (preserve existing .env) ───────────────────────────────
rsync -az \
    -e "${RSYNC_SSH_E}" \
    .env.example \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/.env.example"
ssh_run "[ -f ${DEPLOY_PATH}/.env ] || cp ${DEPLOY_PATH}/.env.example ${DEPLOY_PATH}/.env"
ok ".env.example synced (existing .env preserved)"

# ── Start Mosquitto (always via Docker) ───────────────────────────────────────
banner "Starting Mosquitto…"
if ssh_run "command -v docker >/dev/null 2>&1"; then
    COMPOSE_CMD="docker compose"
    ssh_run "${COMPOSE_CMD} version >/dev/null 2>&1" || COMPOSE_CMD="docker-compose"

    if [ "${DEPLOY_NGINX_MODE}" = "existing" ]; then
        # Only start mosquitto — nginx is already running on the host
        ssh_run "cd ${DEPLOY_PATH} && ${COMPOSE_CMD} -f compose.native.yaml up -d mosquitto"
        ok "Mosquitto running (Docker nginx skipped — using host nginx)"
    else
        ssh_run "cd ${DEPLOY_PATH} && ${COMPOSE_CMD} -f compose.native.yaml up -d"
        ok "Mosquitto + nginx running"
    fi
else
    warn "Docker not found — skipping Mosquitto startup."
    warn "Install Docker or run: sudo apt install mosquitto mosquitto-clients"
fi

# ── Configure nginx ────────────────────────────────────────────────────────────
if [ "${DEPLOY_NGINX_MODE}" = "existing" ]; then
    banner "Configuring existing nginx…"

    # Build a ready-to-include conf file from the snippet, substituting the
    # actual DEPLOY_PATH for the frontend root
    PATCHED_SNIPPET="/tmp/agentflow-nginx-snippet.tmp"
    sed "s|/opt/agentflow|${DEPLOY_PATH}|g" \
        infra/nginx/agentflow-snippet.conf > "$PATCHED_SNIPPET"

    # Upload to home dir, then sudo-move to the nginx conf path
    rsync -az \
        -e "${RSYNC_SSH_E}" \
        "$PATCHED_SNIPPET" \
        "${DEPLOY_HOST}:~/agentflow-nginx-snippet.tmp"
    rm -f "$PATCHED_SNIPPET"

    ssh_run "sudo mv ~/agentflow-nginx-snippet.tmp ${DEPLOY_NGINX_CONF}"
    ok "Snippet deployed to ${DEPLOY_NGINX_CONF}"

    echo ""
    warn "The snippet contains location blocks — NOT a full server { } block."
    warn "Include it inside your existing SSL server block if not already done:"
    info "  include ${DEPLOY_NGINX_CONF};"
    echo ""

    if ssh_run "sudo nginx -t 2>/dev/null"; then
        ssh_run "sudo systemctl reload nginx"
        ok "nginx reloaded"
    else
        warn "nginx -t failed — check your config:"
        info "  sudo nginx -t"
        info "  sudo journalctl -u nginx -n 20"
    fi
fi

# ── Install / update systemd service ─────────────────────────────────────────
banner "Setting up systemd service…"

if ssh_run "command -v systemctl >/dev/null 2>&1"; then
    # Patch the unit file locally, upload to a temp location, then sudo-move it
    PATCHED_UNIT="/tmp/agentflow-deploy.service"
    sed \
        -e "s|WorkingDirectory=.*|WorkingDirectory=${DEPLOY_PATH}|" \
        -e "s|EnvironmentFile=.*|EnvironmentFile=${DEPLOY_PATH}/.env|" \
        -e "s|ExecStart=.*|ExecStart=${DEPLOY_PATH}/agentflow --no-cli|" \
        -e "s|User=%i|User=${REMOTE_USER}|" \
        systemd/agentflow.service > "$PATCHED_UNIT"

    # Upload to home dir first (no sudo needed for rsync)
    rsync -az \
        -e "${RSYNC_SSH_E}" \
        "$PATCHED_UNIT" \
        "${DEPLOY_HOST}:~/agentflow.service.tmp"
    rm -f "$PATCHED_UNIT"

    # Move to systemd directory and reload
    ssh_run "sudo mv ~/agentflow.service.tmp /etc/systemd/system/agentflow.service && \
             sudo systemctl daemon-reload"
    ok "Unit installed: /etc/systemd/system/agentflow.service"

    # Enable + start (first run) or restart (update)
    if ssh_run "systemctl is-active --quiet agentflow 2>/dev/null"; then
        ssh_run "sudo systemctl restart agentflow"
        ok "Service restarted"
    else
        ssh_run "sudo systemctl enable --now agentflow"
        ok "Service enabled and started"
    fi

    # Brief wait then show status
    sleep 2
    ssh_run "systemctl is-active agentflow && \
             echo '   agentflow is running' || \
             echo '   agentflow failed to start — check: journalctl -u agentflow -n 30'"
else
    warn "systemctl not found on remote — running binary in the foreground instead."
    warn "Open a second terminal and run:"
    info "  ssh${DEPLOY_KEY:+ -i $DEPLOY_KEY} -p ${DEPLOY_SSH_PORT} ${DEPLOY_HOST}"
    info "  cd ${DEPLOY_PATH} && source .env && ./agentflow --no-cli"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
HOST_DISPLAY="$(echo "$DEPLOY_HOST" | cut -d@ -f2)"
DASH_PORT="${DASHBOARD_EXTERNAL_PORT:-80}"
echo ""
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
printf  "║  ✓  Dashboard   http://%-28s║\n" "${HOST_DISPLAY}:${DASH_PORT}/"
printf  "║     Logs        journalctl -u agentflow -f%-9s║\n" ""
echo    "╠══════════════════════════════════════════════════════╣"
echo    "║  Future redeploys — from the AgentFlow dashboard:   ║"
echo    "║                                                      ║"
printf  "║  ${DIM}@nautilus-agent push ./static/app/ \\%-12s${RESET}${BOLD}${GREEN}║\n" ""
printf  "║  ${DIM}  %s:${DEPLOY_PATH}/static/app/%-2s${RESET}${BOLD}${GREEN}║\n" "${DEPLOY_HOST}" ""
printf  "║  ${DIM}@nautilus-agent exec %s \\%-15s${RESET}${BOLD}${GREEN}║\n" "${DEPLOY_HOST}" ""
printf  "║  ${DIM}  sudo systemctl restart agentflow%-18s${RESET}${BOLD}${GREEN}║\n" ""
echo    "╚══════════════════════════════════════════════════════╝${RESET}"
