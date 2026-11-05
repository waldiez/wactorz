# Extensions

Extensions are self-contained feature modules that plug into Wactorz at both the
backend (Python) and frontend (TypeScript) layers. They follow a shared protocol
so the core system never needs to know about individual extension names, routes,
or config — there is no `if extension == "tts"` anywhere.

## Overview

```text
wactorz/
├── ext/                    # Backend extensions (Python)
│   ├── __init__.py         #     discovery, setup_all(), collect_public_config(),
│   │                        #     collect_actor_decorators()
│   └── tts/                #     TTS — edge-tts voice synthesis

frontend/src/
├── ext/                    # Frontend extensions (TypeScript) — mirror backend
│   └── tts/index.ts        #     register({ apiBase, configRead })
├── ui/shell/
│   └── icons.ts            #     Icon registry — registerIcon(name, svg)
├── ui/CardDashboard.ts     #     View registry — registerView(key, icon, label, builder)
└── config/serverConfig.ts  #     Config-seeding registry — registerConfigEntry(key, extract)
```

At startup the monitor calls `setup_all(app)`, which auto-discovers every module
or package under `wactorz/ext/` that exposes a `setup(app)` hook, and every
extension registered under the `wactorz.extensions` entry point by an installed
package named in `WACTORZ_EXTENSIONS`, and wires them in. `collect_public_config(app)` gathers each extension's
non-secret browser config and merges it into `/api/config`. Nothing in core
references a specific extension.

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

def actor_decorator(app: web.Application) -> Callable[[actor, dict], None] | None:
    """Return a per-actor payload enricher (or None to opt out)."""

def panel(app: web.Application) -> Panel:
    """A tab on the dashboard (see "A dashboard tab from a package")."""

# Optional: declare ordering dependencies for on_ready().
__deps__ = ["other-ext"]  # this on_ready() runs after other-ext's
```

### Protocol

| Hook                  | When                                                        |
|-----------------------|-------------------------------------------------------------|
| `setup(app)`          | (**required**) register routes + state                      |
| `public_config(app)`  | (optional) merged into `/api/config` at startup             |
| `on_ready(app)`       | (optional) runs as `on_startup`, topo-sorted by `__deps__`  |
| `actor_decorator(app)`| (optional) enriches each actor in the `/api/actors` payload |
| `panel(app)`          | (optional) a dashboard tab, read after `setup()` succeeds   |

### Enriching the actors list

`actor_decorator(app)` lets an extension add fields to every actor the dashboard
shows, without core knowing about them. Return a `(actor, payload) -> None`
closure — typically one that fetches your index once, then mutates each payload
in place:

```python
from wactorz.core import contract  # ACTOR_REGISTRY app-key (core-provided)

def actor_decorator(app):
    index = app.get("my-index") or {}       # your own per-actor data
    def decorate(actor, payload):
        payload["myField"] = index.get(actor.actor_id)
    return decorate
```

The registry itself is on `app[contract.ACTOR_REGISTRY]` (set by core before
`setup_all()`; `None` in standalone mode). A raising factory or one returning
`None` is skipped — never fatal.

### From another package

An extension does not have to live in this repository. Any package installed
beside Wactorz can register one, free or commercial, and once it is named in
`WACTORZ_EXTENSIONS` it is loaded exactly like a module under `wactorz/ext/`, after
the built-ins:

```toml
# pyproject.toml of the other package
[project.entry-points."wactorz.extensions"]
mlops = "wactorz_mlops.ext"
```

The value names a module with the hooks above, or `module:object` for an object
that has them. The entry point's name is the extension's short name: it namespaces
`public_config()` on `/api/config` (`{"mlops": {...}}`) and is what another
extension's `__deps__` refers to. A name a built-in extension already uses is
refused with a log line, so a package cannot stand in for one; a package that
fails to import, or has no `setup()`, is logged and skipped like a broken module
under `wactorz/ext/`. Restart Wactorz after installing or removing such a package.

### Enabling a package

Loading a package runs its code in the server, so being installed is not enough:
an agent's own requirements install packages too. Name the ones you trust, by
distribution name, or use `*` to load everything installed:

```bash
WACTORZ_EXTENSIONS=wactorz-mlops,wactorz-tts
```

The setting covers all three things a package can register: `wactorz.extensions`,
`wactorz.agents` and `wactorz.pipelines`. An unlisted package is not imported at
all; it is named once in the log, with the line to add, and on the Overview. The
modules under `wactorz/ext/` always load.

A node reads its own `WACTORZ_EXTENSIONS`. `/deploy` rewrites the node's `.env`, so
set it on main as `DEPLOY_<NODE>_EXTENSIONS`. On a node the setting decides which
agents it can build. Under Home Assistant it is the `extensions` option, on the
add-on and on each deploy target.

### A dashboard tab from a package

A package adds a tab without changing the dashboard: `panel()` names an ES module
in the package, and the page loads it the first time the tab opens.

```python
from pathlib import Path

from wactorz.ext import Panel


