# Changelog

## 0.5.3

- Added: `deploy_targets` add-on option — list the remote machines `/deploy <name>` may bootstrap as edge nodes over SSH. Credentials live in the add-on configuration, never in chat; private keys go under `/config` or `/share`. See "Remote edge nodes" in the documentation for the broker requirement — `mosquitto_embedded` cannot serve remote nodes.
- Changed: **the API and dashboard ports are no longer published to your network by default.** The dashboard is reached through the Wactorz panel (ingress), which is behind Home Assistant's own login. If you were reaching `8000` or `8888` directly from another machine, assign a host port for them again in the add-on's Network settings.
- Changed: **the embedded Mosquitto broker now requires authentication.** Credentials are generated once and kept under `/data`, so they survive restarts and updates; Wactorz is pointed at them automatically. Its WebSocket listener on `8083` is gone — the dashboard has used the add-on's own `/mqtt` proxy for some time.
- Changed: the add-on now requests the `default` Supervisor role instead of `admin`. It only reads its own options; `admin` additionally exposed every other add-on's configuration and secrets.
- Removed: the `mqtt_ws_port` option, which no longer had any effect. Delete it from your add-on configuration if you had set it.
- Fixed: dashboard cost and message totals now stay live during a conversation instead of only refreshing on reload.
- Fixed: Home Assistant entities that belong to no device are now visible to the planner and actuator, so agents can act on them.
- Fixed: Claude models that reject a `temperature` parameter no longer fail every request.
- Fixed: quoted values in LLM environment settings (e.g. `"sk-..."`) are now stripped rather than sent as-is, and an unknown override site is reported instead of silently ignored.
- Fixed: the planner no longer waits past its own lifetime cap, and the supervisor no longer restarts retired agents when a sibling crashes.
- Fixed: the REST API now requires its key on every route except `/health`, rejects request bodies that are not JSON objects, and reports real values from `/actors/{id}/metrics`.

## 0.5.2

- Added: `ha_connection` add-on option (`auto` / `supervisor` / `custom`) — explicit Home Assistant connection mode. `auto` keeps the previous token-presence inference, so existing installs are unaffected.
- Added: startup auth probe — the add-on log now shows one deterministic line with the HA connection mode, URL, and auth result (e.g. `HA connection OK — mode=supervisor ...` or `HA auth FAILED (401) ...`).
- Fixed: silent HA misconfiguration — a custom `ha_url` with a blank `ha_token` (or a token set in supervisor mode) now logs a loud warning explaining what is ignored and why, instead of failing quietly.
- Fixed: shellcheck hygiene in `run.sh` (declare-then-export, shebang directive).

## 0.5.1

- Added: `openai_url` add-on option — surfaces the existing OpenAI-compatible endpoint support in the settings UI, so the `openai` provider can be pointed at a LiteLLM proxy or any compatible API (Groq, Together, vLLM, LM Studio) without editing env vars.

## 0.5.0

- Added: optional MQTT broker authentication via `mqtt_username` / `mqtt_password`, wired into runtime MQTT clients and the dashboard MQTT WebSocket proxy without exposing credentials to the browser.
- Fixed: dashboard now binds before the supervisor starts, so slow, unreachable, or auth-rejecting MQTT brokers no longer leave the add-on serving a blank page during boot.
- Fixed: external broker startup now gets a short readiness probe before Wactorz launches, reducing churn when the configured broker is still unavailable.
- Fixed: TTS, agent avatar, and PWA manifest requests now stay inside the Home Assistant ingress prefix, restoring these assets under ingress.
- Fixed: cost totals now survive agent deletion and hard kills more reliably, with deleted-agent spend retained in the all-time total.
- Fixed: agent date/time context now uses the real current time with timezone override support through `WACTORZ_TZ`.
- Removed: remaining Fuseki/SPARQL surfaces and the dashboard Graph tab are gone from the add-on path.

## 0.4.4.2

- Removed: Apache Jena Fuseki / SPARQL entirely. Gone are the bundled JRE 17 + Fuseki tarball (~170 MB), the `fuseki_embedded` option, all `fuseki_url` / `fuseki_dataset` / `fuseki_user` / `fuseki_password` options, and addon port `3030`. The UI "Graph" tab has also been removed. Wactorz runs without a triplestore.

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
