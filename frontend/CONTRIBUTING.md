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
- [ ] Config fields (e.g. `available`, `url`) are registered from the barrel via
      `registerConfigEntry()` from `config/serverConfig.ts` and read from `safeStorage`
- [ ] Custom icons registered via `registerIcon()` from `ui/dashboard/icons.ts`
      before emitting `af-register-view` for the extension's view — core never imports your icons
- [ ] Unit test in `src/__tests__/ext/<name>/` mirroring the extension layout
- [ ] Extension docs in `backend` and `frontend` READMEs

### New UI component

- [ ] File lives in `src/ui/`
- [ ] Component is a plain class — no framework, no global state
- [ ] Wires events via the typed `emit` / `listen` helpers in `src/events.ts`
- [ ] Cleaned up in a `destroy()` method (remove event listeners)
- [ ] Instantiated in the matching numbered section of `main.ts` (see its header map)
- [ ] Unit test added in `src/__tests__/` (coverage is gated in CI)
- [ ] What a person acts on or reads has a role and a name (see [Finding elements](#finding-elements))
- [ ] A new view needs nothing for its address: `src/router.ts` gives every view `#/<key>`, an
      extension's included. A view with levels of its own (`#/models/hvac/versions/7`) reads them
      from `Route.path` in `ui/dashboard/viewRoutes.ts`, and reports where it is with `af-view-changed`
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
      `agents/deletionGuard`, `ui/haFeed`, `config/serverConfig`), not inline
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

- [ ] It is a model: no DOM and no `ui/` import (lint enforces the import). Views read it and `subscribe`
- [ ] Records are replaced, never changed in place, so a view holding the old one can tell what changed
- [ ] A name is not an identity: two agents may share one, so look an agent up by id, and by name only
      where several answers are handled (`byName` returns a list)
- [ ] Each change is told once per batch: a new kind of change goes in `AgentChanges`

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

- Build elements in code with the helpers in `src/ui/dom.ts` (`el`, `button`, `iconButton`, `option`,
  `externalLink`, `named`) — they set text as text, give every button `type="button"`, and keep
  links and form controls consistent. Reach for `document.createElement` only for what they do not
  cover (a tag computed at runtime, `<template>`, `<style>`)
- No `innerHTML` with user-controlled strings (XSS); icon markup from the icon registry is the exception
- Use CSS classes for state (`.active`, `.hidden`) rather than inline styles where possible
- Give assistive technology what it reads with the helpers in `src/ui/dom.ts` rather than
  `setAttribute` by hand: `labelled`, `dialog`, `live`, `list` / `listItem`, `clickable`,
  `expanded`, `pressed`, `focusables`

### Finding elements

Tests find an element the way a person using a screen reader does: by its role and the name
read out for it — `getByRole("button", { name: "Stop weather" })` in the end-to-end harness,
`byRole(root, "button", "Stop weather")` from `src/__tests__/queries.ts` in a unit test. So a
locator that works is also a check that the element is accessible.

Where an element has no such name (a card's state line, a message's sender, the parts of a
toast), mark it with `testId(node, "…")` from `src/ui/dom.ts`. The ids are listed in
`src/ui/testIds.ts`, and `testids.test.ts` checks that list against what
`e2e/harness/browser.py` looks for, in both directions. Never find an element by class: classes
belong to the styles and move with them.

### Accessibility checks

`axe.test.ts` runs [axe](https://github.com/dequelabs/axe-core) on every view of the dashboard
as the page builds it, and the end-to-end suite runs it again in a real browser, where colour
contrast is judged too; the journeys also walk each view with Tab and fail where focus does not
show. A serious or critical finding fails either. One that has to stay for now goes in
`e2e/harness/axe-accepted.json` with its reason.

Colours for text come from the tokens in `src/styles/base.css`: `--af-text-dim` is the dimmest
text that keeps 4.5:1 on the dashboard's backgrounds, so nothing dimmer is used for words.

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
