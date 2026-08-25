# AGENTS.md

Guidance for coding agents (and human contributors) working in this repository.
Keep changes small, tested, and consistent with what's already here.

## What this is

**Wactorz** — an actor-model multi-agent framework. Agents spawn, coordinate, and
monitor each other at runtime over MQTT. A Python `aiohttp` monitor server exposes a
REST + WebSocket API and serves a framework-free TypeScript dashboard (SPA).

## Layout

- `wactorz/` — the Python package: `agents/`, `core/`, `interfaces/`, `web/` (the aiohttp
  server: `app` routes + `runtime` shared state + a module per concern — named `web` to avoid
  colliding with `monitoring/`), `ext/` (optional features), `cli.py`, `config.py`.
- **Inside `agents/`, an agent is a file until it grows a second concern; then it becomes a
  package with the same internal shape** — `agents/main/` is the worked example, and its
  `__init__.py` states the rule and the layering it depends on (a package may import from the
  shared tier — `mixins/`, `llm/`, `prompts/`, `lookup.py` — and the shared tier may never
  import back from it). Copy that shape rather than inventing a second one.
- `frontend/` — Vite + TypeScript dashboard. **Read `frontend/CONTRIBUTING.md` before touching it.**
- `tests/` — the pytest suite.
- `ha-addon/` — Home Assistant add-on packaging (bundles the built frontend).
- `static/` — pre-built assets (SPA + docs), committed and bundled into the wheel.

## Commands — prefer the Makefile (`make help` lists all)

| Task | Command |
| ---- | ------- |
| Install dev deps | `make install-dev` |
| Run backend | `make run` · full dev stack: `make dev-full` |
| Tests | `make test` (Python + frontend) · split: `make test-py` / `make test-frontend` · coverage: `make coverage` (or `-py` / `-frontend`) |
| Build frontend | `make build-frontend` (never raw `bun run build` — this also syncs the installed package) |
| Frontend lint | `make lint` (typecheck + prettier + eslint + markdownlint) |
| Build everything | `make build` · local CI: `make ci` |

## Branches & pull requests

- Branch from and target **`dev`**. Never base on or open a PR against `main` — `main` is releases only.
- Branch names use conventional prefixes: `feat/`, `fix/`, `refactor/`, `chore/`, `docs/`.
- Commit subjects are a single conventional line: `type(scope): summary` (no body unless it adds something).
- Add a `CHANGELOG.md` entry under `[Unreleased]` for any user-facing change.
- Before opening a PR: `make lint` and `make test` (and `make build` if you touched the frontend) must pass.

## Frontend conventions

- Bun for JS/TS. Tests run on Vitest + happy-dom and coverage is **gated** — `bun run coverage`
  fails below the floor. Raise the floor as you add tests; never lower it. New components and bug
  fixes ship with a test in `src/__tests__/`.
- No framework: components are plain DOM classes that communicate through the typed event bus in
  `src/events.ts` (`emit`/`listen` + `AppEventMap`) — fire events, don't call other components directly.
- Build elements in code; never `innerHTML` with untrusted/remote strings (escape or use `textContent`).

## Python conventions

- Python 3.10+. Tests are **pytest** (`asyncio_mode = auto`); new code and bug fixes ship with a test in `tests/`.
- Async-first (`aiohttp` / `aiomqtt`) — prefer `async`/`await` over blocking calls in the event loop.
  A synchronous call inside `async def` freezes *every* actor in the process, not just its own —
  MQTT keepalive included. Anything that waits on the network or disk goes through `await`, or
  `asyncio.to_thread` when the library offers no async API. Give every outbound call a timeout,
  and prefer one too generous over one too tight: the bug being prevented is an unbounded wait,
  so a limit that cuts off slow-but-healthy work just trades one failure for another.
- **Imports go at the top of the file.** A function-local import is for exactly two things: an
  optional dependency that must not be required to import the module (`openai`, `torch`,
  `telegram`), or breaking a genuine circular import between `wactorz` modules. Never for the
  standard library, and never for a required dependency such as `aiohttp` or `aiomqtt` — those
  cost nothing at module scope, and hiding them there conceals what a module actually depends on
  and complicates patching in tests. When one is load-bearing, say which of the two reasons
  applies on the line itself. This holds inside `AGENT_CODE` too — a catalogue
  agent's program is a file that happens to be quoted.
- **Paths are `pathlib.Path` inside a module**; accept `str | os.PathLike[str]` at a public
  edge and convert once, on the way in.
- **Define helpers at module level, not inside the function that calls them.** A function
  redefined on every call is rebuilt on every call, cannot be tested on its own, and hides how
  much a loop body is really doing. Decorator wrappers built once at import time are the
  exception.
- **Keep side effects out of `__init__`.** Creating directories, opening files and connecting
  belong in a start method, so an object can be constructed in a test without touching anything.
- **Comments and docstrings are written for a stranger reading the file a year from now.** They
  explain what the code is for and why it is shaped that way. Three things do not belong in
  them:
  - **Counts and measurements.** They are true on the day they are written and misleading
    afterwards. Say "most", or say nothing.
  - **What happened.** No "used to", no "previously", no reference to the bug that prompted the
    change. The reader was not there and cannot check.
  - **Superlatives.** "the largest", "the only", "the last remaining" — all of them decay
    silently.
- Ruff is the gated linter and formatter (`pyproject.toml` `[tool.ruff]`). `make lint-py` runs it,
  plus an advisory pass that reports but never blocks. Pre-commit and CI both enforce the gated
  rules, so a push that skips them fails rather than merging.

## Catalogue agents

`wactorz/catalogue_agents/*.py` hold a runnable agent program as a string in `AGENT_CODE`,
exec'd when the agent is spawned. Two consequences:

- **Ruff and the type checker see a string literal**, so none of the gated rules reach that code.
  A near-zero finding count for these files means nothing was read, not that nothing is wrong.
  `tests/test_catalogue_agent_code.py` parses each program so a syntax error fails a test rather
  than an agent that will not start.
- **The program cannot import `wactorz`** when it runs on a node. What it needs is either stdlib
  or injected into the exec namespace by the host.

## Safety

- **Never commit secrets or `.env*` files.** All credentials come from environment variables (see `wactorz/config.py`).
- **Never ship secrets to the browser.** The dashboard receives only non-secret config from `/api/config`
  (e.g. the Home Assistant URL — never tokens).
- Changes that touch Home Assistant or the add-on must be a no-op for real configured values, so the
  add-on never breaks for existing users.
