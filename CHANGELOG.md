# Changelog

All notable changes to Wactorz are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased] — pending

> **sinergym branch:** synced with `dev`. Unlike `dev`, this branch keeps Fuseki
> (server container, HA→Fuseki bridge, `/api/fuseki` proxy, Graph tab), extra
> agents, and the `sinergym/` + `models/` experiment trees.

### Added

- **Real `did:swid` identities (SWF-STD-5)** via the `waldiez-swid` library. Agents, HA devices,
  and areas get a key-bound DID (Ed25519 key in an encrypted keystore, CEL log in a file registry)
  plus a readable handle carried in the DID document's `alsoKnownAs`; DIDs resolve at
  `GET /1.0/identifiers/{did}` and are linked on the Fuseki graph nodes (`swidns:did`/`swidns:handle`).
  Minting is enabled by setting `SWID_KEYSTORE_PASSPHRASE` (empty = handles only). See
  `SWID_DATA_DIR` / `SWID_HSTP_BASE` in `.env.template`.

### Changed

- **Device `swid` labels are now readable handles** (`swid:device:<ns>:<slug>-<fp>`), replacing the
  pilot `did:swid:home:<area>:<name>-<hash>` format that squatted the official `did:swid:` namespace.
  Handles are room-independent: a device that moves rooms keeps its handle.
- **Dashboard uses a single WebSocket transport.** Live agent/system/node data and Home Assistant
  activity now stream to the browser as server-push over `/ws`; the dashboard no longer opens its own
  MQTT connection to the broker, and the browser receives no broker credentials.

### Fixed

- **Remote agent vanish-detection** — a remote agent missing from a single node heartbeat is no longer pruned from the registry (which broke delete and node-reboot recovery); pruning now needs several consecutive misses, and never touches an agent that hasn't appeared yet or has migrated away.
- **Chat input** — up-arrow history recall now grows the textarea to fit a multi-line message instead of clipping it to one line.
- **Nodes panel** — remote-runner agents no longer also appear under the local node; each agent is listed only on the node it runs on.
- **Remote agent delete** — deleting an agent that runs on a remote node now stops it on the node instead of only clearing the server's records, so it no longer keeps running and reappearing.
- **Remote agents flicker / chat misroutes to main** — the dashboard dropped the `node` field when mapping WS state patches, so the 15s /api/actors reconcile (local-only) repeatedly evicted remote agents. They now keep their node marker and survive the reconcile.

## [0.5.1] - 2026-07-06

### Added

- **`openai_url` add-on option** — surfaces the existing OpenAI-compatible endpoint support in the
  Home Assistant add-on settings, so the `openai` provider can be pointed at a LiteLLM proxy or any
  compatible API (Groq, Together, vLLM, LM Studio) without editing env vars.
- **Named volume presets for reachy** - `whisper` (70), `normal` (85), `louder` (93)
  and `presenter` (100) speaking modes, mapped to the robot speaker's usable loudness band.
  Deterministic "whisper X" / "say X softly|loudly" set the level and speak aloud.
- **`wactorz-google-login` command** — one-time interactive OAuth login for the Calendar and
  Gmail agents (`wactorz-google-login [calendar|gmail|both]`). Mints/refreshes the token with
  the scopes Wactorz needs (no `gmail.metadata`) so the agents work without hand-running a
  snippet. `.env.template` now documents the `CALENDAR_MCP_*` / `GMAIL_MCP_*` keys.
- **Docs: "Catalogue agents" section** — a new documentation section with an overview and a
  per-agent page for every catalogue recipe (Google Calendar, Gmail, Weather, Smart Energy,
  Anomaly Detector, Device Manuals, Doc → PPTX, Time-Series Collector), plus a shared Google
  setup/login page. Replaces the old standalone Calendar MCP page.
- **Global error surfacing** — uncaught exceptions and unhandled promise rejections
  now log with context and raise a toast, instead of failing silently to the console.
- **Content-Security-Policy on the dashboard** — the monitor server sends an enforcing
  CSP with a per-request nonce for inline scripts and `frame-ancestors 'self'`, restricting
  script/connect/worker/style/img sources. Works behind Home Assistant ingress (the header is
  forwarded and framing is same-origin); rolled out via a report-only pass verified on both
  standalone and ingress before enforcing.
- **Per-agent token counts** — LLM agents' cumulative input/output tokens now show on
  their card next to cost (compact `12.5k↑ 900↓`); non-LLM agents show nothing, as
  before. The counts were already on the wire but previously parsed and dropped.
- **More activity-feed sources** — the dashboard feed now surfaces agent actuations
  (what an agent actually changed) and anomaly events, via an extensible topic
  registry so further feed-only topics are a one-line addition.
- **Pipeline-rule conflict advisory** — planner now semantically checks a new rule
  against active ones and flags duplicates and contradictions (e.g. "over 25° AC off"
  vs "AC on") as a non-blocking "⚠️ Heads up" note at approval.
- **Weather catalog agent** — `@catalog spawn weather-agent` adds an optional manual weather helper backed by Open-Meteo for current conditions, forecasts, historical weather, default locations, and weather-related natural-language questions.
- **Smart energy catalog agent** — `@catalog spawn smart-energy` adds an optional Home Assistant smart-plug helper for plug discovery, live wattage, kWh/cost tracking, and guarded user-requested auto-off rules.
- **Calendar & Gmail are optional catalogue agents** — `google-calendar-agent` and `gmail-agent` are spawned on demand from the catalogue and reached through normal agent delegation (so the main actor can combine them with other agents), not wired into the core message loop. Installs that don't add them are unaffected: everyday words like "meeting", "event", or "agenda" no longer get intercepted and routed to a Google agent that isn't there.
- **Tests** — `test_spawning.py` (23) covering spawn routing, idempotency/replace,
  both install models, the `trusted` flag and TopicContract wiring; `test_memory.py`
  (12) covering fact extraction/namespacing and system-prompt assembly.
- **Home Assistant entity history** — `home-assistant-agent` can now answer questions
  about past entity states (e.g. "what was the office temperature yesterday at 17:00?"),
  backed by a new `get_entity_history` helper against HA's `/api/history/period` REST
  endpoint. The current local datetime is injected into the LLM prompt so relative
  times resolve correctly, and returned timestamps are localised to the server's
  timezone. Also available as a structured `get_history` A2A operation for peer agents,
  which returns the history as CSV alongside the raw JSON.
- **Gmail agent (`gmail-agent`)** — new catalog-backed native agent that searches/reads
  mail, lists labels and drafts, and creates drafts. Draft-first by design (like Google's
  hosted Gmail MCP, it never sends). Hosted Gmail MCP primary with a Gmail REST v1 fallback
  on `PERMISSION_DENIED`, mirroring the calendar agent.
- **Gmail: read an email's full contents** — new `read` action opens one message (by id, or the
  top match of a topic query) and returns its readable body — text/plain, or HTML stripped to
  text — so "what does the trello one say?" / "content of the latest vodafone bill" return the
  actual message instead of a search snippet list.
- **Gmail read answers your question** — when an LLM is configured, `read` now returns a
  concise answer to the asked question (or a short summary), quoting exact amounts/dates,
  instead of dumping the raw email; bodies are also de-noised (tracking URLs → `[link]`,
  collapsed blank lines). Without an LLM it returns the cleaned body.
- **Direct OAuth login** (`GoogleMcpClient.authorize_direct`) — mints a REST token via a
  direct Google OAuth flow with caller-chosen scopes, bypassing the MCP server's scope set.
  Used to get a Gmail token **without** `gmail.metadata` (which blocks free-text `q` search),
  so `find invoices` / `from:…` work; also guarantees a refresh token.
- **Shared Google-MCP base** (`core/integrations/google_mcp.py`) — `GoogleMcpClient` +
  `GoogleMcpConfig` factor out the OAuth token storage, MCP call, and REST-fallback plumbing;
  Calendar and Gmail are now thin subclasses. MCP auth during tool calls is **non-interactive**
  (silent refresh only, with a timeout) so a server-side agent fails fast to REST instead of
  blocking on a browser OAuth window; interactive login is an explicit `login()`.
- **Calendar REST fallback** — `GoogleCalendarMcpClient` now falls back to the
  Google Calendar REST API (v3) with the same OAuth token when the hosted Calendar
  MCP (`calendarmcp.googleapis.com`) returns `PERMISSION_DENIED` (it is access-gated
  and denies tool execution even for fully-scoped tokens) or the `mcp` extra isn't
  installed. MCP stays the primary path and resumes automatically once the project
  is allowlisted. Events are rendered as readable lines instead of raw JSON.
  which returns the history as CSV alongside the raw JSON.

### Changed

- **Reachy agent renamed `reachy-body` → `reachy-mini`** — the catalogue agent, its
  recipe file, spawn command (`@catalog spawn reachy-mini`), planner references, and
  documentation all use the new name. MQTT topics (`custom/reachy/*`) are unchanged.
