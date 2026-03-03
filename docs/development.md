# Development

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Rust | ≥ 1.93 | Backend |
| Node.js | ≥ 20 | Frontend build |
| npm | ≥ 10 | Frontend deps |
| Docker + Compose | any recent | Dev stack |
| mosquitto-clients | optional | MQTT debugging |

---

## Quick start (frontend-only, no Rust build)

The fastest way to develop the UI is against the mock agent simulator.

```bash
# Terminal 1 — MQTT broker + mock agents
docker compose -f compose.dev.yaml up -d

# Terminal 2 — Vite dev server (hot-reload on http://localhost:3000)
cd frontend
npm install
npm run dev
```

The mock simulator (`scripts/mock-agents.mjs`) publishes realistic MQTT events:
- 6 agents: main-actor, monitor-agent, data-fetcher, weather-agent, ml-classifier, nautilus-agent, udx-agent
- Heartbeats every 5 s
- Chat replies every 4 s
- Occasional alerts (30% chance every 8 s)
- Dynamic agent spawns every 20 s (20% chance)

The Vite dev server proxies `/api/`, `/ws`, and `/mqtt` to the local ports exposed by `compose.dev.yaml`.

### Stop mock stack

```bash
docker compose -f compose.dev.yaml down
```

---

## Full stack (Rust backend)

```bash
# Terminal 1 — support services
docker compose up -d mosquitto dashboard   # or 'docker compose up -d'

# Terminal 2 — Rust backend
cd rust
RUST_LOG=agentflow=debug cargo run --bin agentflow -- \
    --mqtt-host localhost \
    --llm-provider anthropic \
    --llm-api-key "$LLM_API_KEY"

# Terminal 3 — frontend dev server
cd frontend && npm run dev
```

---

## Project structure

```
agentflow/
├── rust/                          Rust workspace
│   ├── Cargo.toml                 workspace manifest
│   ├── Dockerfile                 multi-stage: builder → runtime
│   └── crates/
│       ├── agentflow-core/        Actor trait, registry, messages
│       ├── agentflow-agents/      All concrete agents
│       ├── agentflow-mqtt/        MQTT client + topic helpers
│       ├── agentflow-interfaces/  REST, WebSocket, CLI
│       └── agentflow-server/      Binary entry point
│
├── frontend/                      Vite + TypeScript + Babylon.js SPA
│   ├── src/
│   │   ├── main.ts                App bootstrap, wires all components
│   │   ├── types/agent.ts         Shared TypeScript types
│   │   ├── mqtt/MQTTClient.ts     MQTT client, event routing
│   │   ├── io/
│   │   │   ├── IOManager.ts       Sends messages to io/chat
│   │   │   ├── IOBar.ts           Input bar (history, multiline, @mention)
│   │   │   ├── AgentImageGen.ts   DiceBear + Gemini avatar generation
│   │   │   └── VoiceInput.ts      Web Speech API integration
│   │   ├── scene/
│   │   │   ├── SceneManager.ts    Babylon.js lifecycle, theme switching
│   │   │   ├── themes/            GraphTheme, GalaxyTheme, GraveTheme,
│   │   │   │                        CardBabylonTheme, ThemeBase
│   │   │   ├── nodes/             GraphNode, PlanetNode, GraveNode,
│   │   │   │                        AgentNodeBase, CardBabylonNode
│   │   │   └── effects/           AlertEffect, HeartbeatEffect,
│   │   │                            MessageEffect, SpawnEffect
│   │   └── ui/
│   │       ├── ChatPanel.ts       Per-agent chat threads
│   │       ├── SocialDashboard.ts Instagram/Twitter-style card grid
│   │       ├── CardDashboard.ts   Minimal HTML card grid
│   │       ├── ActivityFeed.ts    Collapsible MQTT event log
│   │       ├── MentionPopup.ts    @mention autocomplete
│   │       ├── AgentHUD.ts        Top-left agent count HUD
│   │       └── ThemeSwitcher.ts   Theme toggle buttons
│   ├── public/
│   │   ├── favicon.svg
│   │   ├── site.webmanifest
│   │   └── robots.txt
│   ├── index.html                 All CSS is inline here (no external CSS files)
│   ├── vite.config.ts
│   └── tsconfig.json
│
├── infra/
│   ├── nginx/
│   │   ├── nginx.conf             Full Docker mode
│   │   └── nginx-native.conf      Native binary mode
│   ├── mosquitto/mosquitto.conf
│   ├── fuseki/                    Apache Jena Fuseki
│   └── homeassistant/             HA configuration
│
├── scripts/
│   ├── deploy.sh                  Build + rsync + restart wizard
│   ├── mock-agents.mjs            Dev-mode mock MQTT simulator
│   ├── package-full-release.sh    Build full zip release
│   ├── package-native.sh          Build native binary tar.gz
│   └── build-native.sh            Build binary on the target host
│
├── systemd/agentflow.service      systemd unit template
├── compose.yaml                   Full Docker stack
├── compose.dev.yaml               Dev stack (mosquitto + mock only)
├── compose.native.yaml            Native binary stack
├── .env.example                   Annotated config template
└── docs/                          This documentation
```

