# Wactorz Frontend

Vite + TypeScript single-page application that visualises a running Wactorz agent system.
Renders a live dashboard as HTML/CSS card components driven by real-time MQTT events.

## Stack

| Layer | Library | Purpose |
| ----- | ------- | ------- |
| Transport | MQTT.js 5 | Real-time agent events |
| Chat bridge | WebSocket (native) | Direct `main` agent replies |
| IDs | `@waldiez/wid` | Time-ordered collision-resistant IDs |
| Build | Vite 8 + TypeScript 6 | Strict mode, ES2022 target |

## Directory layout

```text
frontend/
├── src/
│   ├── main.ts            # Bootstrap — wires transports → store + UI
│   ├── types/             # Shared types: agent.ts, feed.ts, global.d.ts (Window augmentation)
│   ├── mqtt/              # MQTT WebSocket client + typed event emitter
│   ├── agents/            # Agent-state store + logic: AgentStore, mapping, naming, deletionGuard
│   ├── io/                # IO/transport: IOManager, WSChatClient, TTSManager, SpeechToText,
│   │                      #   HAClient, AgentImageGen, AmbientManager, DesktopNotify
│   ├── ui/                # HTML/CSS card components (no framework); ui/dashboard/ is the dashboard
│   ├── styles/            # Global CSS (base, cards, chat, dashboard, …); app.css is the entry
│   └── __tests__/         # Vitest unit tests (happy-dom)
├── public/                # Static assets (avatars, icons, sw.js, webmanifest)
├── index.html
├── vite.config.ts         # Dev proxy → :8888 (REST + WS + MQTT); build → ../static/app
├── vitest.config.ts       # Coverage floors (gated in CI)
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
| ------ | ------------ |
| `bun run dev` | Vite dev server on :3000, proxying `/api`, `/ws`, `/mqtt` to :8888 |
| `bun run build` | TypeScript check + Vite bundle → `../static/app` |
| `bun run preview` | Serve the production bundle locally |
| `bun run typecheck` | `tsc --noEmit` only |
| `bun run lint` | typecheck + Prettier check + ESLint + markdownlint (what CI runs) |
| `bun run fmt` | Prettier write over `src/**/*.{ts,tsx,css,json}` |
| `bun run markdownlint` | markdownlint-cli2 `--fix` over `*.md` (`:check` variant is in `lint`) |
| `bun run test` | Vitest unit tests (happy-dom) |
| `bun run coverage` | Vitest with coverage; **fails below the gated thresholds** |
| `bun run docs` | TypeDoc → `../site/api/js` |

## Testing

Unit tests run on [Vitest](https://vitest.dev) in a `happy-dom` environment — no browser required:

```bash
bun run test         # run once
bun run test:watch   # watch mode
bun run coverage     # run with a coverage report
```

Coverage thresholds live in `vitest.config.ts` and **gate CI**: `bun run coverage`
fails if any metric drops below the floor. The floor is ratcheted up as coverage grows — raise it,
never lower it. New components and bug fixes should ship with a test in `src/__tests__/`.

## Event bus

Components communicate exclusively through **DOM `CustomEvent`s** — no shared mutable state, no framework store.

| Event | Direction | Payload |
| ------ | --------- | ------- |
| `af-agent-command` | CardDashboard → main.ts (WSChatClient) | `{ command, agentId }` |
| `af-send-message` | DashboardChat → main.ts (IOManager) | `{ content, target, attachments }` |
| `af-feed-push` | IOManager / main.ts → CardDashboard | `{ item: FeedItem }` |
| `af-chat-message` | main.ts (WS/MQTT) → DashboardChat | `{ msg: ChatMessage }` |
| `af-stream-chunk` | IOManager → DashboardChat | `{ chunk, from }` |
| `af-stream-end` | IOManager → DashboardChat | `{ text, from }` |
| `af-connection-status` | main.ts (MQTT/WS) → CardDashboard | `{ status: "live" \| "demo" }` |
| `af-attachment-added` | DropZone / chatIobar → DashboardChat | `{ attachment }` |
| `af-ha-state-change` | HAClient → main.ts | `{ entityId, state, friendlyName }` |
| `af-reset-chat` | WSChatClient → DashboardChat | `{ agent: string \| null }` |
| `af-clear-feed` | WSChatClient / main.ts → CardDashboard | _(none)_ |
| `af-wipe-all` | WSChatClient / main.ts → CardDashboard | _(none)_ |
| `tts-voices-loaded` | TTSManager → popovers | `{ voices }` |

## MQTT topics consumed

```text
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
5. Add a unit test in `src/__tests__/` (coverage is gated in CI)
6. Run `bun run lint && bun run test` — both must pass before opening a PR

## Proxy configuration

During development, Vite proxies:

| Path | Target | Protocol |
| ---- | ------ | -------- |
| `/api` | `localhost:8888` | HTTP |
| `/ws` | `localhost:8888` | WebSocket |
| `/mqtt` | `localhost:8888` | WebSocket |

All three proxy to the backend monitor server on `:8888` (see `vite.config.ts`).
Set `VITE_MQTT_WS_URL` in `.env` to override the MQTT broker URL in production builds.
Set `VITE_OPEN=true` to auto-open the browser on `bun run dev`.

## Deployment modes

The same bundle serves two targets, distinguished at runtime:

| Mode | How API/WS URLs resolve |
| ---- | ----------------------- |
| Standalone | Paths are root-relative (`/api`, `/ws`, `/mqtt`) |
| Home Assistant add-on | The Python add-on injects `window.__WACTORZ_INGRESS_PATH`; all API/WS URLs are rebased onto that ingress prefix |

`__WACTORZ_INGRESS_PATH` is typed in `src/types/global.d.ts` and read wherever a
URL is built (e.g. `main.ts`, `chatHistory.ts`, `popovers.ts`, `HAClient`). It
defaults to `""`, so standalone builds need no configuration.

## Feature flags (build-time)

Some UI is gated behind backend endpoints that aren't always available. They're
off by default; enable per-deploy by setting the env var at build time (`.env`):

| Flag | Enables | Needs backend |
| ---- | ------- | ------------- |
| `VITE_STT_ENABLED=true` | Voice/mic button (speech-to-text) | `/api/stt` |
| `VITE_UPLOADS_ENABLED=true` | Attachments (drag-drop + paste) | `/api/upload` |
