# AgentFlow

A real-time, async **multi-agent orchestration system** built on the **Actor Model** with MQTT pub/sub.  Agents are spawned on the fly by an LLM orchestrator and visualised in an immersive **Babylon.js 3D dashboard**.

```
Browser (Babylon.js SPA)
  │  WebSocket · REST · MQTT/WS
  ▼
nginx  ──────────────────────────────────────────── single public entry point
  ├── /         → frontend SPA
  ├── /api/     → agentflow REST  :8080
  ├── /ws       → agentflow WS    :8081
  ├── /mqtt     → Mosquitto WS    :9001
  └── /fuseki/  → Apache Jena Fuseki

agentflow (Rust binary)
  ├── MainActor        LLM orchestrator · spawns agents dynamically
  ├── MonitorAgent     Health watchdog · stale-actor alerts
  ├── IOAgent          UI gateway · routes @mentions to actors
  ├── QAAgent          Safety observer · inspects all chat traffic
  ├── NautilusAgent    SSH/rsync bridge · deploy from the dashboard
  ├── UDXAgent         Built-in knowledge base · instant docs
  └── DynamicAgent*    LLM-generated Rhai scripts · spawned at runtime
```

---

## Features

- **Dynamic agent spawning** — describe what you want; the LLM writes the code and spawns a live actor
- **Actor Model** — isolated mailboxes, no shared state, zero data races
- **MQTT backbone** — every event is a pub/sub message; browser stays in sync without polling
- **5 dashboard views** — 3D Graph, Galaxy, Card grid, Social cards, Graveyard (tombstones for stopped agents)
- **Typing indicators + markdown** — chat panel renders `**bold**`, `` `code` ``, fenced blocks
- **@mention routing** — `@agent-name text` in the IO bar routes directly to any agent
- **NautilusAgent** — SSH ping/exec/sync/push from the chat; deploy the system from inside itself
- **UDXAgent** — instant answers about the system; no LLM key needed
- **Native binary mode** — run the Rust binary directly on the host; Docker only for Mosquitto + nginx
- **PWA-ready** — SVG favicon, web manifest, dark theme-color

---

## Quick start

### Option A — Full Docker (simplest)

```bash
git clone https://github.com/waldiez/agentflow
cd agentflow
cp .env.example .env
nano .env          # set LLM_API_KEY at minimum
docker compose up -d
```

Open **http://localhost/** and start chatting.

### Option B — Dev mode (no LLM, no Rust build)

```bash
# Terminal 1 — MQTT broker + mock agents
docker compose -f compose.dev.yaml up -d

# Terminal 2 — Vite hot-reload dev server
cd frontend && npm install && npm run dev
# → http://localhost:3000
```

6 mock agents appear immediately.  Chat, heartbeats, alerts, and dynamic spawns are all simulated.

### Option C — Native binary

```bash
bash scripts/package-native.sh          # builds agentflow-native-*.tar.gz
scp agentflow-native-*.tar.gz user@host:~/
ssh user@host 'tar xzf agentflow-native-*.tar.gz && cd agentflow-native-*/ && bash deploy-native.sh'
```

Or use the deploy wizard directly:

```bash
cp .env.example .env
nano .env   # set LLM_API_KEY + DEPLOY_HOST
bash scripts/deploy.sh
```

---

## Dashboard views

Switch with the buttons in the top-right corner.

| View | Description |
|---|---|
| **3D Graph** | Spring-force layout; glowing spheres; Bezier chat arcs animate between nodes |
| **Galaxy** | Orbital; main-actor at the centre, others orbit as planets |
| **Cards** | Classic HTML card grid; lightest on low-end devices |
| **Social** | Instagram × Twitter hybrid; profile cards with avatar, bio, stats, controls |
| **Graveyard** | Stopped/failed agents shown as tombstones with RIP dates |

Controls per card: **💬 Chat · ⏸ Pause · ▶ Resume · ⏹ Stop · 🗑 Delete** (protected agents ⭐ cannot be stopped)

---

## Chat

Open a chat thread by clicking any agent card or 3D node.

