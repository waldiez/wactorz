# Contributing to the Wactorz frontend

<!-- markdownlint-disable MD036 -->

## Before you start

- [ ] `bun run typecheck` passes on `dev`
- [ ] `bun run fmt` has been run (Prettier, no manual style arguments)
- [ ] You understand the [event bus](README.md#event-bus) — components talk via `CustomEvent`, not direct calls

## PR checklist

> **Branch target:** all branches start from `dev` and all PRs target `dev`.
> Never base on or open a PR against `main` — `main` is for releases only.

### Every PR

- [ ] `bun run lint` — typecheck + Prettier + ESLint + markdownlint all clean (what CI runs)
- [ ] `bun run test` — all unit tests pass
- [ ] New/changed exported functions and public methods have a JSDoc (see [Style guide](#style-guide))
- [ ] `bun run coverage` — meets the gated thresholds (raise them as you add tests, never lower)
- [ ] `bun run build` — bundle succeeds, no new chunk-size warnings
- [ ] `bun run docs` — TypeDoc builds with no warnings
- [ ] Tested in browser against a live backend (or at minimum the MQTT mock stack)

### New extension

Extensions live in `src/ext/<name>/` and mirror `wactorz/ext/<name>/` on the backend.
TTS (`src/ext/tts/`) is the reference implementation. When adding one:

- [ ] Create `src/ext/<name>/index.ts` — barrel exporting types + a `register(config)` function
- [ ] `register(config)` is called once from `main.ts` during startup; it self-wires hooks,
      probes the backend endpoint, and registers event listeners — no other file imports
      the extension directly
- [ ] Extension talks to other modules **only** via the typed event bus (`AppEventMap` in
      `src/events.ts`) — never imports from other extensions or `ui/` directly
- [ ] Config fields (e.g. `available`, `url`) are added to the whitelist in
      `config/serverConfig.ts` and read from `safeStorage`
- [ ] Custom icons registered via `registerIcon()` from `ui/dashboard/icons.ts`
      before calling `dashboard.registerView()` — core never imports your icons
- [ ] Unit test in `src/__tests__/ext/<name>/` mirroring the extension layout
- [ ] Extension docs in `backend` and `frontend` READMEs

### New UI component

- [ ] File lives in `src/ui/`
- [ ] Component is a plain class — no framework, no global state
- [ ] Wires events via the typed `emit` / `listen` helpers in `src/events.ts`
- [ ] Cleaned up in a `destroy()` method (remove event listeners)
- [ ] Instantiated in the matching numbered section of `main.ts` (see its header map)
- [ ] Unit test added in `src/__tests__/` (coverage is gated in CI)
- [ ] `bun run docs` — TypeDoc still builds

### New agent interaction (command / event)

- [ ] New `CustomEvent` name follows the `af-*` prefix convention
- [ ] Event name + payload added to `AppEventMap` in `src/events.ts` (the single
      source of truth for `emit`/`listen` typing); domain payloads (MQTT/WS shapes)
      stay in `src/types/`
- [ ] Sender dispatches the event; receiver only listens — no circular calls
- [ ] Backend counterpart event/command documented in the PR description

### Touching `main.ts` (the composition root)

`main.ts` is wiring only — it instantiates services, derives URLs, and registers
handlers in its eight numbered sections. It is **covered** by `src/__tests__/main-bootstrap.test.ts`
(no longer a coverage exclusion), so keep it thin:

- [ ] Handlers stay thin delegators — store call + `pushFeed`/`toast`, nothing more
- [ ] Any decision/transform goes in a tested module (`agents/mapping`,
      `agents/deletionGuard`, `ui/haFeed`, `ui/dashboard/haConfig`), not inline
- [ ] New transport/app-event handlers go in the right numbered section, before the
      `connect()` calls in section 7
- [ ] `main-bootstrap.test.ts` drives the new handler (mock the transport, invoke the
      registered callback) so coverage stays green

### Touching the feed / CardDashboard

- [ ] Feed items pass through the `SYSTEM_AGENT_NAMES` filter (infrastructure agents are excluded)
- [ ] `canDirectMessage()` used for chat/action button visibility — do not inline the logic
- [ ] `nameFromWid()` used when displaying agent names from raw WID strings
- [ ] `hideHeartbeats` toggle still works correctly

### Touching AgentStore

- [ ] Keep the public API stable: agent CRUD, `reconcileAgents`, `dispose`
- [ ] Agent-state mutations go through `addOrUpdateAgent` / `removeAgent` so the CardDashboard stays in sync
- [ ] No `console.log` left in coordinator code (use `console.info` for intentional dev output)

## Style guide

**TypeScript**

- Strict mode is on — no `any`, no `!` non-null assertions without a comment
- Prefer `const` and immutable patterns
- Inline comments explain *why*, not *what* — only when it would surprise a reader
- Every exported function/const, public class method, and member of an exported
  interface carries a short JSDoc (one or two lines, written for a stranger —
  what it does now, not how it changed, dates, or who touched it).
  `bun run docs` surfaces anything missing.

**DOM**

- Build elements in code (`document.createElement`) — no `innerHTML` with user-controlled strings (XSS)
- Use CSS classes for state (`.active`, `.hidden`) rather than inline styles where possible

**Events**

- Always clean up `addEventListener` on component destroy
- Never store references to other component instances — fire events instead

## Running the full dev stack

```bash
# Terminal 1 — infrastructure (mosquitto + mock agents)
make dev

# Terminal 2 — Python backend
make run-py

# Terminal 3 — Vite HMR
cd frontend && bun run dev
```

The browser opens at `http://localhost:3000`.
