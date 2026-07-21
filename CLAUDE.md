# CLAUDE.md

Guidance for Claude Code (and human contributors) working in this repository.
Keep changes small, tested, and consistent with what's already here.

## What this is

**Wactorz** — an actor-model multi-agent framework. Agents spawn, coordinate, and
monitor each other at runtime over MQTT. A Python `aiohttp` monitor server exposes a
REST + WebSocket API and serves a framework-free TypeScript dashboard (SPA).

## Layout

- `wactorz/` — the Python package: `agents/`, `core/`, `interfaces/`, `monitor/` (the aiohttp
  monitor: `app` routes + `runtime` shared state + a module per concern), `ext/` (optional
  features), `cli.py`, `config.py`. `monitor_server.py` is a thin back-compat shim.
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
- A linter/formatter/type-checker is being introduced via `pyproject.toml` `[tool.ruff]` — run it
  before pushing once it lands. Until then, match the style of the file you're editing.

## Safety

- **Never commit secrets or `.env*` files.** All credentials come from environment variables (see `wactorz/config.py`).
- **Never ship secrets to the browser.** The dashboard receives only non-secret config from `/api/config`
  (e.g. the Home Assistant URL — never tokens).
- Changes that touch Home Assistant or the add-on must be a no-op for real configured values, so the
  add-on never breaks for existing users.
