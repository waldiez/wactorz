# Contributing to the Wactorz frontend

## Before you start

- [ ] `bun run typecheck` passes on `main`
- [ ] `bun run fmt` has been run (Prettier, no manual style arguments)
- [ ] You understand the [event bus](README.md#event-bus) — components talk via `CustomEvent`, not direct calls

## PR checklist

### Every PR

- [ ] `bun run typecheck` — zero type errors
- [ ] `bun run fmt` — no diff after running
- [ ] `bun run build` — bundle succeeds, no new chunk-size warnings
- [ ] Tested in browser against a live backend (or at minimum the MQTT mock stack)

### New UI component

- [ ] File lives in `src/ui/`
- [ ] Component is a plain class — no framework, no global state
- [ ] Wires events via `document.addEventListener` / `document.dispatchEvent`
- [ ] Cleaned up in a `destroy()` method (remove event listeners)
- [ ] Added to bootstrap order comment in `main.ts`
- [ ] `bun run docs` — TypeDoc still builds

### New agent interaction (command / event)

- [ ] New `CustomEvent` name follows the `af-*` prefix convention
- [ ] Payload type added to `src/types/agent.ts`
- [ ] Sender dispatches the event; receiver only listens — no circular calls
- [ ] Backend counterpart event/command documented in the PR description

### Touching the feed / CardDashboard

- [ ] Feed items pass through the `SYSTEM_AGENT_NAMES` filter (infrastructure agents are excluded)
- [ ] `canDirectMessage()` used for chat/action button visibility — do not inline the logic
- [ ] `nameFromWid()` used when displaying agent names from raw WID strings
- [ ] `hideHeartbeats` toggle still works correctly

### Touching the Babylon.js scene

- [ ] Tested in both `cards` and `graph` themes
- [ ] Dispose of all meshes / materials / textures in the relevant `dispose()` method
- [ ] No `console.log` left in scene code (use `console.info` for intentional dev output)

## Style guide

**TypeScript**
- Strict mode is on — no `any`, no `!` non-null assertions without a comment
- Prefer `const` and immutable patterns
- No comments explaining *what* code does — only *why* when it would surprise a reader

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
