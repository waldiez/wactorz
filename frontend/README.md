# Wactorz Frontend

Vite + TypeScript single-page application that visualises a running Wactorz agent system.
Renders a live dashboard as HTML/CSS card components driven by real-time server-push.

## Stack

| Layer | Library | Purpose |
| ----- | ------- | ------- |
| Transport | WebSocket (native) | One `/ws` connection: live server events + chat |
| IDs | `@waldiez/wid` | Time-ordered collision-resistant IDs |
| Build | Vite 8 + TypeScript 6 | Strict mode, ES2022 target |

## Directory layout

```text
frontend/
├── src/
│   ├── main.ts            # Bootstrap — wires transports → store + UI
│   ├── events.ts          # Typed app event bus over document CustomEvents (emit/listen + AppEventMap)
│   ├── safeStorage.ts     # localStorage wrapper with fallback-safe reads
│   ├── types/             # Shared types: agent.ts, feed.ts, ws.ts, global.d.ts (Window augmentation)
│   ├── time.ts            # toMs() epoch→ms helper
│   ├── config/            # App-level config: fetches /api/config, seeds safeStorage
│   │   └── serverConfig.ts
│   ├── ext/               # Extensions — self-contained feature modules (mirrors backend wactorz/ext/)
│   │   └── tts/           # TTS extension: TTSManager, types, register()
│   │       ├── index.ts
│   │       ├── TTSManager.ts
│   │       └── types.ts
│   ├── agents/            # Agent-state store + logic: AgentStore, mapping, naming, deletionGuard
│   ├── io/                # IO/transport: WSClient (the /ws connection), ServerEventRouter
│   │                      #   (topic→typed-event decoder), IOManager, SpeechToText,
│   │                      #   AmbientManager, logger
│   ├── ui/                # HTML/CSS card components (no framework); ui/dashboard/ is the dashboard
│   ├── styles/            # Global CSS (base, cards, chat, dashboard, …); app.css is the entry
│   └── __tests__/         # Vitest unit tests (happy-dom)
│       └── ext/tts/       # Mirror of ext/ structure for extension tests
├── public/                # Static assets (icons, sw.js, webmanifest)
├── index.html
├── vite.config.ts         # Dev proxy → :8888 (REST + WS); build → ../static/app
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
| `bun run dev` | Vite dev server on :3000, proxying `/api`, `/ws` to :8888 |
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
Use the typed helpers in `src/events.ts`: `emit(type, detail)` to dispatch and `listen(type, handler)` to
subscribe. `AppEventMap` there is the single source of truth for every event name and its payload (the table
below mirrors it); add new events to that map so dispatch and handlers stay type-checked.

| Event | Direction | Payload |
| ------ | --------- | ------- |
| `af-agent-command` | CardDashboard → main.ts (WSClient) | `{ command, agentId }` |
| `af-send-message` | DashboardChat → main.ts (IOManager) | `{ content, target, attachments }` |
| `af-feed-push` | IOManager / main.ts → CardDashboard | `{ item: FeedItem }` |
| `af-chat-message` | main.ts (WSClient) → DashboardChat | `{ msg: ChatMessage }` |
| `af-stream-chunk` | IOManager → DashboardChat | `{ chunk, from }` |
| `af-stream-end` | IOManager → DashboardChat | `{ text, from }` |
| `af-connection-status` | main.ts (WSClient) → CardDashboard | `{ status: "live" \| "connecting" \| "demo" }` |
| `af-attachment-added` | DropZone / chatIobar → DashboardChat | `{ attachment }` |
| `af-send-failed` | IOManager → DashboardChat | `{ content, target }` |
| `af-reset-chat` | WSClient → DashboardChat | `{ agent: string \| null }` |
| `af-agents-settled` | WSClient → CardDashboard | `{ reason: "reset" \| "deleted" }` |
| `af-clear-feed` | WSClient / main.ts → CardDashboard | _(none)_ |
| `af-wipe-all` | WSClient / main.ts → CardDashboard | _(none)_ |
| `tts-voices-loaded` | TTSManager → popovers | `{ voices }` |
| `tts-audio-start` | TTSManager → AmbientManager | _(none)_ |
| `tts-audio-end` | TTSManager → AmbientManager | _(none)_ |

## Server events consumed

Live activity arrives as `server_event` frames over `/ws` (the backend relays them from
MQTT server-side); `ServerEventRouter` decodes each by its topic key into a typed event:

```text
agents/{id}/heartbeat   agents/{id}/status    agents/{id}/spawn
agents/{id}/chat        agents/{id}/alert     agents/{id}/metrics
agents/{id}/logs        agents/{id}/completed
nodes/{node}/heartbeat  system/health         system/host    system/qa-flag
```

## Adding a new UI component

1. Create `src/ui/MyComponent.ts`
2. Instantiate in `main.ts`, in the matching numbered section (see its header map)
3. Subscribe to relevant events via `listen(type, handler)` from `src/events.ts`
4. Fire events via `emit(type, detail)` rather than calling methods on other components directly
   (add the event to `AppEventMap` first)
5. Add a unit test in `src/__tests__/` (coverage is gated in CI)
6. Run `bun run lint && bun run test` — both must pass before opening a PR

## Adding an extension

Extensions live in `src/ext/<name>/` and mirror the backend's `wactorz/ext/<name>/`.
TTS (`src/ext/tts/`) is the reference implementation. An extension:

1. **Owns its folder** — `index.ts` is the barrel, exporting types + a `register(config)` function
2. **Self-bootstraps** — `register(config)` is called once from `main.ts`; it wires hooks,
   probes the backend, and registers event listeners
3. **Talks via the event bus** — never imports other extensions directly; emits/receives
   typed events on `AppEventMap` (`src/events.ts`)
4. **Reads config from safeStorage** — `/api/config` results are seeded by
   `config/serverConfig.ts`; register your extension's fields from the barrel via
   `registerConfigEntry()` at module load
5. **Registers custom icons** via `registerIcon(name, svgPaths)` from
   `ui/dashboard/icons.ts` before calling `registerView` — core never needs to
   know your icon names
6. **Tests mirror the layout** — `src/__tests__/ext/<name>/`

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full guide: style/JSDoc rules, the
PR checklist, and the branch target (`dev`).

## Proxy configuration

During development, Vite proxies:

| Path | Target | Protocol |
| ---- | ------ | -------- |
| `/api` | `localhost:8888` | HTTP |
| `/ws` | `localhost:8888` | WebSocket |

Both proxy to the backend monitor server on `:8888` (see `vite.config.ts`).
Set `VITE_OPEN=true` to auto-open the browser on `bun run dev`.

## Deployment modes

The same bundle serves two targets, distinguished at runtime:

| Mode | How API/WS URLs resolve |
| ---- | ----------------------- |
| Standalone | Paths are root-relative (`/api`, `/ws`) |
| Home Assistant add-on | The Python add-on injects `window.__WACTORZ_INGRESS_PATH`; all API/WS URLs are rebased onto that ingress prefix |

`__WACTORZ_INGRESS_PATH` is typed in `src/types/global.d.ts` and read wherever a
same-origin URL is built (e.g. `main.ts`, `chatHistory.ts`, `popovers.ts`, `haConfig`).
It defaults to `""`, so standalone builds need no configuration.

The dashboard does **not** talk to Home Assistant directly: the "Devices" nav button
links out to the HA UI (new tab) using the `ha.url` from `/api/config`. No HA token
ever reaches the browser. HA entity activity still reaches the feed via the
`ha-state-bridge-agent` over MQTT (`homeassistant/state_changes/#`).

