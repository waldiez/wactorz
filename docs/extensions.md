# Extensions

Extensions are self-contained feature modules that plug into Wactorz at both the
backend (Python) and frontend (TypeScript) layers. They follow a shared protocol
so the core system never needs to know about individual extension names, routes,
or UI views.

## Overview

```
wactorz/
├── ext/                    # Backend extensions (Python)
│   ├── __init__.py         #     discovery, setup_all(), collect_public_config()
│   ├── tts/                #     TTS — edge-tts voice synthesis
│   ├── fuseki/             #     Fuseki — SPARQL triple store bridge
│   └── swid/               #     SWID — DID minting & identity register

frontend/src/
├── ext/                    # Frontend extensions (TypeScript) — mirror backend
│   ├── tts/index.ts        #     register({ apiBase, available })
│   ├── fuseki/index.ts     #     register({ url, dataset, onRender, registerView })
│   └── swid/index.ts       #     register({ onRender, registerView })
├── ui/dashboard/
│   ├── icons.ts            #     Icon registry — registerIcon(name, svg)
│   └── CardDashboard.ts    #     View registry — registerView(key, icon, label, builder)
└── config/serverConfig.ts  #     Whitelist for /api/config → safeStorage seeding
```

Each extension owns everything it needs — routes, state, views, icons — and the
core system discovers and composes them. No `if extension == "fuseki"` anywhere.

---

## Backend

A backend extension lives in `wactorz/ext/<name>/`. Only `setup()` is required;
`public_config` and `on_ready` are optional (duck-typed at discovery):

```python
# wactorz/ext/<name>/__init__.py

def setup(app: web.Application) -> None:
    """Register routes, create state, wire lifecycle hooks."""

def public_config(app: web.Application) -> dict:
    """Return a dict that gets merged into the /api/config response."""

async def on_ready(app: web.Application) -> None:
    """Called after all extensions have run setup().
       Use for cross-extension logic (e.g. fuseki reads swid state)."""

# Optional: declare ordering dependencies.
__deps__ = ["swid"]  # on_ready() runs after swid's on_ready()
```

### Protocol

| Hook              | When                                     |
|-------------------|------------------------------------------|
| `setup(app)`      | (**required**) Phase 1 — register routes + state        |
| `public_config(app)` | (optional) Called at startup; merged into `/api/config` |
| `on_ready(app)`   | (optional) Phase 2 — topo-sorted by `__deps__`, registered as `on_startup` |

### Requirements

- Routes use `aiohttp.web.RouteTableDef` (namespace via `/api/<ext>/…`).
- State belongs to the extension, not `app["…"]` globals — use a manager class.
- Config comes from `os.getenv(…)`, not `wactorz.config.CONFIG`.
- If the extension is optional, disable gracefully when its env var is unset.

### Example: Fuseki

```
wactorz/ext/fuseki/
├── __init__.py     # setup(), public_config(), on_ready(), __deps__
├── bridge.py       # FusekiClient, HAFusekiBridge, HAWebSocketClient
├── manager.py      # BridgeManager — instance state (ha_task, agent_tasks)
└── proxy.py        # aiohttp route handlers for /api/fuseki/<dataset>/sparql
```

---

## Frontend

A frontend extension lives in `src/ext/<name>/` and exports a `register()`
function from its barrel file:

```ts
// src/ext/<name>/index.ts

export interface MyConfig {
    available: boolean;
    onRender: () => void;
    registerView: (key: string, icon: string, label: string, builder: () => HTMLElement) => void;
}

export function register(config: MyConfig): void {
    if (!config.available) return;

    // 1. Register custom icons (core never imports your icon names).
    registerIcon("hexagon", '<path d="M21 16V8a2 …"/>');

    // 2. Register a view tab.
    config.registerView("mytab", "hexagon", "My Tab", () =>
        buildMyView(config.onRender),
    );
}
```

### Registry

| Registry          | Module                        | Purpose                      |
|-------------------|-------------------------------|------------------------------|
| `registerIcon()`  | `ui/dashboard/icons.ts`       | Add SVG icons by name        |
| `registerView()`  | `CardDashboard` (method)      | Add a nav tab + view builder |

### View builder contract

The builder is a `() => HTMLElement` thunk. It is called lazily — only when the
user navigates to that tab. Keep the builder pure: create a fresh element tree
on every call; the dashboard replaces the previous view before mounting.

### Wiring in main.ts

```ts
import { register as registerMyExt } from "./ext/myname";

// Inside the /api/config seed callback:
const available = safeStorage.get("wactorz-myext-available") === "1";
registerMyExt({
    available,
    onRender: () => agentStore.cardDashboard!.renderView(),
    registerView: (k, i, l, b) => agentStore.cardDashboard!.registerView(k, i, l, b),
});
```

### Config seeding

Add your extension's config fields to `src/config/serverConfig.ts` so
`/api/config` values flow into `safeStorage` on startup.

### Testing

| What                             | Where                                       |
|----------------------------------|---------------------------------------------|
| Barrel (`register()` logic)      | `src/__tests__/ext/<name>/index.test.ts`    |
| View (DOM rendering + behaviour) | `src/__tests__/ext/<name>/<view>.test.ts`   |

### Icons

Extension icons are registered via `registerIcon(name, svgPaths)` from
`ui/dashboard/icons.ts`. Built-in icons (grid, list, chat, settings, etc.) are
pre-registered at module load. Extensions must call `registerIcon()` **before**
`registerView()` with the same icon name.

---

## Checklist

- [ ] Backend `setup()` registers routes under `/api/<ext>/…`
- [ ] Backend `public_config()` returns a dict with consumer-specific keys
- [ ] Backend `on_ready()` handles cross-extension ordering via `__deps__`
- [ ] Config is read from `os.getenv(…)`, not `wactorz.config.CONFIG`
- [ ] Frontend barrel exports a `register(config)` function
- [ ] Icons registered via `registerIcon()` before `registerView()`
- [ ] Config fields added to `config/serverConfig.ts` whitelist
- [ ] Tests in `src/__tests__/ext/<name>/` mirror the extension layout