---

## Rust development

```bash
cd rust

# Check (fast — no codegen)
cargo check

# Build debug
cargo build --bin agentflow

# Build release
cargo build --release --bin agentflow

# Run with debug logging
RUST_LOG=agentflow=debug cargo run --bin agentflow

# Lint
cargo clippy -- -D warnings

# Format
cargo fmt
```

### Cross-compile for Linux from macOS

```bash
# Install target
rustup target add x86_64-unknown-linux-gnu

# Install linker (Homebrew)
brew install FiloSottile/musl-cross/musl-cross

# Build
CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-musl-gcc \
cargo build --release --target x86_64-unknown-linux-gnu --bin agentflow
```

Alternatively, use Docker buildx (no linker needed):

```bash
docker buildx build --platform linux/amd64 --tag agentflow:local --load ./rust
```

---

## Frontend development

```bash
cd frontend

# Install deps
npm install

# Dev server (hot-reload, http://localhost:3000)
npm run dev

# Type-check only
npm run typecheck   # or: npx tsc --noEmit

# Production build
npm run build
# → frontend/dist/

# Preview production build locally
npm run preview
```

### Vite dev proxy

```typescript
// vite.config.ts
proxy: {
  "/api":  { target: "http://localhost:8080", ... },
  "/ws":   { target: "ws://localhost:8081",  ws: true },
  "/mqtt": { target: "ws://localhost:9001",  ws: true },
}
```

This means in dev, `window.location.host` resolves to `localhost:3000`, and the MQTT URL is automatically `ws://localhost:3000/mqtt` → proxied to Mosquitto.

### Adding a new UI theme

1. Create `frontend/src/scene/themes/MyTheme.ts` extending `ThemeBase`
2. Add a node type in `frontend/src/scene/nodes/`
3. Register in `SceneManager.ts`:
   ```typescript
   case "my-theme": this.activeTheme = new MyTheme(this.scene, ...); break;
   ```
4. Add a button in `index.html` and `ThemeSwitcher.ts`

---

## Debugging tips

### MQTT messages not arriving in the browser

```bash
# Subscribe to all topics from the terminal
mosquitto_sub -h localhost -p 1883 -t '#' -v

# Or check the mock is publishing
docker compose -f compose.dev.yaml logs mock-agents
```

### Rust actor not responding

```bash
RUST_LOG=agentflow=debug cargo run --bin agentflow
# Look for: "[agent-name] handle_message" or heartbeat logs
```

### TypeScript errors

```bash
cd frontend && npx tsc --noEmit
```

### MQTT URL wrong in browser

Check browser console — the `MQTTClient` logs its connection URL.  In dev it should be `ws://localhost:3000/mqtt`.  Override with `VITE_MQTT_WS_URL` in `.env` if needed.