The backend subscribes server-side to `agents/#`, `system/#`, `nodes/#`, and
`homeassistant/state_changes/#` and relays them to the browser as `server_event`
frames. `ServerEventRouter` routes most topics to typed events; feed-only agent
topics (e.g. `actuations`, `anomaly`) ride the `raw` catch-all and are mapped to
feed rows by the extensible `rawFeedItem` registry in `agents/mapping.ts` — add a
topic there without touching the router.

## Gated UI

Two things are gated, for two different reasons — and the difference decides
where the switch belongs.

### Not built yet — build-time

| Flag | Enables | Needs backend |
| ---- | ------- | ------------- |
| `VITE_STT_ENABLED=true` | Voice/mic button (speech-to-text) | `/api/stt` |

`STT_ENABLED` (`src/io/SpeechToText.ts`) is off by default because **`/api/stt`
does not exist**. The client half is written and waiting; turning the flag on
without the endpoint gives a mic button whose every recording fails. It is a
switch for developing the feature, not a per-deploy option — when the endpoint
lands, this should become a server capability like the one below and the flag
should go.

### Optional per deployment — runtime, from the server

Attachments (drag-drop + paste) are **not** a build-time flag. The server only
registers `/api/upload` when it has uploads on, so the browser asks rather than
assumes: the `uploads.enabled` field of `/api/config` is seeded into
`wactorz-uploads-enabled` and read by `uploadsEnabled()`.

A build genuinely cannot know the answer here — one bundle is served by
deployments that differ, and the committed `static/app` goes to all of them. Set
`WACTORZ_UPLOADS` on the backend; the UI follows it.
