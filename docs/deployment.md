# Deployment

AgentFlow supports two deployment modes:

| Mode | When to use |
|---|---|
| **Full Docker** | Simplest; everything in containers |
| **Native binary** | Better SSH key access, faster cold start, smaller footprint |

---

## Full Docker  (`compose.yaml`)

### Prerequisites

- Docker + Compose plugin
- `LLM_API_KEY` (Anthropic / OpenAI) or a local Ollama instance

### Steps

```bash
git clone https://github.com/waldiez/agentflow
cd agentflow
cp .env.example .env
nano .env           # set LLM_API_KEY at minimum
docker compose up -d
```

Open `http://localhost/` (or `http://localhost:80/`).

### Services

| Service | Internal address | Public path |
|---|---|---|
| nginx (dashboard + proxy) | — | `:80` |
| agentflow | `agentflow:8080` / `:8081` | `/api/`, `/ws` |
| mosquitto | `mosquitto:1883` / `:9001` | `/mqtt` |
| fuseki | `fuseki:3030` | `/fuseki/` |
| home-assistant | `homeassistant:8123` | `:8123` |

---

## Native binary  (`compose.native.yaml`)

Only Mosquitto and nginx run in Docker.  The `agentflow` binary runs directly on the host OS.

### Advantages

| | Full Docker | Native binary |
|---|---|---|
| SSH keys (NautilusAgent) | Needs volume mounts | `~/.ssh/` works automatically |
| Cold start | Container init | `< 100 ms` |
| Binary size | 39 MB image | ~12 MB binary |
| Cross-compile | buildx + QEMU | `cargo build --target …` |

### Prerequisites

- Docker + Compose plugin (for Mosquitto + nginx)
- The `agentflow` binary (see below)

### Bootstrap (first deploy)

#### Option A — use the package script

```bash
# On the build machine:
bash scripts/package-native.sh
# → agentflow-native-YYYYMMDD.tar.gz

# Transfer to target host:
scp agentflow-native-*.tar.gz user@host:~/
ssh user@host
tar xzf agentflow-native-*.tar.gz
cd agentflow-native-*/
bash deploy-native.sh        # interactive wizard
```

#### Option B — use `scripts/deploy.sh`

```bash
# 1. Configure .env
cp .env.example .env
nano .env
# Set: LLM_API_KEY, DEPLOY_HOST, DEPLOY_PATH, NAUTILUS_SSH_KEY

# 2. Run the deploy wizard (builds frontend + binary, rsyncs, restarts)
bash scripts/deploy.sh
```

The wizard will:
1. Check / generate an SSH key (`~/.ssh/agentflow_deploy`)
2. Build the frontend (`npm run build`)
3. Build the binary via `cargo build --release` or Docker buildx
4. rsync `frontend/dist/` and the binary to the remote host
5. Create `.env` from `.env.example` on the remote (preserves existing)
6. Restart the service via systemd or docker compose

### Subsequent deploys — from the AgentFlow dashboard

Once the system is running, use **NautilusAgent** from the IO bar:

```
# Frontend only (fastest — no binary rebuild needed)
@nautilus-agent push ./frontend/dist/ deploy@host:/opt/agentflow/frontend/dist/
@nautilus-agent exec deploy@host sudo systemctl restart agentflow

# Binary + frontend
@nautilus-agent push /path/to/agentflow deploy@host:/opt/agentflow/agentflow
@nautilus-agent exec deploy@host chmod +x /opt/agentflow/agentflow
@nautilus-agent exec deploy@host sudo systemctl restart agentflow
```

Or re-run the script locally:

```bash
DEPLOY_SKIP_BINARY=1 bash scripts/deploy.sh   # frontend-only redeploy
bash scripts/deploy.sh                         # full redeploy
```

---

## systemd service (persistent, starts on boot)

```bash
# On the target host (after initial deploy):
sudo cp systemd/agentflow.service /etc/systemd/system/
sudo nano /etc/systemd/system/agentflow.service
# Edit: WorkingDirectory, EnvironmentFile, ExecStart, User

sudo systemctl daemon-reload
sudo systemctl enable --now agentflow
journalctl -u agentflow -f
```

The unit template at `systemd/agentflow.service` has comments for every field.

---

## Environment variables

See `.env.example` for the full annotated list.  The most important ones:

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `ollama` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Any model ID |
| `LLM_API_KEY` | _(required)_ | API key |
| `MQTT_HOST` | `localhost` | Use `mosquitto` inside Docker |
| `MQTT_PORT` | `1883` | |
| `API_ADDR` | `0.0.0.0:8080` | REST listen address |
| `WS_ADDR` | `0.0.0.0:8081` | WS bridge listen address |
| `DASHBOARD_EXTERNAL_PORT` | `80` | nginx host port |
| `NAUTILUS_SSH_KEY` | _(default key)_ | Path to SSH private key |
| `NAUTILUS_STRICT_HOST_KEYS` | `0` | `1` = enforce strict host-key checking |
| `NAUTILUS_CONNECT_TIMEOUT` | `10` | SSH timeout in seconds |
| `DEPLOY_HOST` | _(required for deploy.sh)_ | `user@hostname` |
| `DEPLOY_PATH` | `/opt/agentflow` | Remote base directory |
| `DEPLOY_SSH_PORT` | `22` | SSH port on remote host |
| `DEPLOY_RESTART_CMD` | `systemctl restart agentflow` | Service restart command |
| `DEPLOY_SKIP_BINARY` | `0` | `1` = frontend-only deploy |
| `CARGO_BUILD_TARGET` | _(host arch)_ | e.g. `x86_64-unknown-linux-gnu` |
| `RUST_LOG` | `agentflow=info` | Logging filter |

---

## SSH key management

Generate a dedicated deploy key (recommended):

```bash
ssh-keygen -t ed25519 -C "agentflow-deploy" -f ~/.ssh/agentflow_deploy -N ""

# Authorise on the target host
ssh-copy-id -i ~/.ssh/agentflow_deploy.pub -p 22 user@host

# Add to .env
echo "NAUTILUS_SSH_KEY=~/.ssh/agentflow_deploy" >> .env
```

`scripts/deploy.sh` will generate the key interactively if `NAUTILUS_SSH_KEY` is unset and `~/.ssh/agentflow_deploy` does not exist.

---

## Updating Home Assistant integration

AgentFlow can send REST commands to Home Assistant and receive automations.

```yaml
# infra/homeassistant/configuration.yaml
rest_command:
  agentflow_chat:
    url: "http://agentflow:8080/api/chat"
    method: POST
    content_type: "application/json"
    payload: '{"to":"main-actor","content":"{{ message }}"}'
```

Set `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` in `.env`.
