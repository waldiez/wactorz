# Changelog

## 0.4.4

- Added: OpenAI-compatible endpoint support — set `OPENAI_URL` to redirect the `openai` provider to any compatible API (Groq, Together, vLLM, LM Studio, etc.).
- Added: `Actor.notify_user(text)` pushes messages directly to the chat panel.
- Added: `agent.run_in_background(coro)` for long work that shouldn't block `handle_task`.
- Added: `<delegate>` blocks — main agent can delegate tasks via structured blocks alongside `@mentions`.
- Changed: ManualAgent loads now run in the background and notify when ready (no longer blocked by the 60 s timeout).
- Fixed: Chat panel renders agent replies as a Markdown subset (bold, italic, inline code, links, lists).
- Fixed: Delegation via bare `@agent <task>` mentions now correctly dispatches instead of being streamed as prose.
- Fixed: DynamicAgent RESULT replies now echo `_task_id` so `delegate_task` no longer hangs until timeout.
- Fixed: Monitor UI — "Demo fallback" MQTT badge no longer appears when `MONITOR_PORT` differs from the default.
- Fixed: Monitor UI — MQTT WebSocket URL derived from `window.location` on every load, never stale-cached in `localStorage`.
- Fixed: Monitor UI — Service worker fetches `index.html` network-first so fresh JS bundles always load after a redeploy.
- Fixed: Monitor UI — HA / Fuseki config seeding tracks a baseline so `.env` changes (e.g. `HA_URL`) propagate on next load.
- Fixed: Cost tracking — period spend now accumulates even when no cap is configured; weekly period uses ISO week boundaries.
- Fixed: SQLite schema no longer uses `unixepoch('subsec')`, fixing write failures on older SQLite builds (e.g. python.org Windows).

## 0.4.3.2

- Fixed: add-on state now genuinely persists across updates — state directory pinned to an absolute `/data/state` (`WACTORZ_STATE_DIR`) instead of relying on the working directory, which let state fall into the container's ephemeral layer.
- Fixed: embedded Mosquitto retained messages (live overview/cost) now survive restarts and updates — `persistence true` under `/data/mosquitto`, broker pinned to `user root` so it can write the persistence DB.
- Added: developer guide for testing the add-on locally on real HA OS (`LOCAL_TESTING.md`).

## 0.4.3.1

- Fixed: addon state (agents, cost tracking, spawn registry) now persists to `/data` and survives addon updates and restarts.

## 0.4.3

- Fixed: HomeAssistantAgent no longer crashes on non-dict LLM responses in delete/edit flows.
- Fixed: hardware recommendation and entity extraction now read the correct `devices["data"]` key.
- Changed: `create_automation` intent temporarily disabled; requests route to hardware recommendations instead.

## 0.4.2

- Fixed: remote agents are now fully visible to the planner and wired correctly (manifest-driven contract registration).
- Fixed: remote agents now have full API parity with local agents (`subscribe`, `mqtt_get`, `declare_contract`, etc.).
- Fixed: heartbeat no longer overwrites freshly-arrived remote manifests with stale spawn-config contracts.

## 0.4.1

- Added: optional InfluxDB 2.x integration — set `influx_url` in addon config to enable time-series metrics export.
- Added: OpenTelemetry metrics support via `otel_endpoint` config option.
- Fixed: Gemini API key now correctly mapped to `GEMINI_API_KEY` in the container environment.
- Fixed: optional schema fields (`api_key`, `ha_token`, etc.) marked as `str?` so they can be left blank.

## 0.4.0

- Added: global LLM cost limit with configurable period (daily / weekly / monthly) and automatic reset.
- Added: embedded Mosquitto and Fuseki options — run without external addons.
- Added: Discord and Telegram bot token config options.
