# Changelog

## 0.6.0.1

An add-on rebuild of 0.6.0 — the library is unchanged; these are dashboard fixes
that landed after the 0.6.0 image was built.

- Added: the header names which Wactorz is running. It is read from the server rather than baked into the bundle, so it names the version actually answering rather than the one the dashboard was built against.
- Fixed: the activity feed shows what the house *did*, not only what it measured. It listed the domains to keep, so a light turning on, a door opening or a switch firing could be missing while sensor readings filled the feed — and a domain Home Assistant added later was invisible with nothing to say so. It now names the noisy domains and shows everything else, so an unknown domain appears rather than disappears.
- Fixed: "hide heartbeats" no longer hides Home Assistant activity with it. The filter matched a CSS class that health rows also carried, so turning it on removed rows that were never heartbeats; it now matches what a row actually is.

## 0.6.0

- Added: `api_key` add-on option. It changes nothing for panel users — ingress means Home Assistant has already signed you in — but it is now required if you publish port `8000` or `8888` under Network settings, because Wactorz refuses an unauthenticated wide bind.
- Added: the application log is readable from the dashboard. Startup lines, errors and tracebacks appear in the activity feed alongside agent events, with source, level and search filters; known credential shapes are scrubbed before anything is stored.
- Added: files attached to a chat message now actually reach the model. Images go to every supported provider, PDFs are read inline by Anthropic and Gemini, text files are inlined, and a file that cannot be carried is named rather than dropped. Attachments cap at 25 MB.
- Added: the chat shows that an agent is working, so a slow reply is no longer indistinguishable from one that went nowhere.
- Added: `broker_user` / `broker_password` per entry in `deploy_targets`. `/deploy` now delivers broker credentials to the node over SSH, so a broker requiring authentication works — and `mosquitto_embedded` can serve remote nodes if you publish port `1883`. A node falls back to this add-on's broker account when given none of its own.
- Added: the dashboard reopens on the agent you were last talking to, instead of always starting at `main`.
- Changed: **pausing and resuming an agent are gone — an agent is either running or stopped.** Stopping already did what pausing was reached for, and starting brings it back.
- Changed: **the system agents can now be stopped.** `monitor`, `catalog` and `installer` previously refused every control; they can be stopped and started, and still cannot be deleted. `main` still refuses stop, since stopping it leaves chat unanswered.
- Changed: **edge nodes must be redeployed.** The broker-credential delivery and a package-name fix both ship in the runner, which is a file copied to each machine, so an old node keeps the old behaviour until `/deploy` runs again.
- Changed: the dashboard is sent only the broker topics it actually uses. If you wrote a custom agent whose own topics you were reading from the browser, they are no longer relayed.
- Removed: the OpenTelemetry options (`otel_endpoint`, `otel_service_name`) and the InfluxDB options (`influx_url`, `influx_token`, `influx_org`, `influx_bucket`). Both integrations are gone from Wactorz. Delete these from your add-on configuration if you had set them; point your own collector at `/metrics` instead of OTLP.
- Fixed: chat history reaches the persistent feed again. Every turn handled by an LLM agent was failing to store and the error was swallowed, so a restart came back to an empty conversation even though the agent still remembered it.
- Fixed: a reset now clears what it says it clears. A deleted conversation, fact or setting could be read back from an older state file and reappear — that fallback no longer outlives the reset.
- Fixed: leaving the TTS voice blank now gives you the default voice instead of failing every attempt to speak with `Invalid voice ''`.
- Fixed: an agent that cannot hold a conversation says so, instead of replying with a dump of its own internal state. Asking the Home Assistant actuator a question now gets a sentence.
- Fixed: a dashboard whose connection died is noticed and closed, rather than lingering and receiving broadcasts nobody reads.
- Fixed: a broker that is away no longer grows the outbound queue without limit. Telemetry gives way first; anything queued for guaranteed delivery is written to disk and never discarded.
- Fixed: a state file that cannot be read is moved aside as `<name>.corrupt.<timestamp>` instead of being overwritten by the next save.
- Fixed: a failed framework migration is retried on the next start instead of being recorded as done.
- Fixed: an unrecognised URL can no longer add a permanent new series to `/metrics` on every distinct path.
- Fixed: a port already in use is reported as one line naming the port, not an aiohttp traceback.
- Fixed: agents on a runner node accept lifecycle commands over the REST API instead of answering `404`.

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
