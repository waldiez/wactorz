# Extensions

Extensions are self-contained feature modules that plug into Wactorz at both the
backend (Python) and frontend (TypeScript) layers. They follow a shared protocol
so the core system never needs to know about individual extension names, routes,
or config — there is no `if extension == "tts"` anywhere.

## Overview

```
wactorz/
├── ext/                    # Backend extensions (Python)
│   ├── __init__.py         #     discovery, setup_all(), collect_public_config()
│   └── tts/                #     TTS — edge-tts voice synthesis

frontend/src/
└── ext/                    # Frontend extensions (TypeScript) — mirror backend
    └── tts/index.ts        #     register({ apiBase, available })
```

At startup the monitor calls `setup_all(app)`, which auto-discovers every module
or package under `wactorz/ext/` that exposes a `setup(app)` hook and wires it in.
`collect_public_config(app)` gathers each extension's non-secret browser config
and merges it into `/api/config`. Nothing in core references a specific extension.

An extension is **opt-in and self-disabling**: it reads its own environment and
no-ops when unconfigured, so shipping one forces nothing on anyone.

---

## Backend

A backend extension lives in `wactorz/ext/<name>/`. Only `setup()` is required;
`public_config` and `on_ready` are optional (duck-typed at discovery):

```python
# wactorz/ext/<name>/__init__.py

def setup(app: web.Application) -> None:
    """Register routes, create state, wire lifecycle hooks."""

def public_config(app: web.Application) -> dict:
    """Return a dict merged into the /api/config response, namespaced by ext."""

async def on_ready(app: web.Application) -> None:
    """Called after every extension's setup() has run — for cross-extension
       logic that needs another extension already wired."""

# Optional: declare ordering dependencies for on_ready().
__deps__ = ["other-ext"]  # this on_ready() runs after other-ext's
```

### Protocol

| Hook                 | When                                                        |
|----------------------|-------------------------------------------------------------|
| `setup(app)`         | (**required**) register routes + state                      |
| `public_config(app)` | (optional) merged into `/api/config` at startup             |
| `on_ready(app)`      | (optional) runs as `on_startup`, topo-sorted by `__deps__`  |

### Requirements

- Register routes on `app.router` and namespace them under `/api/<ext>/…`.
- State belongs to the extension, not shared `app["…"]` globals — a module
  singleton or manager class keeps the feature self-contained.
- Config comes from `os.getenv(…)`, **not** `wactorz.config.CONFIG` (which stays
  free of extension-specific fields).
- Disable gracefully when the extension's env var is unset — never raise at import
  or startup because an optional dependency is missing.

### Example: TTS

```
wactorz/ext/tts/
└── __init__.py     # setup() registers /api/tts + /api/tts/voices,
                    # public_config() reports availability,
                    # on_startup warms the voice list. edge-tts is imported
                    # inside the handlers so the ext is a no-op when it's absent.
```

---

## Frontend

A frontend extension lives in `src/ext/<name>/` and exports a `register()`
function from its barrel file. It seeds itself from the config that
`public_config()` merged into `/api/config`, then communicates with the rest of
the app **only through the typed event bus** (`src/events.ts` — `emit`/`listen`),
never by calling other components directly.

```ts
// src/ext/<name>/index.ts

export interface MyConfig {
    apiBase: string;
    available: boolean;
}

export function register(config: MyConfig): void {
    if (!config.available) return;
    // Seed the singleton, then let it self-wire via the event bus.
    myManager.setApiBase(config.apiBase);
    void myManager.init();
}
```

### Wiring in main.ts

`register()` is called once during startup:

```ts
import { register as registerMyExt } from "./ext/myname";

registerMyExt({ apiBase: _apiBase, available: true });
```

### Event bus

If your extension emits or listens for new event types, add them to
`AppEventMap` in `src/events.ts` so payloads stay typed. Other components react
to those events — they never import your extension. (TTS, for example, emits
`tts-audio-start` / `tts-audio-end`; the ambient-audio manager listens and ducks
its volume, with no import between the two.)

### Testing

| What                        | Where                                       |
|-----------------------------|---------------------------------------------|
| Barrel (`register()` logic) | `src/__tests__/ext/<name>/index.test.ts`    |
| Manager / behaviour         | `src/__tests__/ext/<name>/<name>.test.ts`   |

New extensions ship with tests; coverage is gated (`bun run coverage`).

---

## Optional infrastructure (compose overlay)

If an extension needs an external service (a database, triple store, extra
broker, …), **do not add it to the base `compose.yaml`.** Doing so forces the
dependency on every user and turns the shared compose file into a merge-conflict
magnet. Instead, ship an **additive** `compose.<ext>.yaml` overlay and activate
the extension through an environment variable:

```yaml
# compose.<ext>.yaml — additive, opt-in
services:
  my-service:
    profiles: [full]
    image: …
    networks: [wactorz-net]
```

```bash
# Layer the overlay onto either base stack; both read .env, so set the ext's
# activation var (e.g. MY_EXT_URL=…) there.
docker compose -f compose.yaml     -f compose.<ext>.yaml --profile full up -d
docker compose -f compose.dev.yaml -f compose.<ext>.yaml --profile full up -d
```

Because the extension reads its URL from `os.getenv(…)` and no-ops when unset,
the base stack stays lean and the infrastructure is purely opt-in — the same
"self-disabling" contract as the code.

---

## Checklist

- [ ] Backend `setup()` registers routes under `/api/<ext>/…`
- [ ] Optional `public_config()` returns only non-secret keys (never tokens)
- [ ] Optional `on_ready()` handles cross-extension ordering via `__deps__`
- [ ] Config is read from `os.getenv(…)`, not `wactorz.config.CONFIG`
- [ ] Extension no-ops gracefully when its env var / dependency is absent
- [ ] Frontend barrel exports a `register(config)` function; wires via the event bus
- [ ] New event types added to `AppEventMap` in `src/events.ts`
- [ ] Any external service ships as an additive `compose.<ext>.yaml` overlay
- [ ] Tests in `wactorz`/`tests/ext/` and `src/__tests__/ext/<name>/`
