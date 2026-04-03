# Changelog

All notable changes to Wactorz are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versioning follows [SemVer](https://semver.org/).

---

## [Unreleased]

### Added
- **Telegram interface** — new `--interface telegram` mode using `python-telegram-bot`; users self-host their own bot via a BotFather token. Supports `TELEGRAM_ALLOWED_USER_ID` to restrict access to a single user. The `/start` command replies with the user's numeric Telegram ID for easy setup.
- **`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_ID`** env vars added to `config.py` and `.env.example`
- **One-shot Home Assistant actuation** — `MainActor` now classifies immediate device-control requests as `ACTUATE` and routes them to a new ephemeral `OneOffActuatorAgent` that resolves natural language to HA service calls, executes them, reports the result, tracks LLM cost, then unregisters, stops, and deletes its own persistence directory.

### Changed
- **Discord interface** — bot now responds to `@mention` instead of the `!` prefix for a more natural UX. Long responses are automatically split into 2000-character chunks to avoid Discord's message length limit.
- **Documentation** — added README and agent reference coverage for `ACTUATE` intent routing and the new `OneOffActuatorAgent`.

---

## [0.2.0] — 2026-03-13

### Added
- **IOAgent** — MQTT gateway routing `io/chat` messages to the correct actor; replaces direct topic publishing
- **MQTT TCP bridge** in `monitor_server.py` — `/mqtt` WebSocket endpoint now falls back to raw TCP (port 1883) when Mosquitto's WS listener (port 9001) is unavailable
- **Web UI auto-start** — `wactorz` CLI spawns the monitor server as a quiet background asyncio task (`--no-monitor` to opt out, `--monitor-port` to override port 8888)
- **`/api/actors` REST endpoint** on Python monitor server — returns live agent state from MQTT-derived in-memory store
- **`wactorz[all]` wheel** now bundles `static/app/` and `monitor.html` via hatchling `force-include`; custom build hook rebuilds frontend when stale
- **`wactorz/_version.py`** — single source of version truth, imported by `__init__.py` and `pyproject.toml`
- **Rust WS bridge** — `/mqtt` proxy route added alongside `/ws`; `WsBridge` now tracks MonitorState and broadcasts `full_snapshot`/`patch`/`delete_agent` to `/ws` clients
- **`scripts/build.py`** — clean build script (hatchling + twine) with `--upload` flag for PyPI

### Fixed
- **`RangeError: invalid date`** — Python heartbeat uses epoch seconds (`timestamp`); TypeScript normaliser now converts to ms automatically for both Python (snake_case) and Rust (camelCase) payloads
- **MQTT disconnect on listener error** — `emit()` now wraps each listener call in try/catch; a throwing handler no longer crashes the MQTT connection
- **Chat infinite typing indicator** — fixed key mismatch between `showTyping("main-actor")` and `hideTyping("io-agent")`; `IOManager` tracks `_lastTypingKey` and clears it on any reply
- **`llm_agent._handle_task`** — `complete()` returns `(text, usage)` tuple; was incorrectly storing the whole tuple as message `content`, causing Anthropic 400 errors on the second conversation turn
- **CI test failures** — `wactorz/` package was accidentally gitignored; restored source tracking and fixed test import paths for the new package layout
- **`/api/actors` 404** — Python monitor server now serves actor list at this endpoint

### Changed
- `wactorz/__init__.py` — optional agent imports (LLM, HA, ML) now wrapped in `try/except ImportError` so importing any submodule works without all optional deps installed
- Python payload normalisers centralised in `MQTTClient.ts` — `normaliseHeartbeat`, `normaliseChat`, `normaliseStatus`
- Monitor server `_find_dir()` helper resolves `static/app` for both editable and installed-wheel layouts

---

## [0.1.0] — 2025-11-01

### Added
- Initial open-source release
- Python actor model core: `Actor`, `ActorSystem`, `Supervisor` with ONE_FOR_ONE / ONE_FOR_ALL / REST_FOR_ONE strategies
- Built-in agents: `MainActor`, `MonitorActor`, `CodeAgent`, `ManualAgent`, `IOAgent`, `InstallerAgent`, `AnomalyDetectorAgent`
- LLM providers: Anthropic Claude, OpenAI, Ollama, NVIDIA NIM
- MQTT pub/sub telemetry (heartbeat, metrics, status, alert, chat, spawn, logs, completed)
- Babylon.js 3D web dashboard (graph, galaxy, cards, social, fin themes)
- CLI interface (`wactorz --interface cli`)
- REST interface with API key auth
- Discord and WhatsApp interfaces
- Python monitor server (aiohttp) serving dashboard + WebSocket bridge
- Rust axum server with WebSocket bridge and REST API
- Home Assistant integration agents
- Docker Compose stacks (dev and production)
- `pyproject.toml` with optional dependency groups

[0.2.0]: https://github.com/waldiez/wactorz/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/waldiez/wactorz/releases/tag/v0.1.0