- **Reachy Home Assistant routed through the HA agent** - reachy-mini delegates all
  device control and automations to `home-assistant-agent` (natural-language
  `{cmd:ha, request}`) instead of calling HA's REST API directly; entity discovery for
  reactive binds is routed through the HA agent too.
- **Home Assistant "Devices" → direct link** — the dashboard's embedded device
  list/control panel was replaced with a "Devices" button that opens Home
  Assistant's own UI in a new tab (using the URL from `/api/config`). HA entity
  activity still appears in the activity feed via the MQTT state bridge.
- **`main_actor.py` decomposed** (6113 → ~4400 lines) with no behaviour change:
  prompts → `agents/prompts/main_actor_prompts.py`, constants + pure helpers →
  `agents/helpers/main_actor_helpers.py`, and two behaviour mixins →
  `agents/mixins/{spawning,memory}.py`. `planner_agent.py` lost ~200 lines of
  duplicated spawn code. New `agents/mixins/` and `agents/helpers/` subpackages
  keep `agents/` to actual agents only.
- **Unified planner JSON parsing** — both decomposition paths share
  `_extract_json_array` instead of fragile fence-stripping.
- **Continuous agents declarable** — `_ensure_agents` honours
  `spawn_config["continuous"]` before falling back to code substring-matching.
- **ha_actuator name collisions** now keyed on agent name (was `automation_id`);
  a colliding actuator may get a different suffixed name.
- **`type: "manual"` spawn configs** now route correctly through `MainActor`
  (previously fell through to a no-op).
- **`_is_pipeline_request`** is now a proper `@staticmethod`.

### Removed

- **Flutter companion app** — the `mobile/` Flutter project (iOS/Android companion
  app) and its `test-mobile` CI job were removed. The web dashboard and REST/WS
  API remain the supported clients.

### Fixed