```
@main-actor explain the actor model
@nautilus-agent ping deploy@myserver.com
@nautilus-agent exec deploy@myserver.com df -h
@udx-agent docs deployment
@udx-agent explain mqtt
```

- **Shift+Enter** — newline; **Enter** — send
- **↑ / ↓** — message history
- **Swipe right** / **Escape** — close chat panel
- **3-dot indicator** — agent is processing

---

## Agent roster

| Agent | Type | Protected | What it does |
|---|---|---|---|
| `main-actor` | orchestrator | ⭐ | LLM brain; parses `<spawn>` blocks to create new agents |
| `monitor-agent` | monitor | ⭐ | Polls all actors every 15 s; alerts on 60 s silence |
| `io-agent` | gateway | | Routes `io/chat` to actors by `@mention` |
| `qa-agent` | qa | ⭐ | Passively inspects all chat for policy violations |
| `nautilus-agent` | transfer | | SSH + rsync: `ping`, `exec`, `sync`, `push` |
| `udx-agent` | expert | | Built-in docs: `help`, `explain`, `docs`, `agents`, `status` |
| `dynamic-*` | dynamic | | LLM-generated Rhai scripts, spawned at runtime |

---

## Deploying updates from the dashboard

Once the initial deploy is done, use **NautilusAgent** for all future redeploys:

```
# Frontend-only (no binary rebuild — fastest)
@nautilus-agent push ./frontend/dist/ user@host:/opt/agentflow/frontend/dist/
@nautilus-agent exec user@host sudo systemctl restart agentflow

# Binary + frontend
@nautilus-agent push ./rust/target/release/agentflow user@host:/opt/agentflow/agentflow
@nautilus-agent exec user@host chmod +x /opt/agentflow/agentflow
@nautilus-agent exec user@host sudo systemctl restart agentflow
```

---

## Configuration

Copy `.env.example` to `.env` and fill in values.  Key settings:

```bash
# LLM
LLM_PROVIDER=anthropic          # anthropic | openai | ollama
LLM_MODEL=claude-sonnet-4-6
LLM_API_KEY=sk-ant-...

# NautilusAgent SSH
NAUTILUS_SSH_KEY=~/.ssh/agentflow_deploy
NAUTILUS_STRICT_HOST_KEYS=0

# Deployment target (for scripts/deploy.sh)
DEPLOY_HOST=user@myserver.com
DEPLOY_PATH=/opt/agentflow
```

---

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System topology, actor model, message flow, IDs |
| [docs/agents.md](docs/agents.md) | Every agent in detail; how to add a new one |
| [docs/windows.md](docs/mqtt_topics.md) | Every mqtt topic in detail |
| [docs/deployment.md](docs/deployment.md) | Bootstrap, native mode, systemd, SSH keys, env vars |
| [docs/development.md](docs/development.md) | Dev setup, project structure, debugging tips |
| [docs/api.md](docs/api.md) | REST endpoints, MQTT topic reference, WebSocket format |
| [docs/windows.md](docs/windows.md) | Windows (x86-64 + ARM64) — Docker, dev mode, native binary |

---

## Scripts

| Script | Purpose |
|---|---|
| `scripts/deploy.sh` | Build + rsync + restart wizard (SSH key gen, binary build, remote deploy) |
| `scripts/mock-agents.mjs` | Dev-mode MQTT simulator — no LLM or Rust build needed |
| `scripts/package-full-release.sh` | Build `agentflow-full-*.zip` (full source + built artifacts) |
| `scripts/package-native.sh` | Build `agentflow-native-*.tar.gz` (binary + SPA + compose.native.yaml) |
| `scripts/build-native.sh` | Build binary natively on the target host (requires Rust) |

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Rust 1.93+, Tokio, axum, rumqttc, serde_json, clap, tracing |
| Agent IDs | [`waldiez-wid`](https://github.com/waldiez/wid) — HLC-WID |
| Scripting | Rhai (DynamicAgent) |
| Frontend | Vite, TypeScript, Babylon.js 7.x, mqtt.js |
| Broker | Eclipse Mosquitto |
| Proxy | nginx |
| RDF store | Apache Jena Fuseki (optional) |
| Home automation | Home Assistant (optional) |

---

## License

MIT
