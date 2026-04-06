# Deployment

Wactorz supports two deployment modes:

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
git clone https://github.com/waldiez/wactorz
cd wactorz
cp .env.example .env
nano .env           # set LLM_API_KEY at minimum
docker compose up -d
```

Open `http://localhost/` (or `http://localhost:80/`).

### Services

| Service | Internal address | Public path |
|---|---|---|
| nginx (dashboard + proxy) | — | `:80` |
| wactorz | `wactorz:8080` / `:8081` | `/api/`, `/ws` |
| mosquitto | `mosquitto:1883` / `:9001` | `/mqtt` |
| fuseki | `fuseki:3030` | `/fuseki/` |
| home-assistant | `homeassistant:8123` | `:8123` |

---

## Native binary  (`compose.native.yaml`)

Only Mosquitto and nginx run in Docker.  The `wactorz` binary runs directly on the host OS.

### Advantages

| | Full Docker | Native binary |
|---|---|---|
| SSH keys (NautilusAgent) | Needs volume mounts | `~/.ssh/` works automatically |
| Cold start | Container init | `< 100 ms` |
| Binary size | 39 MB image | ~12 MB binary |
| Cross-compile | buildx + QEMU | `cargo build --target …` |

### Prerequisites

- Docker + Compose plugin (for Mosquitto + nginx)
- The `wactorz` binary (see below)

### Bootstrap (first deploy)

#### Option A — use the package script

```bash
# On the build machine:
bash scripts/package-native.sh
# → wactorz-native-YYYYMMDD.tar.gz

# Transfer to target host:
scp wactorz-native-*.tar.gz user@host:~/
ssh user@host
tar xzf wactorz-native-*.tar.gz
cd wactorz-native-*/
bash deploy-native.sh        # interactive wizard
```

#### Option B — use `scripts/deploy.sh`

```bash
# 1. Configure .env
cp .env.example .env
nano .env
# Set: LLM_API_KEY, DEPLOY_HOST, DEPLOY_PATH, NAUTILUS_SSH_KEY
# If the remote already has nginx running (certbot/SSL), also set:
#   DEPLOY_NGINX_MODE=existing

# 2. Run the deploy wizard (builds frontend + binary, rsyncs, restarts)
bash scripts/deploy.sh
```

The wizard will:
1. Check / generate an SSH key (`~/.ssh/wactorz_deploy`)
2. Build the frontend (`npm run build`)
3. Build the binary via `cargo build --release` or Docker buildx
4. rsync `static/app/` and the binary to the remote host
5. Create `.env` from `.env.example` on the remote (preserves existing)
6. Start Mosquitto via Docker + configure nginx (see modes below)
7. Install + start the `wactorz` systemd service

#### nginx modes

| `DEPLOY_NGINX_MODE` | What happens |
|---|---|
| `docker` (default) | Starts the Docker nginx container from `compose.native.yaml` on port 80 |
| `existing` | Skips Docker nginx; uploads `infra/nginx/wactorz-snippet.conf` to `DEPLOY_NGINX_CONF` on the remote and reloads the host nginx |

**If you already have nginx running (e.g. with certbot/SSL):**

```bash
# In your local .env:
DEPLOY_NGINX_MODE=existing
DEPLOY_NGINX_CONF=/etc/nginx/conf.d/wactorz.conf   # adjust if needed

# Run deploy normally:
bash scripts/deploy.sh
```

Then, on the remote, include the snippet inside your SSL `server { }` block (once):

```nginx
# /etc/nginx/sites-enabled/your-site.conf  (inside server { } block)
include /etc/nginx/conf.d/wactorz.conf;
```

After `sudo nginx -t && sudo systemctl reload nginx`, the dashboard is live at your existing HTTPS URL.

**Important: MQTT_HOST must be `localhost` in native mode.**
The wactorz binary connects to Mosquitto on `localhost:1883`.
If you copied `.env` from a Docker setup, change `MQTT_HOST=mosquitto` → `MQTT_HOST=localhost`.

