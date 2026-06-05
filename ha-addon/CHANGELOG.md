# Changelog

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