def panel(app):
    return Panel(
        title="MLOps",
        script=Path(__file__).parent / "panel.js",
        icon="models",                # one of the dashboard's icons, or an .svg file; "grid" otherwise
        topics=("mlops/#",),          # relayed to the page for host.subscribe
    )
```

```js
// panel.js: one module, bundled if it needs more than one file.
export const apiVersion = 1;

export function mount(root, host) {
    const heading = document.createElement("h2");
    heading.textContent = "MLOps";
    root.append(heading);
    const stop = host.subscribe("mlops/+/state", (topic, payload) => { /* ... */ });
    void host.request("/api/mlops/runs").then(answer => { /* answer.ok, answer.value */ });
    return () => stop();              // called when another view opens
}
```

**Its icon.** `icon` names one of the dashboard's icons, or is a `Path` to an `.svg`
file in the package, `icon=Path(__file__).parent / "icon.svg"`. The file is read once,
when the extension loads, and the page keeps only the shapes inside its `<svg>`
(`path`, `circle`, `rect`, `line`, `polyline`, `polygon`, `ellipse`, `g`), drawn as the
dashboard's own icons are: lines in the text's colour, two units wide, on a 24 by 24
grid. So draw it on that grid; the file's own `viewBox`, size and colours give way. A
file that is missing, larger than 16 KiB, not UTF-8 or with no `<svg>` in it leaves the
tab in place on the default icon, and the log says why.

`mount(root, host)` fills `root` and may return, or resolve to, a function that is
called when another view opens; the tab is mounted again when it is opened again.
`host` is the panel's whole view of the dashboard, version 1:

| Member | What it does |
|--------|--------------|
| `apiVersion`, `name` | The interface's version, and the panel's name |
| `route()`, `onRoute(fn)` | Where in the panel the address points: `#/mlops/runs/7` is `["runs", "7"]` |
| `navigate(path)` | Move the address within the panel; a refresh and Back follow it |
| `request(path, init?)` | JSON from the server with a timeout; answers `{ok: true, value}` or `{ok: false, error}`, never rejects |
| `subscribe(filter, fn)` | Messages on a topic the page is sent: the dashboard's own, and the panel's `topics` |
| `toast({title, message, type?})` | A notice; `type` is `info`, `warning` or `error` |
| `chart(series, options)` | The dashboard's line chart, to place in `root`; returns `{element, update(series, options?), destroy()}` (below) |

What `onRoute`, `subscribe` and `chart` start ends with the panel, and each returns a function
(for `chart`, its `destroy`) that ends it sooner. A panel styles itself, inside `root`: it inherits the dashboard's
text colour and font, and can read its CSS variables (`--af-text-dim`, `--af-focus`). A script that fails
to load, exports no `mount`, was written for another `apiVersion` or throws says so in
its own tab, and the rest of the dashboard is unaffected.

**A chart.** `host.chart` draws what the dashboard's own charts draw, so a tab looks like
the rest. A series is `{name, points: [{t, v}], color?, spread?}`, `t` in milliseconds since
the epoch and `v` a number or `null` for a gap; `spread: [{t, lo, hi}]` shades each point's
least and most, as a bucketed series (`GET /api/data/{source}/series`) gives them. The axes
label round values and round local times, with a faint gridline at each, worked out again
for any window zoomed into. Options:

| Option | What it draws |
|--------|---------------|
| `title`, `format?` | The chart's name, also its name for a screen reader; how a value is written (the number as the reader's locale writes it when left out) |
| `yLabel?`, `xLabel?` | What the values are, beside the value axis, and what runs along the bottom (time unless said); both join the chart's name for a screen reader |
| `thresholds: [{value, label, color?}]` | A dashed level across the chart, kept in view, named in the legend |
| `bands: [{from, to, label, tone?}]` | A stretch of time shaded across it, `tone` `warn` or `info`; its label on top and in the tooltip |
| `marks: [{t, label, v?, shape?, color?}]` | A point in time marked in a `dot`, `square`, `triangle` or `diamond`, at height `v` or at the top; each kind counted in the legend |
| `mode`, `modeToggle` | How points are drawn, `line` (broken at gaps), `points` or `joined`, or `auto` (points when most of the slots are empty); `modeToggle: true` offers the choice in the chart's header |
| `zoom`, `onZoom(window)` | `zoom: true` lets a mouse drag across the plot, or the buttons, zoom into a window of its time; `onZoom` is asked for `{from, to}` (or `null` on reset) and may answer with series for it, such as finer buckets from the server, else the chart narrows to what it holds; if it rejects, the chart keeps what it holds and says the finer view could not be read |

`chart` came within version 1, so a panel that may meet an older dashboard checks
`typeof host.chart === "function"` first.

A panel need not use it: `root` is the panel's own, to draw in with its own code or any
library. The dashboard serves the panel one script and allows scripts from its own origin
only, so a library is bundled into `panel.js`, its stylesheet inlined (inline styles and
`<style>` are allowed; a stylesheet or a font from elsewhere is not), and nothing in it may
need `eval` or `new Function`. Canvas, SVG, `data:` images and workers from `blob:` work. A
chart of its own ends with the function `mount` returns, as anything else the panel starts.

