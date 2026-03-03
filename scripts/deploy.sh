#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentFlow — deploy.sh
#
# Builds the frontend (and optionally the Rust binary) then deploys to a
# remote host via rsync + SSH.  Works without a running AgentFlow instance —
# useful for the initial bootstrap.
#
# After the first deploy you can skip this script entirely and redeploy
# directly from the AgentFlow dashboard using NautilusAgent:
#
#   @nautilus-agent push ./frontend/dist/ user@host:/opt/agentflow/frontend/dist/
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
#
#   NAUTILUS_SSH_KEY         path to SSH private key  (auto-generated if absent)
#   NAUTILUS_STRICT_HOST_KEYS  0 = accept-new (default)  1 = strict
#   NAUTILUS_CONNECT_TIMEOUT   seconds                (default: 10)
#
# ── Prerequisites (build machine) ─────────────────────────────────────────────
#   • Node.js + npm  (for frontend build)
#   • cargo          (for native binary)   OR
#   • Docker + buildx (for linux/amd64 cross-compiled binary)
#   • rsync + ssh    (always required)
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
NAUTILUS_STRICT_HOST_KEYS="${NAUTILUS_STRICT_HOST_KEYS:-0}"
NAUTILUS_CONNECT_TIMEOUT="${NAUTILUS_CONNECT_TIMEOUT:-10}"

info "Target  : ${DEPLOY_HOST}:${DEPLOY_PATH}"
info "SSH port: ${DEPLOY_SSH_PORT}"

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
                echo "   ${DIM}cat ${DEFAULT_KEY_PATH}.pub | ssh -p ${DEPLOY_SSH_PORT} ${DEPLOY_HOST} 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'${RESET}"
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

# ── Test connectivity ─────────────────────────────────────────────────────────
banner "Testing SSH connectivity to ${DEPLOY_HOST}…"
if ssh_run exit 2>/dev/null; then
    ok "Connected"
else
    die "Cannot connect to ${DEPLOY_HOST}. Check the host name, port, and key."
fi

# ── Build frontend ────────────────────────────────────────────────────────────
banner "Building frontend…"
[ -d frontend ] || die "frontend/ not found — run from repo root."
(cd frontend && npm install --silent && npm run build)
ok "frontend/dist/ ready"

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

# ── Prepare remote directory ──────────────────────────────────────────────────
banner "Preparing ${DEPLOY_HOST}:${DEPLOY_PATH}…"
ssh_run "mkdir -p ${DEPLOY_PATH}/frontend"
ok "Remote directories ready"

# ── Sync frontend ─────────────────────────────────────────────────────────────
banner "Syncing frontend/dist/ → ${DEPLOY_HOST}:${DEPLOY_PATH}/frontend/dist/"
rsync -az --delete \
    -e "${RSYNC_SSH_E}" \
    frontend/dist/ \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/frontend/dist/"
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

# ── Sync .env.example (preserve existing .env) ───────────────────────────────
banner "Syncing config template…"
rsync -az \
    -e "${RSYNC_SSH_E}" \
    .env.example \
    "${DEPLOY_HOST}:${DEPLOY_PATH}/.env.example"
# Create .env from example only if it doesn't already exist
ssh_run "[ -f ${DEPLOY_PATH}/.env ] || cp ${DEPLOY_PATH}/.env.example ${DEPLOY_PATH}/.env"
ok ".env.example synced (existing .env preserved)"

# ── Restart service ───────────────────────────────────────────────────────────
banner "Restarting service…"
if ssh_run "command -v systemctl >/dev/null 2>&1 && \
            systemctl list-unit-files agentflow.service 2>/dev/null | grep -q agentflow"; then
    ssh_run "sudo ${DEPLOY_RESTART_CMD}"
    ok "Service restarted (systemctl)"
elif ssh_run "command -v docker >/dev/null 2>&1 && \
              [ -f ${DEPLOY_PATH}/compose.native.yaml ]"; then
    warn "systemd service not found — restarting via docker compose (native)…"
    ssh_run "cd ${DEPLOY_PATH} && docker compose -f compose.native.yaml restart 2>/dev/null || true"
else
    warn "Could not auto-restart.  Manually:"
    info "  ssh${DEPLOY_KEY:+ -i $DEPLOY_KEY} -p ${DEPLOY_SSH_PORT} ${DEPLOY_HOST}"
    info "  cd ${DEPLOY_PATH} && source .env && ./agentflow --no-cli &"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
HOST_DISPLAY="$(echo "$DEPLOY_HOST" | cut -d@ -f2)"
DASH_PORT="${DASHBOARD_EXTERNAL_PORT:-80}"
echo ""
echo "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
printf  "║  ✓  Dashboard  http://%-30s║\n" "${HOST_DISPLAY}:${DASH_PORT}/"
echo    "╠══════════════════════════════════════════════════════╣"
echo    "║  Future redeploys — from the AgentFlow dashboard:   ║"
printf  "║  ${DIM}@nautilus-agent push ./frontend/dist/ %-14s${RESET}${BOLD}${GREEN}║\n" "\\"
printf  "║  ${DIM}  %s:${DEPLOY_PATH}/frontend/dist/%-4s${RESET}${BOLD}${GREEN}║\n" "${DEPLOY_HOST}" ""
printf  "║  ${DIM}@nautilus-agent exec %s %-17s${RESET}${BOLD}${GREEN}║\n" "${DEPLOY_HOST}" "\\"
printf  "║  ${DIM}  sudo ${DEPLOY_RESTART_CMD}%-22s${RESET}${BOLD}${GREEN}║\n" ""
echo    "╚══════════════════════════════════════════════════════╝${RESET}"