### Subsequent deploys — from the Wactorz dashboard

Once the system is running, use **NautilusAgent** from the IO bar:

```
# Frontend only (fastest — no binary rebuild needed)
@nautilus-agent push ./static/app/ deploy@host:/opt/wactorz/static/app/
@nautilus-agent exec deploy@host sudo systemctl restart wactorz

# Binary + frontend
@nautilus-agent push /path/to/wactorz deploy@host:/opt/wactorz/wactorz
@nautilus-agent exec deploy@host chmod +x /opt/wactorz/wactorz
@nautilus-agent exec deploy@host sudo systemctl restart wactorz
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
sudo cp systemd/wactorz.service /etc/systemd/system/
sudo nano /etc/systemd/system/wactorz.service
# Edit: WorkingDirectory, EnvironmentFile, ExecStart, User

sudo systemctl daemon-reload
sudo systemctl enable --now wactorz
journalctl -u wactorz -f
```

The unit template at `systemd/wactorz.service` has comments for every field.

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
| `PROMETHEUS_EXTERNAL_PORT` | `9090` | Prometheus host port |
| `PROMETHEUS_SCRAPE_INTERVAL` | `15s` | Global Prometheus scrape interval |
| `PROMETHEUS_MONITOR_MOSQUITTO` | `1` | Enable Mosquitto TCP availability probe |
| `PROMETHEUS_MONITOR_FUSEKI` | `0` | Enable Fuseki HTTP availability probe |
| `DASHBOARD_EXTERNAL_PORT` | `80` | nginx host port |
| `NAUTILUS_SSH_KEY` | _(default key)_ | Path to SSH private key |
| `NAUTILUS_STRICT_HOST_KEYS` | `0` | `1` = enforce strict host-key checking |
| `NAUTILUS_CONNECT_TIMEOUT` | `10` | SSH timeout in seconds |
| `DEPLOY_HOST` | _(required for deploy.sh)_ | `user@hostname` |
| `DEPLOY_PATH` | `/opt/wactorz` | Remote base directory |
| `DEPLOY_SSH_PORT` | `22` | SSH port on remote host |
| `DEPLOY_RESTART_CMD` | `systemctl restart wactorz` | Service restart command |
| `DEPLOY_SKIP_BINARY` | `0` | `1` = frontend-only deploy |
| `DEPLOY_NGINX_MODE` | `docker` | `docker` or `existing` (host nginx already running) |
| `DEPLOY_NGINX_CONF` | `/etc/nginx/conf.d/wactorz.conf` | Remote path for the nginx snippet |
| `CARGO_BUILD_TARGET` | _(host arch)_ | e.g. `x86_64-unknown-linux-gnu` |
| `RUST_LOG` | `wactorz=info` | Logging filter |

---

## SSH key management

Generate a dedicated deploy key (recommended):

```bash
ssh-keygen -t ed25519 -C "wactorz-deploy" -f ~/.ssh/wactorz_deploy -N ""

# Authorise on the target host
ssh-copy-id -i ~/.ssh/wactorz_deploy.pub -p 22 user@host

# Add to .env
echo "NAUTILUS_SSH_KEY=~/.ssh/wactorz_deploy" >> .env
```

`scripts/deploy.sh` will generate the key interactively if `NAUTILUS_SSH_KEY` is unset and `~/.ssh/wactorz_deploy` does not exist.

---

## Updating Home Assistant integration

Wactorz can send REST commands to Home Assistant and receive automations.

```yaml
# infra/homeassistant/configuration.yaml
rest_command:
  wactorz_chat:
    url: "http://wactorz:8080/api/chat"
    method: POST
    content_type: "application/json"
    payload: '{"to":"main-actor","content":"{{ message }}"}'
```

Set `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` in `.env`.
