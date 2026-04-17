# Wactorz Frontend

Vite + TypeScript single-page application that visualises a running Wactorz agent system.
Renders a live dashboard over a Babylon.js canvas with HTML/CSS overlays.

## Stack

| Layer | Library | Purpose |
|-------|---------|---------|
| 3D engine | Babylon.js 8 | Galaxy / graph themes |
| Transport | MQTT.js 5 | Real-time agent events |
| Chat bridge | WebSocket (native) | Direct `main` agent replies |
| IDs | `@waldiez/wid` | Time-ordered collision-resistant IDs |
| Build | Vite 8 + TypeScript 5.9 | Strict mode, ES2022 target |

## Directory layout

```
frontend/
├── src/
│   ├── main.ts            # Bootstrap — wires everything together
│   ├── types/agent.ts     # Shared types (AgentInfo, ChatMessage, …)
│   ├── mqtt/              # MQTT WebSocket client + typed event emitter
│   ├── io/                # IOManager, WSChatClient, TTS, VoiceInput, HAClient
│   ├── scene/             # Babylon.js engine, themes, agent nodes, effects
│   └── ui/                # HTML overlay components (no framework)
├── index.html
├── vite.config.ts         # Dev proxy → :8000 (REST) + :8081 (WS) + :9001 (MQTT)
└── tsconfig.json          # Strict, noUncheckedIndexedAccess, exactOptionalPropertyTypes
```

## Quick start

```bash
# From repo root — install + start Vite dev server
make install-frontend
make dev-ui          # needs mosquitto already running (make dev)

# Or directly
cd frontend
bun install
bun run dev          # http://localhost:3000
```

## Available scripts

| Script | What it does |
|--------|-------------|
| `bun run dev` | Vite dev server on :3000 with proxy to :8000/:8081/:9001 |
| `bun run build` | TypeScript check + Vite bundle → `../static/app` |
| `bun run typecheck` | `tsc --noEmit` only |
| `bun run fmt` | Prettier over `src/**/*.ts` |
| `bun run docs` | TypeDoc → `../site/api/js` |

## Event bus

Components communicate exclusively through **DOM `CustomEvent`s** — no shared mutable state, no framework store.

| Event | Direction | Payload |
|-------|-----------|---------|
| `theme-change` | any → SceneManager | `{ theme: "cards"\|"social"\|"graph"\|"galaxy" }` |
| `agent-selected` | UI → SceneManager | `{ agent: { id } }` |
| `af-agent-command` | UI → WSChatClient | `{ command, agentId }` |
| `af-send-message` | IOBar/CardDash → IOManager | `{ content, target }` |
| `af-feed-push` | any → CardDashboard | `{ item: FeedItem }` |
| `af-chat-message` | WS/MQTT → ChatPanel | `{ msg: ChatMessage }` |
| `af-stream-chunk` | IOManager → ChatPanel | `{ chunk, from }` |
| `af-stream-end` | IOManager → ChatPanel | — |
| `af-connection-status` | MQTT/WS → HUD | `{ status: "live"\|"demo" }` |

## MQTT topics consumed

```
agents/{id}/heartbeat   agents/{id}/status    agents/{id}/spawn
agents/{id}/chat        agents/{id}/alert     agents/{id}/metrics
agents/{id}/logs        agents/{id}/completed
nodes/{node}/heartbeat  system/health
```

## Adding a new UI component

1. Create `src/ui/MyComponent.ts`
2. Instantiate in `main.ts` (follow the existing bootstrap order comment)
3. Subscribe to relevant DOM events via `document.addEventListener`
4. Fire DOM events rather than calling methods on other components directly
5. Run `bun run typecheck` — fix all errors before opening a PR

## Proxy configuration

During development, Vite proxies:

| Path | Target | Protocol |
|------|--------|----------|
| `/api` | `localhost:8000` | HTTP |
| `/ws` | `localhost:8081` | WebSocket |
| `/mqtt` | `localhost:9001` | WebSocket |

Set `VITE_MQTT_WS_URL` in `.env` to override the MQTT broker URL in production builds.