```js
const chart = host.chart(
    [{ name: "score", points }],
    { title: "Pump 1", thresholds: [{ value: 0.22, label: "alarm" }], zoom: true, modeToggle: true },
);
root.append(chart.element);
```

**What a panel is trusted with.** It runs in the page with the signed-in session, so
it can do whatever the dashboard can: as much as the package's code already can on
the server. So a tab is offered only for an extension whose `setup()` succeeded,
which for a package means one named in `WACTORZ_EXTENSIONS`. Its script is served at
`/api/ext/<name>/panel.js`, behind the same key as the rest of the API. The extension's
name is the tab's address, so it is lowercase letters, digits and `-`, and cannot be a
built-in view's; a panel that breaks a rule is logged and left out.

### Requirements

- Register routes on `app.router` and namespace them under `/api/<ext>/…`.
- State belongs to the extension, not shared `app["…"]` globals — a module
  singleton or manager class keeps the feature self-contained.
- Config comes from `os.getenv(…)`, **not** `wactorz.config.CONFIG` (which stays
  free of extension-specific fields).
- Disable gracefully when the extension's env var is unset — never raise at import
  or startup because an optional dependency is missing.

### Example: TTS

```text
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

export function register(config: MyConfig): () => void {
    if (!config.available) return () => {};
    // Seed the singleton, then let it self-wire via the event bus.
    myManager.setApiBase(config.apiBase);
    void myManager.init();
    return listen("my-event", detail => myManager.handle(detail));
}
```

### Wiring in app.ts

`register()` is called once during startup, after `/api/config` is seeded, by
`bootFromConfig` in `app.ts`; what it returns is how `createApp`'s `stop` ends it:

```ts
import { register as registerMyExt } from "./ext/myname";

running.stopMyExt = registerMyExt({ apiBase: ingressBase(), available });
```

### Config seeding

`config/serverConfig.ts` fetches `/api/config` once and seeds registered keys
into `safeStorage` (paired with a `__server` baseline so a user's local edit
survives until the server value itself changes). Core registers only its own
keys; your barrel contributes the extension's at module load — before
`seedServerConfig()` runs:

```ts
import { registerConfigEntry } from "../../config/serverConfig";

// Fields your backend public_config() returns, namespaced by ext name.
registerConfigEntry("wactorz-myext-flag", c =>
    (c.myext as Record<string, unknown> | undefined)?.flag as string | undefined,
);
```

Keys not registered are never stored, even if the server sends them.

### Dashboard views and icons

An extension that adds a dashboard tab uses the two registries — core never
imports extension views or icon names:

```ts
import { registerIcon } from "../../ui/shell/icons";

export function register(config: MyConfig): void {
    // 1. Register the icon BEFORE registerView() uses its name.
    registerIcon("hexagon", '<path d="M21 16V8a2 …"/>');

    // 2. Register the tab: nav button + lazy view builder.
    config.registerView("mytab", "hexagon", "My Tab", () => buildMyView(config.onRender));
}
```

A view is asked for on the bus: `emit("af-register-view", { key, icon, label, build })`
reaches `CardDashboard.registerView`. `build` is a `() => HTMLElement` thunk called lazily on navigation — keep it pure and build
a fresh element tree per call.

| Registry                | Module                   | Purpose                      |
|-------------------------|--------------------------|------------------------------|
| `registerIcon()`        | `ui/shell/icons.ts`      | Add SVG icons by name        |
| `registerView()`        | `CardDashboard` (method) | Add a nav tab + view builder |
| `registerConfigEntry()` | `config/serverConfig.ts` | Seed `/api/config` fields    |

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
- [ ] A packaged extension is named in `[project.entry-points."wactorz.extensions"]`, and its docs say to add the distribution to `WACTORZ_EXTENSIONS`
- [ ] Optional `public_config()` returns only non-secret keys (never tokens)
- [ ] Optional `on_ready()` handles cross-extension ordering via `__deps__`
- [ ] Optional `actor_decorator()` enriches the `/api/actors` payload
- [ ] Optional `panel()` names a `.js` module exporting `apiVersion` and `mount(root, host)`, and an icon by name or an `.svg` on a 24 by 24 grid
- [ ] Config is read from `os.getenv(…)`, not `wactorz.config.CONFIG`
- [ ] Extension no-ops gracefully when its env var / dependency is absent
- [ ] Frontend barrel exports a `register(config)` function; wires via the event bus
- [ ] Config fields registered from the barrel via `registerConfigEntry()`
- [ ] Icons registered via `registerIcon()` before `registerView()`
- [ ] New event types added to `AppEventMap` in `src/events.ts`
- [ ] Any external service ships as an additive `compose.<ext>.yaml` overlay
- [ ] Tests in `wactorz`/`tests/ext/` and `src/__tests__/ext/<name>/`