- **Agent chat no longer appears twice** — in `direct_ws` mode the dashboard was rendering
  every `notify_user` frame (e.g. reachy's spoken replies) twice: once from the monitor's
  WebSocket relay and once from the browser's own `agents/#` MQTT subscription. The browser
  now honours the single-transport-per-mode invariant and ignores the redundant MQTT copy.
- **Reachy no longer echoes unparsed input** - when the planner can't turn a message
  into a robot action, reachy returns a helpful hint instead of repeating the user's words.
- **Frontend minor fixes** — synthesised chat/WS message ids now use collision-free
  WIDs instead of `<prefix>-<ms>` (two messages in the same millisecond could collide and
  be dedupe-dropped); the dashboard now asks not to be indexed (`robots: noindex, nofollow`
  — it is an ops control surface); chat-history params renamed `agentId` to `agentName` to
  match what they actually receive; a11y attributes added (lightbox `role="dialog"`/
  `aria-modal`, popover `aria-haspopup`/`aria-expanded`/`aria-controls`, view tabs
  `aria-current="page"`).
- **Dashboard could fail to load in private-mode / storage-disabled browsers** —
  `localStorage` access during startup threw (Safari private mode, storage disabled,
  quota exceeded), aborting bootstrap with a blank page. All access now routes through
  a `safeStorage` wrapper that degrades to `null`/no-ops, so the dashboard loads and
  persistence is best-effort.
- **Dashboard reliability nits** — the live-actor refresh now times out after 10s (a
  hung request could otherwise wedge every later refresh); chat/upload message ids use
  collision-free WIDs instead of `Date.now()`; loaded chat history is capped like the
  live feed; and the service worker no longer skips caching sibling paths like `/wsfoo`.
- **@mention could silently fail to switch target** — the mention list offered every
  agent, but only messageable agents are in the target picker, so mentioning a
  non-messageable one left the placeholder claiming a target that was never set.
  Suggestions now mirror the picker (messageable only), and accepting an untargetable
  name is a clean no-op.
- **Planners leaked until restart** — proposal/pipeline planners never stopped and
  stayed pinned by both the registry and the Supervisor. Added a lifetime watchdog
  (`max_lifetime_s`, 10 min) + idempotent `_terminate()` doing `release()` →
  `unregister()` → `stop()`.
- **Plan steps silently dropped** — bad/cyclic `depends_on` aborted the plan with no
  trace; references are now validated and failures surfaced per-step.
- **`plan_only` could spawn agents** — `approved_plan` was checked first despite the
  docs; precedence is now enforced in `on_start`.
- **Planner-spawned agents silently missing setup** — `PlannerAgent` carried its
  own drifted copy of the spawn logic, so dynamic agents it spawned skipped
  migrated-state injection, TopicContract auto-wiring, and the `trusted` flag
  (catalog agents were needlessly re-run through the safety validator). Spawn and
  install logic for `MainActor` and `PlannerAgent` is now a single shared
  `SpawnMixin`, so an agent behaves identically regardless of which one spawns it.
  ~550 lines of duplication removed.
- **Headless `cli` interface self-shutdown** — `wactorz --interface cli` with no TTY (piped, Docker without `-it`, systemd) booted fully then tore the whole system down ~1s later: `input()` raised `EOFError` immediately, finishing the interactive loop, and with `run_forever()` already a no-op there was nothing left keeping the process alive. The `cli` interface now detects a non-interactive stdin and stays up via `run_forever()` instead of starting the interactive loop.
- **Calendar agent "No time zone found with key UTC"** — every read (`today`,
  `week`, `list_events`) crashed on systems without the IANA tz database (e.g.
  Windows without `tzdata`), where even `ZoneInfo("UTC")` raises. Timezone
  resolution now falls back to the system-local offset and only sends Google a
  `timeZone` key when it's a valid IANA zone (a bare `UTC` is rejected too).
- **OAuth refresh token wiped on first refresh** — the MCP token storage replaced the whole
  token blob on every refresh, but Google omits `refresh_token` from refresh responses, so the
  first silent refresh dropped it and permanently broke re-auth (surfaced once an access token
  expired). Storage now preserves the initial refresh token. Affects Calendar and Gmail.
- **Calendar "show events" flooded with recurring instances** — the upcoming-events list had no
  time bound, so a yearly recurring event (e.g. a birthday) returned many past/future copies that
  looked identical. It's now bounded to now → +1 year, and cross-year dates include the year.
- **Gmail HTML entities not decoded** — message subjects and snippets showed raw entities
  (`didn&#39;t`, `&quot;`, `&amp;`); they're now unescaped to real characters.
- **Gmail draft follow-up dropped bare replies** — after "make an email to X" the agent asked
  "what should the email say?" but a plain reply (e.g. "Bloop") wasn't captured as the body. It
  now fills whichever field it just asked for (body if the recipient is known, or the recipient
  if the reply is an email address). "make an email/draft …" phrasings are also recognised.

---

## [0.5.0] - 2026-06-22

### Added

- **`WACTORZ_TZ` env var** — optional override for the timezone used in agents' date/time context. Precedence: a user's `pref_timezone` fact > `WACTORZ_TZ` > standard `TZ` env var > host local zone. Blank = unchanged (falls through to `TZ` / system local), and any unknown zone value falls through to the next candidate rather than erroring.
- **MQTT broker authentication** — optional `MQTT_USERNAME` / `MQTT_PASSWORD` (add-on options `mqtt_username` / `mqtt_password`) inject broker credentials into every in-process MQTT connection via a central `mqtt_client()` factory. Blank = anonymous, so the embedded/anonymous broker is unchanged; auth only engages when set. Fixes external brokers with `allow_anonymous false` — e.g. the official Home Assistant Mosquitto add-on — which previously rejected every connection. The dashboard's MQTT WebSocket proxy injects the same credentials into the browser's CONNECT server-side, so the live monitor keeps working under an authenticated broker without exposing credentials to the browser.

### Changed

- **TimeSeriesCollector** — moved from an auto-started supervised actor to an on-demand catalog agent (`@catalog spawn timeseries-collector`); no longer started at boot.

### Removed

- **Rust, Node, and Tauri backend surfaces** — removed the Rust workspace/backend, Node backend, Tauri desktop shell, native packaging scripts, desktop release workflow, and backend parity harness. Wactorz now ships the Python runtime, web dashboard, Docker/Compose paths, and Home Assistant add-on path.
- **Apache Jena Fuseki / SPARQL — removed entirely** across the whole product. Gone are: the Python `fuseki.py` / `fuseki_proxy.py` / `fuseki_agent.py` / `sparql_context.py` / `smart_cities_agent.py` and their wiring (HA→Fuseki bridge, `/api/fuseki` proxy, `/api/ha/sync`, planner SPARQL enrichment, `config.py` fuseki fields, `wactorz-fuseki` entry point); the Rust `FusekiAgent`, the `/api/fuseki` proxy + its tests, the `--fuseki-*` CLI args, and the Fuseki RDF writes in the HA→state bridge (now HA→MQTT only); the Node `FusekiAgent`; the UI **Graph** tab + its HUD link/CSS; the embedded Fuseki in the HA add-on; the `fuseki` Docker services (`compose.yaml` / `compose.dev.yaml`) + image (`config/fuseki-container/`), the ontology (`infra/fuseki/`), the nginx `/fuseki/` proxy, the Prometheus Fuseki probe, and all `FUSEKI_*` env/docs. Wactorz no longer ships or depends on a triplestore.
- **Stale `wactorz/main.py`** — removed the unused 5.6k-line embedded entry point (no longer importable, referenced nowhere). Internal cleanup; no runtime behaviour change.
- **Babylon.js 3D dashboard layer** — removed the unused 3D scene engine and the disabled social/theme-switcher (graph/galaxy themes, `SocialDashboard`, `ThemeSwitcher`). The dashboard is cards-only; this drops the `@babylonjs/*` dependencies and the 6 MB `babylon-core` bundle, cutting the frontend from 2204 → 27 modules and the cold build from ~19s → ~2s. `SceneManager` is retained as a plain agent-state/dashboard coordinator (public API unchanged).

### Fixed

- **Agents now anchored to the real current date/time** — every LLM-backed agent receives a live "current date & time" block at the top of its system prompt on each turn, so requests like "notify me tomorrow at 3pm" resolve against today's actual date instead of the model's training-cutoff guess (which defaulted to 2025 and silently produced wrong schedule dates). Injected in three previously-static spots: `LLMAgent`'s `complete`/`stream` calls (covers main and every base-class agent), `PlannerAgent`'s feasibility / pipeline-architect / task-planner calls (where a request is decomposed into a `schedule_spec`), and the synthesized remote LLM-agent bridge. The timezone resolves from the user's `pref_timezone` fact — the same source `ScheduledAgent` already fires against — for main and the planner, so what the model thinks "tomorrow" means now matches what actually gets scheduled.
- **HA add-on blank page on boot** — the monitor web UI now binds *before* the supervisor starts, so a slow, unreachable, or auth-rejecting MQTT broker no longer leaves the add-on serving a blank page; the dashboard is reachable immediately and the overview fills in as agents register. `run.sh` also probes an external (non-embedded) broker for up to 15s before launch so wactorz doesn't churn against an unreachable broker at boot.
- **Headline cost total** — the dashboard's total no longer drops below the visible cards. It now resolves each agent's cost from the same three sources the cards use (MQTT state → live actor → persisted `_final_cost`), and a durable, monotonic per-`actor_id` ledger (fed by each agent's heartbeat `cost_usd`, persisted under `_system`) keeps deletions and hard kills from ever lowering the total. A full metrics reset clears the ledger so the total can still be zeroed deliberately.
- **Headline cost total drops on agent deletion** — follow-up to the headline-total fix above. The total could still read *lower* than the "this period" spend shown beside it (an impossible state) because it derived from delete-fragile sources: per-agent `_final_cost` rows are purged on delete, and the heartbeat-fed per-`actor_id` lifetime ledger can miss/lose short-lived agents. A new durable `_global_cost_alltime` counter is accrued at call time via the same path as the per-period spend buckets — so it is never reduced by a single agent's deletion or per-agent metrics reset — and is used as a third floor for the headline total (`max(live + historical, lifetime ledger, all-time counter)`). Deleted agents' spend is now retained and `this period ≤ all-time` always holds. The counter is seeded once from existing durable totals on upgrade and zeroed by a full cost/metrics reset; cap enforcement is unchanged (it still reads the per-period counter).
- **HA add-on ingress URL escaping** — TTS, agent-avatar, and PWA-manifest requests now stay inside HA's `/api/hassio_ingress/<token>/` prefix. `TTSManager` fetched bare `/api/tts/voices` and `/api/tts`, and `AgentImageGen` returned root-absolute `/avatars/*.webp` — all of which resolve against HA core and 404 under ingress (server edge-tts silently fell back to browser voices; agent avatars failed to load). TTS now uses the same ingress-aware `_apiBase` as the rest of the UI, avatars use relative `./avatars/*` paths, and the `<link rel="manifest">` gained `crossorigin="use-credentials"` so the browser sends the ingress auth cookie (was a 401 on `site.webmanifest`).
- **Dashboard XSS hardening** — agent names, tasks, and bios (set by spawned/LLM agents over MQTT) were interpolated raw into `innerHTML` in the social-card and chat-list views. A new `escapeHtml()` helper now escapes them, so an agent named `<img onerror=…>` can no longer execute script in the dashboard.
- **Duplicate user message after history load** — a user's chat message could render twice: the optimistic echo used id `user-<ts>` while the persisted copy from `/api/chats` used `hist-<agent>-<rowid>`, so `_loadHistory`'s id-based de-dup never matched them. The persisted copy is now reconciled with its pending optimistic echo (same target + content + a tight timestamp window), adopting the persisted id instead of appending a second bubble. The window avoids collapsing a new message that merely repeats an older identical one, and when no optimistic copy exists the persisted message is still added — so a message that wasn't on screen is never hidden.

---

## [0.4.4] - 2026-06-08

### Added

- **OpenAI-compatible endpoint support** — set `OPENAI_URL` to redirect the `openai` provider to any compatible API (Groq, Together, vLLM, LM Studio, llama.cpp server, etc.) without a separate provider choice. `OpenAIProvider` now accepts an optional `base_url`; `OPENAI_URL` in `.env` feeds it automatically. When unset, behaviour is identical to before.
- **Agent → UI notifications** — `Actor.notify_user(text)` pushes a message to the chat panel (via `agents/{id}/chat`); the monitor bridges it to a live chat frame. Previously agent messages only hit the dashboard.
- **`agent.run_in_background(coro)`** — schedules a coroutine tracked on the actor, for long work that shouldn't block `handle_task`.
- **`<delegate>` blocks** — `main` can delegate via `<delegate>{"agent": "...", "task": "..."}</delegate>`, alongside `@mentions`.
- **HomeAssistantAgent — camera tools** — three new tools exposed to the LLM tool-call loop and to A2A structured dispatch:
  - `list_camera_entities` — returns all `camera.*` entities with their current state and friendly name.
  - `get_camera_snapshot` — fetches a JPEG from `/api/camera_proxy/{entity_id}`, returns it base64-encoded; the agent appends an inline `![…](data:image/jpeg;base64,…)` markdown tag to the final reply so the chat panel renders the image.
  - `get_camera_stream_url` — aggregates stream URLs from three sources: the always-available MJPEG proxy (`/api/camera_proxy_stream/{entity_id}`), the [Expose Camera Stream Source](https://github.com/felipecrs/hass-expose-camera-stream-source) custom integration if installed (`/api/camera_stream_source/{entity_id}`, returns plain-text URL, silently skipped on 404), and HLS/other formats via HA WebSocket `camera/capabilities` + `camera/stream` (relative URLs are resolved to absolute).
- **HomeAssistantAgent — A2A camera dispatch** — a peer agent can send a structured payload to `home-assistant-agent` without triggering any LLM call: `{"operation": "list_cameras"}`, `{"operation": "get_camera_snapshot", "camera_entity_id": "camera.x"}`, `{"operation": "get_camera_stream_url", "camera_entity_id": "camera.x"}`, or `{"operation": "get_camera_snapshot_url", "camera_entity_id": "camera.x"}` (returns only the URL, no HTTP fetch).
- **ha_helper — camera helpers** — five new functions: `get_camera_entities`, `get_camera_snapshot`, `get_camera_stream_url` (sync, MJPEG proxy URL only), `get_camera_stream_urls` (async, all sources), `get_camera_snapshot_url` (sync, returns `/api/camera_proxy/{entity_id}` URL without fetching).
- **PlannerAgent — camera URL resolution** — before generating a plan, the planner resolves real stream and snapshot URLs for camera entities mentioned in the task via A2A requests to `home-assistant-agent`. Resolved URLs are injected into the LLM prompt so generated agents never guess `/dev/video0` or invent proxy paths. MJPEG proxy URLs require a Bearer token; the planner injects an `OPENCV_FFMPEG_CAPTURE_OPTIONS` hint into PATTERN 3 so OpenCV passes the header automatically.
- **PlannerAgent — PATTERN 7** — new plan pattern for one-shot camera snapshots (e.g. "take a snapshot of the office camera"). Uses `httpx` to fetch the snapshot URL with an `Authorization: Bearer` header rather than opening a continuous `cv2.VideoCapture` stream.

### Changed

- **ManualAgent** — user-facing loads now ack immediately and run search/download/extract in the background, notifying when ready (no longer blocked by the 60 s `handle_task` timeout). Programmatic `action: load_manual` stays synchronous.
- **Orchestrator prompt** — added a "HOW TO DELEGATE" section and removed the contradictory "NEVER PROXY" guidance.

### Fixed

- **HA add-on persistence** — state (chat, agents, cost, spawn registry) now reliably survives add-on **updates**, not just restarts. The state directory resolves from `WACTORZ_STATE_DIR` (absolute `/data/state` in the add-on) instead of a CWD-relative `./state`, so it no longer lands in the container's ephemeral layer; `wactorz-reset` honours the same path.
- **HA add-on embedded Mosquitto** — retained messages (live overview/cost) now persist across restarts and updates: `persistence true` under `/data/mosquitto`, with the broker pinned to `user root` so it can actually write there.
- **Delegation never dispatched** — bare `@agent <task>` mentions in `main`'s output were streamed as prose, not dispatched. `_execute_llm_delegations` now matches them (line/sentence-anchored).
- **Recipe-agent replies dropped** — `DynamicAgent` RESULT replies didn't echo `_task_id`, so `delegate_task` hung until timeout. They now echo it, matching `LLMAgent`.
- **Monitor UI** — "Demo fallback" MQTT badge no longer appears when `MONITOR_PORT` differs from the default 8888. `config_handler` was advertising a hardcoded `:8888` WebSocket URL to the frontend; it now uses the actual bound port (`WS_PORT`).
- **Monitor UI** — MQTT WebSocket URL is derived from `window.location` on every load and never cached in `localStorage`. Existing browsers with a stale cached URL (e.g. `ws://…:8888/mqtt`) self-heal automatically on next page load — no manual `localStorage` clearing required.
- **Monitor UI** — Service worker now fetches `index.html` network-first so fresh content-hashed JS bundles always load after a redeploy (fixes stale-SW Demo fallback in normal vs incognito browsing).
- **Monitor UI** — HA / Fuseki config seeding now tracks a `__server` baseline so `.env` changes (e.g. `HA_URL`) propagate to the browser on next load instead of being permanently shadowed by the first-seen value.
- **Cost limit** — Period spend now accumulates even when no cap is configured. Previously `_accumulate_global_cost` skipped bookkeeping unless a limit was set, so enabling a cap mid-period gave false protection (spend already incurred this period was never recorded and the cap could be silently overshot), and the "Current spend (no limit set)" readout was permanently `$0`.
- **Cost limit** — Weekly budget period now keys on the ISO week (`%G-W%V`) instead of `%Y-W%W`, which produced a partial `W00` bucket at the start of January and week boundaries that didn't align with Mon–Sun.
- **Monitor UI** — "Reset spend" button now states explicitly that it clears only the current period's budget counter and leaves the lifetime "Cost" total unchanged (use `wactorz-reset --metrics` for that), removing confusion between the two separate accumulators.
- **Persistence** — SQLite schema no longer uses `unixepoch('subsec')` (requires SQLite ≥ 3.42, 2023) for column DEFAULTs. SQLite resolves a DEFAULT's functions when compiling *any* write to the table, so on older bundled SQLite (e.g. python.org Windows builds) every write to `kv_store`, `spawn_registry`, and other config tables failed with `unknown function: unixepoch` — silently breaking cost tracking and agent persistence. Replaced with a portable `julianday()`-based expression (core since SQLite 3.0), keeping sub-second precision. Deploy images and CI were unaffected; this fixes local/dev pip installs on any platform.

### Tests

- **Tests** — `mqtt.test.ts`: updated stale assertion for the 6 s disconnect-debounce introduced in a prior PR.
- **Tests** — `test_persistence_writes.py`: new coverage for the real `WactorzDB` write path (the suite previously only used an in-memory fake), including a guard against reintroducing version-gated SQLite functions in the schema.
- **Tests** — `test_ha_helper.py`: 17 new tests in `HomeAssistantHelperCameraTest` covering all four camera helper functions, including URL normalisation, relative-URL resolution, silent 404 skip, web_rtc stream-call exclusion, and exception fallbacks.
- **Tests** — `test_home_assistant_agent.py`: 23 new tests in `HomeAssistantAgentCameraTest` covering the LLM tool loop, A2A structured dispatch, heuristic routing, and snapshot image appending for all three camera tools (plus the `get_camera_snapshot_url` A2A operation added in the follow-up commit).

## [0.4.3] - 2026-06-01

### Changed

- **HomeAssistantAgent** — `create_automation` intent is temporarily disabled; requests are routed to `_recommend_hardware` instead.
- **HomeAssistantAgent** — Edit automation flow refactored into three focused helpers (`_identify_automation`, `_get_automation_config`, `_generate_modified_automation_config`) with `AutomationEditError` for internal error propagation.
- **HomeAssistantAgent** — All LLM system prompts extracted to `wactorz/agents/prompts/home_assistant_prompts.py`.
- **ha_helper** — Type hints modernised (`Optional[str]` → `str | None`, `List[Dict]` → `list[dict]`); URL helpers reorganised; `get_automations` rewritten.

### Fixed

- **HomeAssistantAgent** — Non-dict LLM response no longer crashes the delete/edit path (guard ordering corrected).
- **HomeAssistantAgent** — Stale `devices["devices"]` key corrected to `devices["data"]` throughout hardware recommendation and entity extraction helpers.

### Tests

- Comprehensive test suite added for `ha_helper` (`tests/test_ha_helper.py`) and `HomeAssistantAgent` (`tests/test_home_assistant_agent.py`).

## [0.4.2] - 2026-05-21

# Remote-Agent Consistency Fixes

This changelog documents a series of fixes that close the gap between local and
remote agent behaviour. Before these changes the framework worked well for local
agents but broke in many ways for remote ones, with inconsistencies clustering
around topic registration, agent API parity, migration, and LLM agent support.

Each fix below is described with its symptom, root cause, and resolution.

## Files touched

- `main_actor.py`
- `remote_runner.py`
- `dynamic_agent.py`
- `actor.py` (read-only in this pass; no edits)

---

## Fix 1 — Manifest listener is the source of truth for remote contracts

**Symptom.** The planner knew remote agents existed (they appeared in
`list_capabilities`) but auto-wiring routed traffic to the wrong topics. Local
agents wired correctly; remote ones did not.

**Root cause.** Local agents register their `TopicContract` directly with the
`TopicBus` whenever they `publish()`, `subscribe()`, or call
`declare_contract()`. Remote agents have no local `TopicBus` — they advertise
themselves via a retained MQTT manifest. Main's `_manifest_listener` was
storing those manifests in `_agent_manifests` and `_topic_registry` but was
**not** translating them into `TopicContract`s on the central bus. The planner
read from the bus, so remote agents were invisible to wiring.

**Fix.** `_manifest_listener` now builds a `TopicContract` from every incoming
manifest and calls `bus.register_contract(...)`. `observed_samples` is folded
into `produces_schema` so the real wire format wins over LLM-declared guesses.
Tombstone payloads (empty retained messages) call `bus.unregister(name)` so
deleted agents stop appearing as wiring candidates.

---

## Fix 2 — Remote runner ships full contract data in its manifest

**Symptom.** Even after Fix 1, remote contracts were missing `subscribes`,
`triggers_when`, and accurate schemas.

**Root cause.** `_RemoteAgentAPI._publish_manifest()` only shipped `publishes`.
The local `_AgentAPI._publish_manifest()` ships the full TopicContract surface.

**Fix.** `_RemoteAgentAPI._publish_manifest()` now ships `subscribes`,
`triggers_when`, `produces_schema`, `consumes_schema`, and `observed_samples`,
matching the local shape exactly. Pre-declared spawn-config topics are unioned
into `publishes` so the planner sees them even before the first `publish()`
call. Schemas are also auto-captured per publish — every `publish()` call
records field names and types into `observed_samples`, and the manifest
re-publishes on schema change.

---

## Fix 3 — Heartbeat-driven contract refresh is strictly non-overwriting

**Symptom.** Sometimes a freshly-arrived correct manifest would be replaced
seconds later by a stale spawn-config-derived one.

**Root cause.** A heartbeat handler in `main_actor.py` was rebuilding remote
contracts from `from_spawn_config(...)` on every heartbeat, clobbering the
manifest-derived contract.

**Fix.** The heartbeat refresh now skips any agent that already has a contract
on the bus or a manifest in main's cache, and only bootstraps from spawn config
when the config declared real topics. The manifest path is the single source of
truth.

---

## Fix 4 — Full API parity between local and remote agents

**Symptom.** Remote agents crashed in `setup()` with
`'_RemoteAgentAPI' object has no attribute 'declare_contract'`. Migrating an
agent that called `agent.subscribe(...)` made it stop listening. Code that used
`agent.window(...)` or `agent.mqtt_get(...)` worked locally and silently broke
remotely.

**Root cause.** `_RemoteAgentAPI` was missing methods that `DynamicAgent._AgentAPI`
exposes. Agent code written for the local API would crash on the remote.

**Fix.** Added to `_RemoteAgentAPI`:

- `subscribe(topic, callback)` — background MQTT listener with dedup, callback
  validation, `await None` tolerance, auto-records the topic into the contract
  and republishes the manifest.
- `mqtt_get(topic, timeout)` — one-shot retained-state read.
- `window(topic, seconds, max_size)` — sliding stream window with
  `mean/min/max/rising/falling/stable/absent_for/event_count/latest/count/values`.
  Idempotent per topic.
- `declare_contract(...)` — full signature with all LLM kwarg aliases
  (`schema`, `output_schema`, `topics`, `subscribe`, etc.), string-to-list
  coercion, awaitable sentinel return.
- `publish_world_state(key, data)` / `read_world_state(topic)`.
- `wiring_opportunities()` — returns `[]` on remote (cluster-wide view lives on
  main).
- `nodes()` / `topics()` / `capabilities()` — local-scope introspection.
- `logger` property exposing `info/warning/error/debug`.

Added at module level:

- `_AwaitableNone` / `_AWAITABLE_NONE` — sentinel mirroring `dynamic_agent`.
- `_RemoteStreamWindow` — self-contained port of `core.topic_bus.StreamWindow`
  so the remote runner stays single-file.

Tightened existing methods:

- `log()` now accepts `level="info"` to match the local signature.
- `_RemoteAgent.stop()` now also cancels stream windows so background MQTT
  listeners don't leak.

---

## Fix 5 — Local→Remote migration captures the live contract

**Symptom.** After a local→remote migration the agent's topic wiring was
incomplete on the remote side.

**Root cause.** The migration path was reading topics from the spawn registry,
which is what was *requested* at spawn time — not what the local agent had
wired up at runtime via `publish()`/`subscribe()`/`declare_contract()`.

**Fix.** Just before stopping the local agent, migration now snapshots the live
`TopicContract` from the `TopicBus` (`publishes`, `subscribes`, `triggers_when`,
`produces_schema`, `consumes_schema`, `observed_samples`) and folds non-empty
values into the spawn config that gets shipped to the remote node. The remote
`_publish_manifest()` then advertises the right surface immediately.

Also removed a stale double-save at the end of the local→remote branch —
`_spawn_remote(save=True)` already saves the complete `new_config` (with
initial state and live contract), but the post-branch code was overwriting that
with the stale `config` dict.

---

## Fix 6 — `migrate_agent` is resilient to missing registry entries

**Symptom.** `[FAIL] Agent 'X' not in spawn registry` even when the agent was
running and visible on the dashboard.

**Root cause.** `migrate_agent` hard-failed if the spawn registry lookup
missed. Several legitimate paths produce running agents that aren't in the
registry — ad-hoc spawns, partial migrations, registry overwrites from earlier
bugs.

**Fix.** `migrate_agent` now consults sources in priority order:

1. Spawn registry (has full config including code).
2. Manifest cache (has node, schemas; no code).
3. Live heartbeats (last resort — confirms which node hosts the agent).
4. Local registry (confirms the agent is running on this node).

Migration proceeds if any source finds the agent. The only "not found" case is
when no source knows about the agent at all.

---

## Fix 7 — Remote→Local migration without code on main

**Symptom.** Remote→local migration failed when main had no spawn-registry
entry for the agent.

**Root cause.** The remote runner had a `@main` sentinel path that shipped the
agent's state back over MQTT, but main had no listener for it — the feature was
half-implemented.

**Fix.** Added `_state_return_listener` on main. Migration flow:

1. Main sends `nodes/{node}/migrate` with `target_node: "@main"` and a
   one-time return token.
2. Remote runner stops the agent, captures its full live config (the in-memory
   `_config`, which includes everything the manifest exposed) plus its
   persistent state, and publishes both to `nodes/{node}/state_return`.
3. Main's `_state_return_listener` matches the token, builds a local spawn
   config from the returned data, attaches the state as `_initial_state`, and
   calls `_spawn_from_config(replace=True)`.

Tokens are one-time, expire after 5 minutes, scoped to the agent/node
combination so concurrent migrations can't collide.

---

## Fix 8 — `_handle_spawn` phantom method

**Symptom.** `[FAIL] Stopped on remote but failed to spawn locally: 'MainActor'
object has no attribute '_handle_spawn'`.

**Root cause.** `_handle_spawn` was referenced in three places in `main_actor.py`
but never defined. The real method is `_spawn_from_config(config, save=True)`,
which routes remote vs local, handles the `replace=True` flow, picks the right
agent type, and saves to the spawn registry.

**Fix.** Replaced all three call sites with `_spawn_from_config(...)`:

1. Remote→local migration (the path users hit).
2. State-return listener.
3. `/nodes remove → re-spawn locally` (pre-existing latent bug at line 2312;
   would have failed any time someone removed a node and had main re-spawn its
   agents locally).

Also updated stale comment references. Zero references to `_handle_spawn`
remain in the codebase.

---

## Fix 9 — Remote task dispatch matches local semantics

**Symptom.** Migrated LLM agent crashed on @mention with
`'str' object has no attribute 'get'` in `handle_task`.

**Root cause.** Main's @mention forwarder sends
`{"text": message, "payload": message, "_reply_topic": ...}`. The remote
runner was extracting just `data["payload"]` (a bare string) and passing it to
`handle_task`. Local actors pass the full envelope dict to `handle_task`, so
`payload.get("text")` worked locally and crashed remotely.

**Fix.** Remote runner now passes the full envelope to `handle_task`, stripped
of transport-level keys (`_reply_topic`, `_remote_task`). Matches local
semantics exactly.

Compat note: if a caller sends `{"payload": 42}` (scalar wrapped in a `payload`
field, nothing else), the runner unwraps the scalar — preserves the older
convention for callers that send `{"payload": 42}` and expect `42`.

---

## Fix 10 — `recall(key, default=None)` everywhere

**Symptom.** Migrating an agent back to local crashed with
`_AgentAPI.recall() takes 2 positional arguments but 3 were given`.

**Root cause.** Local `recall(key)` was strict; remote `recall(key, default=None)`
was permissive. Agents that learned to call `agent.recall("k", default)` on the
remote side crashed when they came back local.

**Fix.** Local now matches remote: `recall(key, default=None)`. The default is
returned when the key is missing or the stored value is `None`, matching
dict-`.get()` semantics.

---

## Fix 11 — `send_to` and `delegate` timeouts align at 60s

**Root cause.** Remote default 30s, local default 60s. Friendlier for LLM
agents, which routinely take 10–40s per turn.

**Fix.** Remote bumped to 60s. All shared method signatures between
`_AgentAPI` and `_RemoteAgentAPI` now match.

---

## Fix 12 — `agent.llm` namespace on the remote side

**Symptom.** LLM agents crashed remotely with
`'_RemoteAgentAPI' object has no attribute 'llm'`.

**Root cause.** Local agents use `agent.llm.chat(prompt, system)` /
`agent.llm.complete(messages, system)` / `agent.llm.converse(user_message, system)`.
Remote agents had a flat `agent.ask_llm(prompt, system)` / `agent.chat(messages, system)`
— different shape, different names. Worse, `chat` meant different things on the
two sides (single-turn locally, multi-turn remotely).

**Fix.** Added `_RemoteLLMInterface` exposed as `agent.llm` on the remote side
with the same three methods the local side has, with the same call shapes:

- `agent.llm.chat(prompt, system)` — single-turn.
- `agent.llm.complete(messages, system)` — multi-turn.
- `agent.llm.converse(user_message, system)` — stateful, maintains history in
  `agent.state['_chat_history']`.

The pre-existing flat `agent.ask_llm(...)` and `agent.chat(...)` stay as
legacy aliases.

**Known follow-up.** Local `agent.llm` increments the agent's token / cost
counters from the provider's usage response. The remote LLM bridge currently
returns only `{"text": ...}` — usage isn't propagated back, so remote LLM cost
is attributed to main rather than to the agent that spent it. Small follow-up
to ship `usage` in the reply.

---

## Fix 13 — LLM bridge attribute/method/return-shape

**Symptom.** LLM bridge error for remote agent:
`'MainActor' object has no attribute '_llm'`.

**Root cause.** Three bugs in two lines:

1. `self._llm` doesn't exist; the correct attribute is `self.llm` (every other
   site in `main_actor.py` uses that name).
2. Wrong method — `self.llm.chat(...)` doesn't exist on `LLMProvider`; the
   correct method is `complete(messages=, system=)`.
3. Wrong return shape — `LLMProvider.complete()` returns `(text, usage)`, not
   a string.

**Fix.** Bridge now calls `self.llm.complete(messages=..., system=...)`, unpacks
`(response, usage)`, guards against `self.llm is None` with a clear error
string, and rolls bridge usage into main's token/cost counters (same pattern as
other `complete()` call sites in `main_actor.py`).

---

## Fix 14 — Head-of-line blocking deadlock in the remote subscriber

**Symptom.** Remote agent timed out 60s after calling `agent.llm.chat()`, yet
main logged a `200 OK` from Anthropic within 4s. Diagnostic warning showed
`Reply arrived on nodes/rpi-n/reply/<uuid> but no agent had a matching pending
future. Waiting keys: []`, firing 4ms after the timeout.

**Root cause.** The runner's MQTT subscriber loop is structured as
`async for msg in client.messages` — a strictly sequential consumer. The
previous code did `await agent.handle_task(payload)` *inside* that loop. While
`handle_task` ran (waiting on `agent.llm.chat`'s reply future), the subscriber
could not dispatch any other message. The LLM reply sat queued behind the same
consumer. 60s later, `ask_llm` timed out, popped the future in `finally`,
returned `""`. The subscriber loop iterated, picked up the long-queued reply —
no waiting future.

**Fix.** Subscriber loop now wraps each `handle_task` invocation in
`asyncio.create_task(...)`. The subscriber returns to its iterator immediately
and remains free to dispatch incoming replies. Specifics:

- Task tracked on `agent._tasks` so a clean shutdown cancels it.
- `done_callback` removes it from the list when finished.
- Reply publish moved inside the background task.
- Exceptions caught, logged, and a `{"error": ..., "agent": ...}` dict goes
  back to the reply topic.

Same fix protects concurrent agents on the same node — two agents calling
`agent.llm.chat()` concurrently no longer block each other.

The diagnostic warning ("Reply arrived but no agent had a matching pending
future") stays as a permanent canary for similar deadlocks or topic mismatches
in the future.

---

## Fix 15 — LLM agents are runnable on remote nodes

**Symptom.** Migrated LLM agents forgot their conversation history. @mentions
returned nothing useful.

**Root cause.** A `type: "llm"` agent has no `code` — its behaviour lives
inside the `LLMAgent` class, which exists only on main. The remote runner is
DynamicAgent-shaped only; it has no `LLMAgent` equivalent. Spawn config with
`system_prompt` but no `code` → remote compiles empty code → no `handle_task` →
@mentions silently fail.

**Fix.** Added a code synthesiser on `MainActor`. When `_spawn_remote` sees a
`type: "llm"` config (or any spawn that has `system_prompt` but no `code`), it
injects an auto-generated `code` field with a `setup` + `handle_task` that:

- Restores `conversation_history` and `history_summary` via `agent.recall(...)`
  on setup.
- Maintains the rolling history in `agent.state['history']`.
- Calls `agent.llm.complete(messages=history, system=system_prompt)` — routes
  through the LLM bridge.
- Persists after every exchange so a runner crash mid-conversation costs at
  most one turn.
- Returns `{"text": reply}` so the @mention reply path works.

The synth tags the upgraded config with `_origin_type: "llm"`. When the agent
migrates back to local, the state-return listener sees that tag, strips the
synthesised `code`, restores `type: "llm"`, and the proper `LLMAgent` class
takes over.

---

## Fix 16 — `_initial_state` applied locally

**Symptom.** Even non-LLM agents lost persisted state on remote→local
migration.

**Root cause.** `_initial_state` was consumed only by the remote runner — the
local spawn path silently discarded it. Every remote→local migration started
with a blank slate.

**Fix.** Added `_apply_initial_state_locally`. After a local spawn following a
migration, it writes the migrated state dict through the actor's
`PersistenceAPI`, mirrors it into `_persistent_state`, and refreshes
LLMAgent-specific in-memory caches (`_conversation_history`, `_history_summary`,
token totals). The next `agent.recall(...)` returns the migrated value.

Not LLM-specific — every agent benefits. A sensor agent that persisted
thresholds and calibration counters now actually gets them back when migrated
to local.

---

## Fix 17 — Remote→Local always uses the `@main` sentinel

**Symptom.** Remote→local migration silently lost state accumulated since the
agent's original spawn (new conversation turns, observed schemas, calibration
counters).

**Root cause.** The `have_code` fast path used the spawn-registry config, which
is the config sent at spawn time — not what the agent had learned since.

**Fix.** Removed the `have_code` branch. Every remote→local migration now
follows the same path:

1. Main → `nodes/<node>/migrate {target_node: "@main"}`.
2. Remote stops agent, snapshots LIVE config + state.
3. Remote → `nodes/<node>/state_return`.
4. Main applies `_origin_type` restoration, spawns locally, applies state.

One round-trip instead of zero, live state every time.

---

## Test plan

- Spawn a `type: "llm"` agent locally, chat with it a few turns.
- Migrate to remote: `/migrate <agent> <node>`. @mention it — should remember
  earlier turns.
- Chat a few more turns on remote.
- Migrate back to local: `/migrate <agent> local`. @mention it — should
  remember all turns, both the local-original ones and the remote-added ones.
- Restart the remote runner (or reboot the Pi) while the agent is there. After
  the runner comes back, the agent should still remember.
- Spawn a DynamicAgent with `agent.subscribe(...)`, `agent.window(...)`, and
  `agent.declare_contract(...)`. Migrate in both directions — wiring should be
  preserved and the agent should keep functioning.
- Verify `/agents` and the planner's auto-wiring see the same topics for
  remote agents as for local ones.

### Added

- **LLM spend limit enforcement** - hard cap on LLM API spend per period (daily, weekly, or monthly). Set via `LLM_COST_LIMIT_USD` / `LLM_COST_LIMIT_PERIOD` env vars or at runtime from the dashboard Settings tab without restart. When the limit is reached, further LLM calls are blocked and a "limit reached" message is delivered as a chat reply. Spend accumulates into all three period keys simultaneously so switching periods always shows real data. New REST endpoints: `GET /api/cost`, `POST /api/cost/limit`, `POST /api/cost/reset`. Env-var values are the startup default; GUI override persists in SQLite and takes priority.

### Fixed

- **`monitor_server` stdio wrapping under pytest** - `monitor_server` no longer re-wraps `sys.stdout` / `sys.stderr` at import time when they have already been replaced by a test capture harness. Prevents `ValueError: I/O operation on closed file` during pytest teardown on Python 3.13 + Windows.

---

## [0.4.2] - 2026-05-14

### Added

- **Dynamic LLM pricing** - `LLMAgent` now fetches live model prices from the [LiteLLM model catalogue](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) on startup and caches them for 24 hours. Falls back to a hardcoded table if the fetch fails or the model is not found. `pricing_info(model)` helper added for debugging (reports source, rates, and cache age).
- **`HomeAssistantAgent` tool call loop** - Agent now runs a structured LLM tool-call loop for actions that require live HA data, replacing single-shot prompts and improving reliability for multi-step queries.
- **`HomeAssistantAgent` `other` action** - A new `other` action handles open-ended HA questions ("Do I have any thermometers?", "What is the state of my thermostat?") that do not map cleanly to `list_*` or `call_service`. The agent runs a short LLM tool-call loop (up to 3 rounds) using `get_simplified_ha_data` to answer the question without over-classifying inventory requests or listing every entity. A `ha_context_terms` heuristic ensures common HA-related questions are routed here instead of falling through to `unknown`.
- **`HomeAssistantAgent` `get_entities_state` action** - Accepts one or more explicit entity IDs, fetches their current states from HA, and re-publishes each state to `homeassistant/state_changes/{entity_id}` over MQTT. This lets callers query live state and simultaneously bootstrap any MQTT subscriber that is waiting for a change event.
- **`ha_helper.get_full_ha_data()`** - New async helper that returns raw registry dumps for floors, areas, devices, entities, and states in a single WebSocket session, without transforming or filtering any field.
- **`ha_helper.get_simplified_ha_data()`** - New async helper that returns a compact, null-stripped snapshot suitable for LLM prompts. Resolves entity display names from live states, excludes `hassio` platform entities, and drops icon/picture fields. Used by `HomeAssistantAgent` to replace the older `fetch_devices_entities_with_location` call, significantly reducing token usage in device-discovery prompts.
- **`PlannerAgent` HA entity state bootstrap** - After spawning a pipeline, the planner now calls `_bootstrap_ha_entity_states()` as a background task. It extracts entity IDs from the plan's generated code, `ha_actuator` actions, MQTT topics, and the enriched task string, then sends a `get_entities_state` request to `home-assistant-agent`. This re-publishes current HA state to `homeassistant/state_changes/{entity_id}` so freshly-spawned agents that subscribe to that topic fire immediately, instead of waiting for the next real HA state change to arrive.
- **Remote runner self-bootstrap** - `RemoteRunnerAgent` nodes now self-install `aiomqtt` / `paho-mqtt` on first start without requiring pre-installed dependencies. Heartbeat begins immediately; dependency installation runs in the background so the node appears on the overview before pip finishes.
- **Live remote node tracking** - the overview panel tracks remote runner nodes in real time; deleted-agent ghost entries no longer re-appear after removal.
- **OpenTelemetry Collector** - `otelcol` service added to Docker Compose with a Prometheus remote-write scrape target; healthcheck included and a commented debug exporter option for local tracing.
- **`watch-costs.ps1`** - PowerShell script for live LLM cost monitoring from the terminal.

### Changed

- **`HomeAssistantAgent` device-discovery token reduction** - Prompt schema for hardware-recommendation requests now uses the flattened `get_simplified_ha_data` structure (separate `floors`, `areas`, `devices`, `entities` lists) instead of the deeply nested `fetch_devices_entities_with_location` format. This cuts the context size and matches the real HA registry field names (`id`, `area_id`, `domain`, etc.).
- **`HomeAssistantAgent` `list_*` classification tightened** - The `list_automations`, `list_areas`, `list_devices`, and `list_entities` actions now only fire on explicit inventory requests ("list all automations"). Existence, count, lookup, and state questions ("do I have a thermostat?", "what is the state of X?") are correctly routed to the new `other` action.
- **`HomeAssistantAgent` MQTT state-change payload** - `get_entities_state` now publishes the canonical state-change payload (`event_type`, `entity_id`, `new_state`, `old_state`) to `homeassistant/state_changes/{entity_id}`, matching the format emitted by `HomeAssistantStateBridgeAgent` on real HA state changes.
- **Slash commands** - all slash commands now route through a single source of truth in `MainActor`, eliminating inconsistencies across entry points.

### Fixed

- **LLM cost persistence** - Five places where token usage was accumulated in memory but never written to SQLite, causing cost data to be lost on restart or crash: `LLMAgent._handle_task` silently discarded all usage from TASK-type messages; `LLMAgent._maybe_summarize` did not persist summarization tokens; `HomeAssistantAgent` never persisted lifetime spend (entirely lost on restart); `MainActor._classify_intent` dropped tokens for PIPELINE/ACTUATE/HA routes where no `chat()` follows; `MainActor._extract_durable_facts` left facts-extraction tokens unpersisted until the next turn.
- **Cost tracking in `PlannerAgent` and `MainActor`** - planner and main actor now persist spend after every LLM call.
- **Gemini API key mapping** - `LLM_API_KEY` now correctly mapped to `GEMINI_API_KEY` in the HA addon `run.sh`.
- **NIM documentation** - `LLM_API_KEY` is always required for NVIDIA NIM calls; docs corrected.
- **HA addon optional fields** - `discord_bot_token`, `telegram_bot_token`, `ha_token`, and `api_key` declared as `str?` in `config.yaml` schema so the addon validates when these fields are left blank.
- **Agent delete blink** - deleted agents are marked immediately on delete command, preventing ghost re-appearance in the UI.
- **NIM fallback pricing** - deprecated NVIDIA NIM model entries removed from the hardcoded fallback price table.
- **OTel Collector healthcheck** - `otelcol` healthcheck corrected; debug exporter added as commented option.
- **Remote runner async bootstrap** - heartbeat now starts before pip completes so the node appears in the overview immediately.
- **UI non-streaming agent communication** - messages from non-streaming agents now display correctly in the chat interface.
- **Catalog agent spawning** - timeout issue resolved; agents spawn reliably under load.
- **Fuseki Python 3.10 compatibility** - `fuseki.py` now runs on Python 3.10.

### Tests

- Added `tests/test_home_assistant_agent.py` - covers `other` tool-call loop, `get_entities_state` action, MQTT publish payloads, and bootstrap entity ID extraction.
- Added `tests/test_llm_provider_tools.py` - covers `complete_with_tools` for all LLM providers.

---

## [0.4.1] -- 2026-05-06

### Added

- **Flutter companion app** -- iOS/Android mobile app with agents list, chat interface, and activity feed. Connects to the Wactorz REST + WebSocket API.
- **PWA / service worker** -- installable progressive web app with `sw.js`; `icon.png` added; bottom tab bar for mobile browsers.
- **Persistent chat log** -- conversation history persisted to SQLite on every message; optionally mirrored to InfluxDB 2.x. Chat panel restores full history on page load.
- **InfluxDB 2.x integration** -- optional `influx_url`, `influx_token`, `influx_org`, `influx_bucket` config added to the HA addon and `.env.template`; `wactorz[influx]` bundled in the `[all]` extras group.
- **Server-side TTS via `edge-tts`** -- text-to-speech synthesised server-side with browser speech-synthesis fallback. Voice selector populated from server or browser voices; audio delivered via `AudioContext`.
- **Procedural ambient soundscapes** -- rain / forest / beach / cafe audio modes; `🔊` button popover replaces inline header controls.
- **Scheduled agents** -- new `ScheduledAgent` for cron-style recurring tasks. Planner and `MainActor` prompts updated to support scheduling intents.
- **User approval before spawning** -- planner generates a dry-run plan and requests explicit user confirmation before spawning agents. `approved` flag added to the plan payload.
- **Activity feed: HA state changes** -- real-time Home Assistant device state changes now appear in the activity feed, routed through `HomeAssistantStateBridgeAgent`, with domain-based filtering and WebSocket + MQTT deduplication.
- **REST: `/api/actors/{id}/history`** -- actor message history endpoint.
- **REST: `/api/chats`** -- chat log endpoint (all persisted messages, paginated).
- **Rust: `/api/feed`, `/feed`, `/config`** -- feed and config alias endpoints added to the Rust server; `MonitorState` wired to REST for consistent snapshots.
- **Desktop SQLite persistence** -- Rust backend now persists actor and message state to SQLite with auto-resume on restart.
- **OpenTelemetry metrics** -- OTel metrics integration; Fuseki triplestore bloat fixed alongside.
- **Docker Hub + GHCR CI image workflow** -- automated multi-registry image publishing on tag push.
- **Frontend test suite** -- comprehensive test suite with 95%+ coverage.
- **SPARQL planner integration** -- planner agent can query the Fuseki triplestore for context via `sparql_context.py` helper.
- **Staging Docker Compose** -- `compose.staging.yaml` for VM staging deployments.
- **Activity feed hover popover** -- styled hover popover for feed messages replaces native `title` tooltip.
- **Overview: message count persistence** -- message and cost stats survive restarts; seeded from SQLite on startup; backend totals shown before the first MQTT heartbeat.
- **Help menu** -- `/migrate` and `/nodes` commands now listed in `/help` output.

### Changed

- **Erlang-style supervision overhaul** -- full rewrite of the `Supervisor` with per-actor restart policies, configurable backoff, and max-restart caps across ONE_FOR_ONE / ONE_FOR_ALL / REST_FOR_ONE strategies.
- **Sound / TTS / voice controls** -- moved from the HUD (unreachable on small screens) to the header bar.

### Fixed

- **LLM API key provider mapping** -- `LLM_API_KEY` now mapped to the correct provider-specific env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `NIM_API_KEY`) in the HA addon `run.sh` and CLI.
- **NIM fallback** -- CLI and `MainActor` now fall back to `LLM_API_KEY` when the provider-specific variable is unset.
- **Compose port mapping** -- `MONITOR_PORT` used consistently on both sides of the port mapping.
- **Cost persistence** -- LLM cost written durably to SQLite; deleted agents included in the lifetime total; partial streaming responses persisted on interruption; user message persisted before the LLM call to avoid data loss on crash.
- **Activity feed** -- real persisted timestamps used for `/api/feed`; `log_feed` flood on WebSocket connect fixed; feed truncation removed from `IOManager`; spawn/alert timestamp normalisation prevents "Invalid Date" in the UI.
- **Deleted agents re-appearing** after delete action due to stale registry state.
- **Camera agents restart** and double-notification issues resolved.
- **HA amnesia** -- agent no longer forgets Home Assistant context across restarts.
- **Catalog timeout and null safety** -- catalog agent spawning timeout fixed; null guards added throughout `CardDashboard`.
- **Persistence layer** -- SQLite data no longer overwritten by stale pickle on restart.
- **`actor.stop()` cancellation** -- shielded from `asyncio.CancelledError` so shutdown completes cleanly.
- **MQTT paho `__del__` `RuntimeError`** -- suppressed on event loop close.
- **TTS voice cache** -- warmed at startup to avoid an executor-shutdown race.
- **Frontend** -- undefined `agentName` / `message` guard in alert handler; feed label hover; various null-safety fixes.
- **Chat history timestamps** -- chat panel now uses real persisted timestamps from the `chat_log` SQLite table.

### Tests

- Added `tests/test_cost_persistence.py` -- cost persistence and chat history API coverage.

---

## [0.4.0] -- 2026-04-25

### Added

- **Home Assistant addon** -- full HA Supervisor addon (`ha-addon/`) supporting HAOS and Supervised installs. Configurable LLM provider, MQTT, HA token, Fuseki, Discord/Telegram integrations. Optional embedded Mosquitto (`mosquitto_embedded`) and Fuseki (`fuseki_embedded`) services bundle broker and triplestore inside the addon container -- no external addons required. Data persisted to `/share/mosquitto` and `/share/fuseki`. Ingress-compatible with relative asset paths and `X-Ingress-Path` header support.
- **MCP server** -- `wactorz/mcp_server.py` exposes the actor system as an MCP (Model Context Protocol) server. Tools: `send_message`, `list_actors`, `get_actor_status`, `spawn_agent`, `stop_agent`. Resources: `wactorz://actors`, `wactorz://topics`. Configurable via `WACTORZ_MCP_*` env vars. Documented in `docs/interfaces.md`.
- **Unified persistence layer** -- `wactorz/core/persistence.py` introduces a 3-tier architecture replacing pickle-only storage: SQLite (`state/wactorz.db`) for durable structured data (spawn registry, pipeline rules, user facts, topic contracts, time-series), Redis for ephemeral fast-access data (falls back to in-memory), and Pickle for arbitrary Python objects (agent state dicts, ML models). `PersistenceAPI` provides backward-compatible `persist()`/`recall()` with automatic key-based routing. `migrate_from_pickle()` runs once on first startup to migrate existing state.
- **Time-series SQLite tables** -- `sensor_readings`, `detections`, `ha_state_changes`, and `actuations` tables with full-text and time-range query helpers (`query_sensor`, `query_detections`, `query_ha_states`, `query_actuations`). Automatic retention pruning via `prune_old_data(days=30)`.
- **Fuseki Channel ontology and MetricsBridge** -- `infra/fuseki/ontology/wactorz.ttl` extended with `af:Channel` class (`channelTopic`, `declaredSchema`, `observedSchema`, `triggersWhen`) and agent metrics properties. `FusekiClient.replace_agent_channels()` persists pub/sub topology to `urn:wactorz:channels`. `MetricsBridge` subscribes to `agents/+/metrics` MQTT and continuously updates agent metrics in the RDF graph via `upsert_agent_metrics()`.
- **Activity feed cap** -- UI activity feed is capped at 500 entries; an overflow banner appears when the limit is reached.
- **Cost metrics persistence and final publish** -- LLM cost and token metrics are persisted across restarts and published in the final heartbeat on actor stop.

### Changed

- **One-shot Home Assistant actuation timeouts** -- intent classification now allows up to 60 seconds, while the ephemeral `OneOffActuatorAgent` resolver and main actuation wait allow up to 120 seconds for slower local models such as Ollama.
- **Versioning** -- `wactorz/_version.py` remains the single source of truth; version handling unified across CLI, pyproject.toml, and the HA addon.

### Fixed

- **Ollama system prompts** -- `OllamaProvider` now sends `system_prompt` as the first `role=system` chat message for both blocking and streaming `/api/chat` calls, instead of relying on an undocumented top-level `system` payload field.
- **HA addon ingress** -- corrected `X-Ingress-Path` header name; relative paths used for favicon and manifest so the base tag resolves correctly behind the HA proxy; SPARQL proxy URLs now prepend the ingress path.
- **HA addon embedded Fuseki startup** -- `shiro.ini` is regenerated on every boot so credential changes apply immediately; correct dataset config and readiness wait added.
- **HA addon Docker layer cache** -- `BUILD_VERSION` arg now busts the Docker cache on version bumps; deprecated `build.yaml` removed; base image fixed to `ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20`.
- **Catalog agent persistence** -- fixed catalog agent spawning and persistence after the persistence layer migration.
- **HA map agent `CancelledError`** -- handled `asyncio.CancelledError` in `HomeAssistantMapAgent` to prevent noisy tracebacks on shutdown.
- **Resource cleanup on stop** -- `Actor.on_stop()` now cancels background tasks and cleans up open resources more reliably.
- **Frontend URL resolution** -- unified backend URL resolution across Tauri desktop, HA addon, and plain browser: checks `window.__WACTORZ_API_PORT`, then `window.__WACTORZ_API_BASE`, then falls back to `window.location`.
- **CI: Linux system deps** -- added missing Linux system dependencies to the Rust test job.

### Tests

- Added focused `OllamaProvider` payload tests covering non-streaming and streaming system-prompt delivery.
- Added MCP server contract tests (`tests/test_mcp_server.py`); contract tests skip gracefully when optional MCP dependency is absent.

---

## [0.3.0] -- 2026-04-18

### Added

- **Telegram interface** -- new `--interface telegram` mode using `python-telegram-bot`; users self-host their own bot via a BotFather token. Supports `TELEGRAM_ALLOWED_USER_ID` to restrict access to a single user. The `/start` command replies with the user's numeric Telegram ID for easy setup.
- **`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_ID`** env vars added to `config.py` and `.env.example`
- **One-shot Home Assistant actuation** -- `MainActor` now classifies immediate device-control requests as `ACTUATE` and routes them to a new ephemeral `OneOffActuatorAgent` that resolves natural language to HA service calls, executes them, reports the result, tracks LLM cost, then unregisters, stops, and deletes its own persistence directory.
- **Prometheus monitoring for the Python runtime** -- the REST interface now exposes `GET /metrics` with Prometheus-formatted HTTP, actor, process, and LLM usage metrics via a shared `PrometheusMonitor` collector in `wactorz/monitoring/prometheus.py`.
- **Prometheus Compose services** -- `prometheus` and `blackbox-exporter` services added to `compose.yaml` and `compose.dev.yaml` for Python-stack monitoring. Optional Mosquitto and Fuseki availability probes are controlled via `PROMETHEUS_MONITOR_MOSQUITTO` and `PROMETHEUS_MONITOR_FUSEKI`.
- **Prometheus configuration assets** -- added templated config generation in `infra/prometheus/` including `prometheus.yml`, `render-config.sh`, `blackbox.yml`, and starter alert rules in `alerts.yml`.
- **Prometheus docs and tests** -- added `docs/prometheus.md`, linked it in the docs navigation, documented `GET /api/metrics`, and added focused tests for collector output, config generation, and REST content type handling.

### Changed

- **Discord interface** -- bot now responds to `@mention` instead of the `!` prefix for a more natural UX. Long responses are automatically split into 2000-character chunks to avoid Discord's message length limit.
- **Documentation** -- added README and agent reference coverage for `ACTUATE` intent routing and the new `OneOffActuatorAgent`.

---

## [0.2.0] -- 2026-03-13

### Added

- **IOAgent** -- MQTT gateway routing `io/chat` messages to the correct actor; replaces direct topic publishing
- **MQTT TCP bridge** in `monitor_server.py` -- `/mqtt` WebSocket endpoint now falls back to raw TCP (port 1883) when Mosquitto's WS listener (port 9001) is unavailable
- **Web UI auto-start** -- `wactorz` CLI spawns the monitor server as a quiet background asyncio task (`--no-monitor` to opt out, `--monitor-port` to override port 8888)
- **`/api/actors` REST endpoint** on Python monitor server -- returns live agent state from MQTT-derived in-memory store
- **`wactorz[all]` wheel** now bundles `static/app/` via hatchling `force-include`; custom build hook rebuilds frontend when stale
- **`wactorz/_version.py`** -- single source of version truth, imported by `__init__.py` and `pyproject.toml`
- **Rust WS bridge** -- `/mqtt` proxy route added alongside `/ws`; `WsBridge` now tracks MonitorState and broadcasts `full_snapshot`/`patch`/`delete_agent` to `/ws` clients
- **`scripts/build.py`** -- clean build script (hatchling + twine) with `--upload` flag for PyPI

### Fixed

- **`RangeError: invalid date`** -- Python heartbeat uses epoch seconds (`timestamp`); TypeScript normaliser now converts to ms automatically for both Python (snake_case) and Rust (camelCase) payloads
- **MQTT disconnect on listener error** -- `emit()` now wraps each listener call in try/catch; a throwing handler no longer crashes the MQTT connection
- **Chat infinite typing indicator** -- fixed key mismatch between `showTyping("main-actor")` and `hideTyping("io-agent")`; `IOManager` tracks `_lastTypingKey` and clears it on any reply
- **`llm_agent._handle_task`** -- `complete()` returns `(text, usage)` tuple; was incorrectly storing the whole tuple as message `content`, causing Anthropic 400 errors on the second conversation turn
- **CI test failures** -- `wactorz/` package was accidentally gitignored; restored source tracking and fixed test import paths for the new package layout
- **`/api/actors` 404** -- Python monitor server now serves actor list at this endpoint

### Changed

- `wactorz/__init__.py` -- optional agent imports (LLM, HA, ML) now wrapped in `try/except ImportError` so importing any submodule works without all optional deps installed
- Python payload normalisers centralised in `MQTTClient.ts` -- `normaliseHeartbeat`, `normaliseChat`, `normaliseStatus`
- Monitor server `_find_dir()` helper resolves `static/app` for both editable and installed-wheel layouts

---

## [0.1.0] -- 2025-11-01

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

[Unreleased]: https://github.com/waldiez/wactorz/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/waldiez/wactorz/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/waldiez/wactorz/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/waldiez/wactorz/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/waldiez/wactorz/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/waldiez/wactorz/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/waldiez/wactorz/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/waldiez/wactorz/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/waldiez/wactorz/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/waldiez/wactorz/releases/tag/v0.1.0
