# Changelog

All notable changes to Wactorz are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased] — pending

### Changed — breaking

- **The dashboard and API now listen on `127.0.0.1` by default.** They listened on every interface, so a fresh install served the dashboard, the whole API and the chat log to everything on the network without being asked to. **If you reach Wactorz from another machine**, set `WACTORZ_BIND_HOST=0.0.0.0` and read the next entry — you will also need to say why that is safe. The supplied Compose file and both Home Assistant add-ons already set it, so those are unaffected.
- **Binding to a reachable address with no `API_KEY` now refuses to start.** Previously it started and served everything to anyone who could reach the port; a warning was considered and rejected, because a warning scrolls past in a container log while the operator believes they merely changed an address. Three ways forward, and the refusal names all three: set `API_KEY`, bind to `127.0.0.1`, or set `WACTORZ_EXPOSED_OK=1` when the only route in is already authenticated — behind Home Assistant ingress, or a reverse proxy you control. A process cannot see its own port mappings, which is why a container has to declare this rather than have it inferred. **Running the plain image** — `docker run -p 8888:8888 …` — now needs one of those too: the image binds wide because a published port cannot reach a container's own loopback, but it cannot know whether you published to a loopback mapping or to the world, so it refuses until you say. **When Wactorz is started as a whole**, the refusal stops the process; when only the dashboard is embedded in a larger app, it logs and returns, so agents keep running and the dashboard simply never appears — one log line is the only sign, and it names the fix.
- **`API_KEY`, once set, is required on every monitor route except `/health`.** It was read by nothing there before. `X-API-Key` or `Authorization: Bearer` both work; `/health` stays open so container and uptime probes keep working. **Setting a key currently means no browser dashboard** — the page holds no credential to send, so it would load and then fail every request it made. A guarded install answers 401 at the door instead. Set a key for API-only installs; leave it unset, with the loopback default, for dashboard use. Sign-in is the next piece of work.

### Added

- **The application log is readable from the dashboard.** Startup lines, errors and tracebacks — the lines that explain a failure — used to exist only on the machine, reachable by SSH and `tail`. The activity feed now shows them alongside agent events, with a source filter (agents / application / both), a level filter and a search box, and a log row expands in place for the full text. Records come from a bounded in-memory buffer (~1000 entries, newest kept) over a new `GET /api/logs` endpoint, redacted as they are written — the filter scrubs known credential shapes before anything is stored. That redaction is a floor, not a guarantee, so treat the view as shareable-with-care: a log can carry what nobody chose to write into it.
- **Files can be attached to a chat message, and the agent reads them.** The composer has accepted attachments for a while, but nothing was ever sent with the message. Images now reach every supported provider, PDFs are read inline by Anthropic and Gemini (elsewhere the file is named and its contents omitted, as those formats have no document part), text files of any kind are inlined under their name, and audio is named but not transcribed. What a file is gets decided by its content, not by what the upload claimed, and a file that cannot be carried is named rather than silently dropped. Storage is on by default and turns off with `WACTORZ_UPLOADS=0`; uploads are capped at 25 MB, and clearing the chat history (or wiping everything) deletes the stored files with it.
- **`WACTORZ_BIND_HOST` chooses which network interface the servers listen on.** The dashboard, the REST API and the WhatsApp webhook all read it, so one setting moves all three rather than three hardcoded addresses. See the breaking entry above for the new default and what it means for an install reached from another machine. Not useful inside a container: a published port cannot reach a process bound to the container's own loopback, which is why the supplied Compose file and both add-ons set it explicitly. The REST API also now logs the address it actually bound rather than a fixed one.

### Changed

- **The dashboard is sent only the broker messages it uses.** The server subscribes to whole topic subtrees because agents publish freely within them, and forwarded every message inside them to every open browser — so whatever any agent happened to publish crossed the wire, including topics nothing on the page reads. Only the topics the dashboard acts on are forwarded now. Nothing on screen changes, and this does not affect what the server itself takes in: an agent's metrics update the dashboard exactly as before. **If you have written a custom agent whose own topics you were reading from the browser**, they are no longer relayed.
- **A web page on another site can no longer act on your Wactorz.** Every response carried `Access-Control-Allow-Origin: *` and every preflight was approved, so any page you visited while Wactorz was running could delete your agents, reset your system, or read your agent names, chat log and spend. Requests from another origin are now refused when they would change something, and responses to them are no longer readable by the calling page. The WebSocket is checked the same way — it is exempt from CORS entirely, so without this a page could still open it, watch the live feed and send commands. Requests carrying no `Origin` at all are unaffected, so `curl`, scripts and the Python client keep working. A dashboard hosted on another origin needs `WACTORZ_CORS_ORIGINS`.
- **A host name nobody configured is refused, closing DNS rebinding.** Checking the origin alone cannot see that attack: the attacker's page and the address it resolves to share a name, so they agree while the request reaches a server on your own machine. Loopback names and IP addresses are always accepted, so `localhost` and `192.168.1.5` are unaffected. **If you reach the dashboard by an mDNS or LAN name** such as `wactorz.local`, add it to `WACTORZ_ALLOWED_HOSTS`; the refusal is logged with the name to add. The Home Assistant add-on is unaffected — requests arriving through its panel are recognised as such.

### Fixed

- **A spawn config can no longer exempt its own code from the safety checks.** Agent configs carry a `trusted` flag that skips both the code sanitizer and the validator; it exists for the packaged catalog agents, whose code is written by us and does not have to pass a check aimed at generated code. But the flag was honoured on any config, and spawn configs are routinely written by a model — so a request that asked for an agent could ask for an unexamined one, and the flag was then saved and restored on every start afterwards. It is now accepted only for agents being restored from the spawn registry; anywhere else it is dropped, logged, and the code is validated as usual. Catalog agents are unaffected.

- **A state file that cannot be read is kept instead of overwritten.** Every load path treated an unreadable file as absent — log it, start empty, carry on — which is the right call for keeping an agent running, but the agent's next save then wrote straight over the file. The only copy of whatever it remembered was destroyed moments after the single log line about it scrolled past, so an agent that had quietly lost everything looked perfectly healthy. Such a file is now moved aside first, to `<name>.corrupt.<timestamp>` beside the original, and the failure is reported with the path it was kept at. The remote node runner did this worst of all: it discarded the error silently, leaving no record anywhere that anything had been lost.
- **A framework migration that failed is retried on the next start.** The stored version stood for two separate things — that the database schema was at version N, and that agent state data had been migrated to N — so when a schema migration succeeded and its paired data migration failed, the version was stamped anyway. The following start saw an up-to-date version, skipped everything, and the half-migrated data stayed that way permanently, with the one warning about it never appearing again. Data migrations are now tracked by what has actually completed, so a failed one is attempted on every start until it succeeds, and it no longer holds back the schema version, which is genuinely up to date. The schema version now also records how far the schema really got rather than assuming a run that stopped part-way finished.

## [0.5.3] - 2026-08-10

### Added

- **Images in an agent's reply are shown as images.** An agent answering with a camera snapshot or a generated chart sends it inline, and the chat rendered that as a wall of base64 text. Such images now appear in the message, bounded so a full-resolution frame cannot stretch the conversation, and clicking one opens it full size. Only real image formats are accepted — PNG, JPEG, GIF, WebP and AVIF, whether inline or fetched over http(s) — and anything else is left as the text it was rather than silently dropped. SVG is excluded on purpose, because it can carry scripts.
- **A stopped agent can be started again.** Stopping one left deleting it as the only remaining action, so stopping was effectively permanent. Agent cards now offer **Start** for a stopped agent, `/start <agent>` does the same from chat, and the agent goes back under supervision — without that it would run unwatched, crashing and staying down. This is not the same as `/agents restart`, which re-creates an agent from its saved spawn configuration; Start resumes the one that is already there, including built-in agents that were never spawned from chat.

### Changed

- **The device-control prompt carried ~2.4x more data than the model could use.** Every actuation sent the full dashboard payload to the LLM — `unique_id`, `platform`, `swid`, icons, entity pictures and feature bitmasks — none of which any prompt in the codebase mentions. On a real home that is ~24k tokens per request, of which `unique_id` alone was the single largest field. The resolver now receives only what it reasons over (entity id, names, area, state, and the colour attributes), roughly halving the prompt: 24.3k -> 10.3k tokens on one setup, 26.3k -> 12.1k on another. Post-processing still sees the full payload, so colour repair is unchanged. This matters most for small local models, where the noise both competes for attention and can overflow a short context window.

- **The LLM spend limit now applies to every call.** `LLM_COST_LIMIT_USD` was checked only on the main chat paths, so the roughly 46 places that call a provider directly — the planner, dynamic agents, the actuator, fact extraction — kept spending after the cap was reached. The check now lives on the provider itself and covers completions, tool calls and streaming alike, so a path added later inherits it. **If you set a limit:** it will now stop work it previously let through.
- **The REST API key guards every route except `/health`.** Only `POST /chat` checked it, so deleting an actor, sending it a message, and pausing or resuming it were open even on an install that had set a key. The check moved into middleware, so a route added later is covered without anyone remembering to guard it, and the comparison is now constant-time. `Authorization: Bearer <key>` is accepted alongside `X-API-Key`. `/health` stays open so container and uptime probes keep working. **Installs with no API key set are unaffected — every route stays open, exactly as before.** **If you set one:** scripts calling anything other than `/chat` must now send it, and a Prometheus scrape of `/metrics` needs an `authorization` block in its scrape config.
- **The WhatsApp webhook verifies Twilio's signature.** It checked the sender number against the allow-list, but a forged request can name any sender it likes — so the allow-list was authorising callers it had never authenticated, and anyone who found the URL could spend LLM budget and make the bot send messages. Requests are now validated against your Twilio auth token, and one that is unsigned or wrongly signed is refused. `X-Forwarded-Proto` and `X-Forwarded-Host` are honoured, so a reverse-proxied deployment validates against its public address rather than the internal one. **If WhatsApp runs behind an unusual proxy chain:** validation depends on the request URL matching the address Twilio signed.
- **`/actors/{id}/metrics` reports real numbers.** `messages_received`, `heartbeats` and the three LLM token and cost fields were hardcoded to zero. Actors that do no LLM work still report zero spend, because they have none.
- **Remote nodes are deployed from configured targets, and SSH credentials no longer travel through chat.** `/deploy` accepted a host, user and password as chat arguments — an unauthenticated request could hand the server a live credential and have it connect anywhere, and because chat is written to the conversation history and the chat log, the password stayed on disk long after the deploy finished. The machines Wactorz may connect to are now listed in the environment, each with its own credentials: `DEPLOY_TARGETS=rpi-kitchen` plus a `DEPLOY_RPI_KITCHEN_*` block for the host, user, key or password and broker (see `.env.template`), or a `deploy_targets` list in the Home Assistant add-on's options. `/deploy` takes a node name and nothing else; run it bare to list the configured targets. Leaving a target's host unset resolves `<name>.local` over mDNS. The installer agent resolves credentials for itself and ignores any a task payload carries, so a request phrased as ordinary conversation cannot supply them either, and it no longer stores an SSH password in its own state — passwords written there by earlier versions are deleted on first start. **If you deploy remote nodes:** add a target block before upgrading, or `/deploy` will have nothing to offer. `/deploy-pkg` likewise takes a node name and no longer prompts for a password.
- **Remote nodes are configurable from the Home Assistant add-on, with their broker requirement documented.** A `deploy_targets` list in the add-on options maps to the same environment variables the standalone app reads, defaulting to empty so nothing changes for an existing install. What it cannot yet do is now written down rather than discovered: a remote node runs on its own machine and dials the broker over the network, so it needs one it can both reach and connect to anonymously. The add-on's embedded broker is not published to the network and cannot serve a node at all, and broker credentials are not delivered to a node yet, so the Home Assistant Mosquitto add-on cannot serve one either — an external broker accepting anonymous connections is the supported arrangement for now.
- **A node name that cannot be an MQTT topic is refused instead of deployed.** The name becomes one level of every topic the runner uses — `nodes/<name>/heartbeat` to publish, `nodes/<name>/spawn` to subscribe — so a `#` or `+` in it left a node that connected to the broker, was refused on every publish and every subscribe, and reconnected every three seconds forever while the deploy that started it reported success and the node never appeared in `/nodes`. A `/` was quieter still: it added a topic level, so the runner worked but published where nothing was listening. Deploy now refuses such a name before it touches the network, and the runner exits with an explanation rather than retrying if it is started with one anyway. Worth knowing when writing `DEPLOY_TARGETS`: in a `.env` file `#` only begins a comment when a space precedes it, so `DEPLOY_TARGETS=rpi-kitchen#,rpi-garage` names a node `rpi-kitchen#`.
- **SSH host keys are verified when Wactorz connects to a remote node.** Every connection passed `known_hosts=None`, which accepts whatever key answers — so anything that could win the race for a node's address on the local network collected the SSH credentials sent to it. A machine that has not been connected to before now has its key recorded on first contact, the same trust-on-first-use that interactive `ssh` performs, and a later change to that key fails the connection instead of being accepted silently. Learned keys live in `<WACTORZ_STATE_DIR>/known_hosts` unless `DEPLOY_KNOWN_HOSTS` points elsewhere. Set `DEPLOY_STRICT_HOST_KEYS=1` to turn off first-use learning entirely, after which an unknown host is refused until its key is added by hand.
- **Credentials are redacted from the log file and the console, and `wactorz.log` now rotates.** Broker URLs like `mqtt://user:password@host`, `Authorization` headers, `password=`/`api_key=` pairs and private keys were written verbatim to a file that grew without bound. Known patterns are replaced with `[redacted]`; the log rolls at 10 MB keeping 5 backups, and `wactorz-reset` deletes those backups. Known shapes only — not a guarantee that no secret ever reaches disk. Logging is now configured at startup rather than on import, so importing Wactorz as a library no longer overrides your own logging setup; call `wactorz.monitoring.log_setup.setup_logging()` for the old behaviour.
- **The dashboard address is printed when startup finishes, rather than when the web server binds.** It used to appear early and scroll out of view under the agents starting up.
- **The Home Assistant add-on no longer publishes its ports to your network.** The dashboard and REST API were mapped to host ports `8888`/`8000`, so anything on the network could reach them directly, bypassing the Home Assistant login that protects the add-on panel. Nothing is published by default now — open Wactorz from its panel, or assign a host port under the add-on's Network section if you deliberately want access from outside Home Assistant.
- **The add-on asks for less authority over Home Assistant.** It requested the Supervisor `admin` role — full Supervisor API access, including every other add-on's configuration and secrets — and used none of it. It now runs with the default role; the Home Assistant core API it actually calls is unaffected.
- **The add-on's embedded MQTT broker requires credentials and no longer runs as root.** It accepted anonymous connections from anything that could reach it on the Home Assistant container network, and stayed root in order to write its own persistence directory. It now generates a credential on first start, keeps it under `/data` so it survives restarts and updates, authenticates every connection, and runs as the unprivileged `mosquitto` user.
- **The Docker image runs as an unprivileged user.** Previously everything ran as root. The container now drops to a dedicated user after preparing the state directory, gives up every Linux capability except the three that preparation needs, mounts its root filesystem read-only, and forbids acquiring new privileges. Packages that agents install at runtime now live in the state directory rather than inside the image, so they survive the container being recreated instead of vanishing with it.
- **Stopping the container is graceful.** Nothing installed a signal handler, and the kernel does not deliver default signal actions to process 1, so every `docker stop` was ignored until the timeout expired and the process was killed outright — losing the agents' shutdown, a clean broker disconnect, and anything still to be written. An init process now runs at process 1, so agents shut down properly and stopping is immediate.
- **Docker Compose binds the dashboard, REST API, Prometheus and InfluxDB to localhost.** They were published on every interface, so a development stack was reachable from the whole network by default; they are now reachable from the host only. Remove the `127.0.0.1:` prefix on any port you mean to expose deliberately. The MQTT broker is unchanged — remote nodes connect to it across the network by design.
- **InfluxDB no longer ships with default credentials.** The compose service seeded a real admin account and API token from hardcoded values. Set `INFLUX_USERNAME`, `INFLUX_PASSWORD` and `INFLUX_TOKEN` in `.env` before enabling the `influx` profile; there are no fallbacks.
- **The broker's configuration is mounted read-only.** The Mosquitto image chowns everything under its config directory at startup, which travelled back through the bind mount and rewrote the ownership of a file tracked in this repository — invisible to Git, which does not track ownership, and enough to break the broker under rootless Podman afterwards.
- **The broker no longer logs every health check.** The compose health check reconnects every ten seconds, and each probe produced three lines that buried everything else.

### Removed

- **The local network scan `/deploy` ran when it was given no host.** Discovery fell back to probing port 22 across the whole local `/24`, so a single chat message swept the operator's network for SSH servers and reported what it found — the sort of thing intrusion detection exists to catch, and on an unauthenticated chat endpoint it was available to anyone who could reach the port. Discovery is now a single mDNS lookup of `<node>.local`, which asks about one machine and learns nothing about any other. Set the node's host explicitly if mDNS is not available on your network.
- **The devops-agent example in the orchestrator's prompt.** It was a template for generated code that took an SSH password out of a task payload and connected with host-key checking disabled, so an agent written from it bypassed the credential and host-key handling everywhere else. The prompt already directed the model to the installer agent, which handles SSH deploys natively; it is now the only supported route to a remote machine.
- **The unused `NAUTILUS_SSH_KEY` and `NAUTILUS_STRICT_HOST_KEYS` settings.** Both were read from the environment and never used by anything — leftovers from NautilusAgent, which went with the `wactorz/experimental_agents/` package. The deployment and Windows guides documented them anyway, walking through generating a key and then setting a variable that did nothing; those pages now describe the deploy-target settings that actually apply.
- **Redis support.** It held three values — observed samples, agent metrics, heartbeat state — each of which normal operation rebuilds, so nothing durable was stored there. It was also optional only at startup: connectivity was checked once, and a Redis that died mid-run turned every persistence call into an error that fed the agent-restart path. Those values now live in process memory, which is what every deployment was already using, since the default URL pointed at the application's own container. `REDIS_URL` is no longer read. **If you embed Wactorz:** `init_persistence()` no longer takes `redis_url` and returns `(db, pickle_store)`; `PersistenceAPI(db, pickle_store, name)` drops its middle argument; and hand-written migrations change from `migrate_state_N(db, redis, pickle_store)` to `migrate_state_N(db, pickle_store)`.
- **The `wactorz-monitor` command.** It started the dashboard as a standalone process, separate from the agents, and chat then had to travel to them over MQTT and back. Every supported way of running Wactorz — `wactorz`, the container, the Home Assistant add-on — starts the dashboard in the same process as the agents, so that second path had no users and simply doubled the code every chat message could take. Run `wactorz` instead. As part of this, `POST /chat/stop` no longer returns the `published` field, which only ever reported whether the stop request had been forwarded over MQTT.
- **The MQTT WebSocket listener, on both brokers, and the `mqtt_ws_port` add-on option.** The listener existed for a browser MQTT client the dashboard no longer has — real-time updates arrive over the monitor's own WebSocket — so it was an open endpoint with no consumer, and in the Compose stack it was published to every interface. The add-on's listener on `8083` and the Compose broker's on `9001` are both gone, along with the `mqtt_ws_port` option, `MQTT_WS_EXTERNAL_PORT`, and `--mqtt-ws-port` on `wactorz-monitor`. **If you connect anything else to the broker over WebSockets** — an MQTT client in a browser, Node-RED, a dashboard of your own — add the listener back to `infra/mosquitto/mosquitto.conf` and republish the port. Existing add-on installations may log a one-time "not in schema" notice for the removed option until their configuration is saved again.

### Fixed

- **Entities without a device were invisible to device control and planning.** `fetch_devices_entities_with_location` grouped the entity registry by `device_id` and skipped anything without one, so entities created outside the device registry — SmartIR and template climate, manually configured Local Tuya devices, helpers — never reached the actuator or the planner. They are visible on the user's own dashboard, so "turn on the air conditioner" simply found nothing to act on. Such entities are now emitted as area-grouped pseudo-devices, keeping the output shape (a list of devices) unchanged for every consumer. Disabled entities are now skipped as well: they have no state and cannot be actuated, so offering them to the model can only produce service calls that silently fail.

- **A quoted `LLM_OVERRIDES`, `LLM_PROVIDER` or `LLM_MODEL` no longer sends a model name that does not exist.** Several ways of setting an environment variable keep the quotes as part of the value — Windows `set VAR="…"`, Docker's `env_file`, an unbalanced quote in a hand-edited `.env`. In `LLM_OVERRIDES` those quotes then land on the first site name and the last model name once the value is split on `,` and `=`: the site silently matches nothing and falls back to the global provider, while the model becomes one character away from real and every call for that site fails with `404 … model: claude-sonnet-4-6"` — a planner that 404s its way to a degraded answer while the rest of the system carries on normally. Surrounding quotes are now stripped from the value as a whole and from each site and model, and from `LLM_PROVIDER` and `LLM_MODEL` as well, where the same mistake fails every call rather than one site's.
- **An `LLM_OVERRIDES` entry for a site nothing reads is reported rather than ignored.** Only malformed entries warned; one that parsed cleanly but named a site the code never asks for — a typo like `plannr=`, or a site that has since been renamed — was accepted into the table and then never consulted, which looks exactly like an override that was applied and had no effect. Such an entry is now skipped with a warning naming the known sites. The startup line also logs the parsed overrides rather than the raw string, so what each site actually resolved to is visible, and an entry that was dropped is visible by its absence.
- **`LLM_TEMPERATURE` no longer breaks every Claude call on a current model.** Anthropic removed the sampling parameters with Claude Opus 4.7 and has kept them out of every model since, so a request carrying `temperature` is refused outright — `400 … 'temperature' is deprecated for this model` — and with the setting present in the environment that was every chat, plan and tool call, not an occasional one. The Anthropic provider now omits the parameter for the models that dropped it, and if a model too new to be named refuses it anyway, that request is retried once without it and the parameter is left off for the rest of the run rather than failing again. Providers that still take a temperature are unaffected, and a temperature that is merely out of range is still reported as the configuration error it is. Fallback prices for the newer Claude models were added alongside, so a run on one is costed rather than recorded as free when the price catalogue cannot be fetched.
- **A malformed request body is answered with 400 rather than 500.** The REST endpoints that read named fields off a JSON body raised when handed a list, a bare string or invalid JSON, reporting a server fault for what was a caller's mistake.
- **A paused or stopped agent no longer answers chat.** Pausing and stopping suspend the agent's message queue, but messages sent from the chat view reach an agent directly rather than through that queue — so a paused agent kept replying as though nothing had happened, and a stopped one kept answering after it had been shut down. Both now decline, as does an agent that has failed and is waiting to be restarted. Pausing, resuming, stopping and deleting also behave the same whichever route they arrive by: the dashboard, chat commands, the REST interface, or another agent. Two of those routes previously skipped telling the supervisor that the stop was deliberate, so the agent was restarted moments later, and neither respected protected agents.
- **Pause, resume, stop and delete reach the agent even when the broker is unreachable.** Commands from the dashboard were always sent over MQTT, including to agents running inside the same process — so with the broker down nothing happened, while the dashboard reported that it had and went on showing the agent in its old state until the next heartbeat. Agents running locally are now acted on directly; MQTT is still how agents on other nodes are reached, and a command that could not be delivered is reported as a failure instead of a success. Deleting an agent also releases it from supervision on this path, which it previously did not — the watchdog noticed the silence and restarted the agent that had just been deleted.
- **The document-to-slides agent no longer freezes everything else while it builds.** Installing its Node dependency and running the slide builder were called in a way that stopped the whole process for as long as they took — up to three minutes between them — so no other agent ran, no broker messages were read, and the timeout meant to bound those very steps could not fire. They now run alongside everything else.
- **An interrupted save no longer discards an agent's stored state.** State files were written in place, which empties the file before the new contents are written, so a crash or a full disk partway through left a file that could not be read back. An unreadable state file is treated as an absent one, so the agent restarted with nothing rather than with the previous save. State is now written alongside and swapped in once complete, leaving the last good version untouched if the write fails — and a save that fails is reported rather than noted in debug logs.
- **The dashboard no longer slows the system down as agents are added.** Every message from the broker rebuilt the whole dashboard state and pushed it to each connected browser in turn — and rebuilding it queried the database once per agent, so the cost grew with the number of agents while sitting between the broker and everything waiting on it. A browser on a slow connection made it worse: the server waited for that one client before reading the next message, so one stalled tab held up the others and the agents' own traffic. Live updates now carry only what changed, each browser is served independently, and one that falls too far behind is resent the full picture rather than a partial one.
- **Several defects in how state was stored.** Sensor readings written one at a time were never committed, so anything not going through the batch writer was silently lost. Database connections were opened and never closed, which left the file locked after a `wactorz-reset` and could block cleanup. An agent whose name contained `..` wrote its state file outside the state directory — those files are loaded back with `pickle`, so a name from an untrusted source could place executable content somewhere it would later be run. And agent state ignored a blank `WACTORZ_STATE_DIR`, landing in a directory literally named from the whitespace while the database and logs went elsewhere. Shutting the storage layer down now also discards its short-lived values, so an agent restarted under a name used earlier no longer recalls the previous run's metrics. The layer is split into one module per store, with everything process-wide in one place, so a change to one no longer means reading a thousand lines.
- **Stopping Wactorz took three interrupts, and `docker stop` never worked at all.** The first interrupt did shut the agents down cleanly, but the command-line interface read your typing on a thread that could not be interrupted, and the interpreter then waited for that thread twice more on the way out. Separately nothing listened for the signal a service manager sends, and a process running as the container's first process is given no default handling for it — so `docker stop` and `systemctl stop` waited out their timeout and killed Wactorz mid-write, losing the agents' shutdown and anything not yet saved. One interrupt, or one stop, now shuts everything down in well under a second and reports success rather than failure.
- **The log file assumed the working directory was writable.** `wactorz.log` was opened at a path relative to wherever the process happened to start, while the rest of the state resolved from `WACTORZ_STATE_DIR` — so a service started from a directory it could not write to failed immediately at startup, and clearing the logs could look for them somewhere the application never wrote. The log now sits with the rest of the durable state, and `wactorz-reset` truncates it there.
- **A configured `WACTORZ_STATE_DIR` only moved part of the state.** The central stores honoured it, but every agent was still created with a working-directory-relative `./state`, so a deployment that pinned an absolute durable location — the Home Assistant add-on, or any container with a mounted volume — had agents writing beside the process instead, and the one-time migration of pre-upgrade pickle state looked in the wrong place. The path now resolves in exactly one place for the whole system: an explicit setting first, then `WACTORZ_STATE_DIR`, then `./state`. A blank `WACTORZ_STATE_DIR=` left in a `.env` file counts as unset rather than resolving to the working directory, and `wactorz-reset` still targets the same location the app writes to without creating it just by being imported.
- **GitHub releases no longer paste the entire changelog into the release body.** The release
  workflow used `CHANGELOG.md` verbatim, so every release page carried `[Unreleased]` plus every
  past version — an endless scroll. It now extracts only the section matching the tag, and fails
  the job if no section for that version exists rather than publishing empty notes.

## [0.5.2] - 2026-07-30

### Added
- **Per-call-site LLM overrides (`LLM_OVERRIDES`).** Route individual call sites to different
  providers/models — e.g. `LLM_OVERRIDES="intent=ollama:qwen3:4b,planner=anthropic:claude-sonnet-4-6"`
  runs intent classification on a local model while the planner stays on a hosted one. Sites:
  `main`, `intent`, `planner`, `actuator`, `ha`, `dynamic`; unlisted sites keep the global
  `LLM_PROVIDER`, and a malformed entry falls back to it with a warning instead of failing startup.
- **`LLM_TEMPERATURE`.** Sets the sampling temperature for every LLM call across all five providers
  (Anthropic, OpenAI, Ollama, NIM, Gemini) — `0` for deterministic classification and device
  control. Unset keeps each provider's own default, so existing installs are unaffected.
- **Call-site evaluation harness (`python -m wactorz.evalharness`).** Benchmarks any set of
  `provider:model` specs across the framework's LLM call sites using the production system prompts,
  with automatic scoring (label match, JSON action match, plan validity, codegen compile + required
  functions), latency and cost capture, JSONL records and a CSV/console summary. Takes
  `--temperature` and records it with every result.
- **Startup line naming the active LLM.** Boot now logs `LLM: <provider>/<model> | temperature=…`
  (plus any overrides), so the effective model configuration is visible without external dashboards.
- **Social channels (Discord/Telegram) as capability-restricted companions.** They now run
  *alongside* the primary interface (e.g. the HA add-on dashboard) whenever their token is set,
  instead of only as a standalone `--interface`. Messages reach the main agent in a **restricted
  mode**: full conversation, Home Assistant queries, and device control are allowed, but spawning
  agents, deleting agents, running code, pipelines/automations, and admin (slash) commands are all
  unreachable — enforced at the actions (intent routing, `<spawn>`/`<delete>` execution) rather than
  by guessing intent from the text, so it can't be talked around. Delegation is limited to an
  allow-list of safe native agents (fails closed), so it can't be used to launder code execution
  through a running `DynamicAgent`/`code-agent`. These channels previously routed straight to the
  unrestricted orchestrator. A channel whose token is set but whose
  library is missing is now skipped with a clear warning naming the pip package, instead of failing
  silently. Both `discord.py` and `python-telegram-bot` now ship in `wactorz[all]` (new `telegram`
  extra), so the Home Assistant add-on includes them out of the box. The add-on now also exposes
  `discord_bot_token`, `telegram_bot_token`, and `telegram_allowed_user_id` as configurable options
  (both variants), so users can enter their tokens from the add-on UI.
- **Device control from a social channel is limited to everyday domains.** The one-off actuator
  executes the Home Assistant `domain.service` the model resolved, so "control my devices" used to
  reach `shell_command` and `python_script` (arbitrary code on the HA host), `hassio`, and
  `homeassistant.stop`. Restricted callers now pass an allow-list - lights, switches, fans, covers,
  climate, media players, vacuums, humidifiers, water heaters, input booleans and scenes - enforced
  at the call site, not in the resolver prompt. Out-of-policy calls are dropped, logged, and named
  in the reply so a partly-blocked request never reads as if it all went through. The dashboard and
  CLI are unaffected and keep full access.
- **Sender allow-lists are required on every social channel.** `DISCORD_ALLOWED_USER_IDS`,
  `TELEGRAM_ALLOWED_USER_IDS` and `WHATSAPP_ALLOWED_NUMBERS` (comma-separated) decide who may talk
  to a bot at all. A channel with a token but no allow-list refuses to start rather than answering
  whoever finds it - the exception being Telegram, which runs in **setup mode**: it answers `/start`
  with the sender's user id and nothing else, so the id needed to fill the allow-list is still
  discoverable without exposing the LLM or the user's home. `TELEGRAM_ALLOWED_USER_ID` (singular)
  is still honored. WhatsApp, whose webhook is a public HTTP endpoint, now also runs in restricted
  mode like the other two.
- **Per-sender rate limit on social channels.** Each inbound message costs at least an intent
  classification plus a completion, so `SOCIAL_RATE_LIMIT_PER_MIN` (default 12, `0` disables) caps
  messages per sender per minute, and a sender's next message is refused while their previous turn
  is still generating. Both limits reply with a short explanation instead of going quiet.
- **Catalog recognises experimental/beta agents.** Catalog recipes can be tagged
  `stability: beta` with a warning; `reachy-mini` is the first one. Beta agents are **hidden by
  default** in `@catalog list` (shown behind a hint; reveal with `list experimental`), the catalog
  warns before spawning one, and the first message to a running beta agent shows a one-time
  instability warning.
- **`ha_connection` add-on option** (`auto` / `supervisor` / `custom`) — explicit Home Assistant connection mode for both add-on variants. `auto` keeps the previous token-presence inference, so existing installs are unaffected. Startup now also logs one deterministic line with the resolved mode, URL, and auth result (e.g. `HA connection OK — mode=supervisor ...` or `HA auth FAILED (401) ...`).
- **Extension seam (`wactorz/ext/`).** Optional features live in self-contained folders that expose a
  `setup(app)` hook; the monitor auto-discovers and wires them at startup, and each may contribute
  non-secret browser config to `/api/config`. Text-to-speech is now packaged as the first such
  extension (`wactorz/ext/tts/` + `frontend/src/ext/tts/`), with no change to its behavior.
- **Frontend extension registries.** Extensions can now add dashboard tabs
  (`CardDashboard.registerView`), custom icons (`registerIcon`), and `/api/config`-seeded settings
  (`registerConfigEntry` in the new `config/serverConfig.ts`) without touching core files. The HA
  URL seeding moved into the same mechanism; TTS availability is now read from the server config
  instead of always probing.

### Changed

- **`--interface discord` / `--interface telegram` / `--interface whatsapp` are now restricted
  too.** The guarantees above live in the interface classes, not in the companion wiring, so a
  social channel is capability-restricted whether it runs alongside the dashboard or as the primary
  interface. There is no longer a way to drive spawning, deletion or code execution from a chat bot;
  that surface is the dashboard, the CLI and the authenticated REST interface. **Action required:**
  a deployment that sets a bot token must now also set the matching allow-list, or that channel will
  not start.
- **Dashboard uses a single WebSocket transport.** Live agent/system/node data and Home Assistant
  activity now stream to the browser as server-push over `/ws`; the dashboard no longer opens its own
  MQTT connection to the broker, and the browser receives no broker credentials.
- **Home Assistant add-on split into two variants.** The store now offers **Wactorz** (slim, Alpine,
  ~200 MB) and **Wactorz Ultra** (Debian + ML/`ultralytics`, ~3 GB) as separate cards; both share the
  same options and entrypoint. CI builds and pushes both variants across `aarch64`/`amd64`.

### Removed

- **`main.run_pipeline()`.** It imported a `task_manager` module that does not exist, so every call raised `ImportError` — while the orchestrator's prompt actively advertised it as a capability for multi-agent tasks. Both the method and the prompt section are gone; delegation to named agents is unaffected.
- **`wactorz/experimental_agents/` package.** The ten scratch agents in it (`code`, `news`, `qa`,
  `tick`, `wif`, `wiz`, `ml`, `nautilus`, `udx`, `weather`) were test scaffolding, were never
  reachable from the catalog, and nothing outside the folder imported them. `reachy-mini` is now the
  only agent carrying experimental/beta status, and it lives in `catalogue_agents/` like every other
  recipe.

### Fixed

- **The `mcp` extra now excludes the incompatible 2.x line (`mcp>=1.0.0,<2`).** `mcp` 2.0.0 removed
  `mcp.server.fastmcp`, so a fresh `pip install wactorz[mcp]` picked up a release the MCP interface
  cannot import. The pin restores installability while the 2.x migration is worked out separately.
- **An unresponsive Ollama server hung the conversation indefinitely.** None of the three calls to it had a time limit, so a wedged or half-started server left the turn waiting with no error and no way out short of a restart. They are now bounded — generously, because a large prompt on a local machine legitimately takes minutes, and for streaming replies the limit applies to the gap between chunks rather than the whole reply, so a long answer is never cut short. HTTP errors from Ollama were also being read as an empty reply rather than raised, which turned a server-side failure into a silently blank answer.
- **A task that failed never told whoever asked for it.** Only one specific kind of failure sent a reply; everything else — including a missing or misconfigured LLM provider — was logged locally and dropped, leaving the caller waiting out its own timeout with no idea what happened. Every outcome now answers, so a failed task reports the reason instead of looking like a slow one.
- **A Gemini reply that stopped early looked like it had finished.** When the stream stalled, the partial answer was delivered with an ordinary completion marker, so nothing downstream — the chat log, the activity feed, or any retry logic — could distinguish half an answer from a whole one. The text that did arrive is still delivered, and the tokens are still billed, but the result now says the reply is incomplete, and the truncation is logged.
- **Home Assistant commands could wait forever.** A Home Assistant instance that stays connected but stops answering left every command — turning on a light, reading a device's state, listing the registry — waiting indefinitely, since only a dropped connection was detected. Commands now give up after a generous interval and are retried by the agents that hold a connection open. Waiting for the next event from a subscription is deliberately still unlimited: silence there is normal.
- **WhatsApp replies briefly froze the entire system.** Sending a reply used a blocking call, so every agent in the process stopped for the length of a network round-trip to Twilio, on every message received. Looking for a device on the local network did the same thing for several seconds per attempt. Both now run without holding everything else up.
- **`run.sh` refused to start after copying `.env.template` to `.env`, as the setup instructions say to do.** The launcher loaded the file by word-splitting it, which chokes on the inline comments and quoted values the template itself ships — and because the script aborts on error, the launch died there. It now sources the file, so comments, quoted values and empty settings are all read the way a shell reads them.
- **Clearing the logs could wedge all logging until restart.** If truncating a log file failed part-way, the lock protecting that file was never released. The failure was caught and reported as a warning, so the reset looked like it worked, while every subsequent log call blocked forever — and nothing appeared in the log to explain it, because logging was what had stopped.
- **The documented chat API didn't work when copied.** The `POST /api/chat` examples named an agent that doesn't exist, so following them produced a 404, and the Home Assistant `rest_command` example sent fields the endpoint never reads (`to`/`content` rather than `message`), so that integration had never worked at all. Both now match the endpoint.
- **Changelog housekeeping.** Two different releases were both labelled `0.4.2`; the later block also carried several hundred lines of engineering notes that were never release notes. Its actual entries have moved to `0.4.3`, where they shipped, and version headings now use one consistent date separator.
- **Building a wheel could bundle a dashboard that didn't match its source.** The packaging hook decided whether to rebuild the frontend from the *age* of the built output, so an edit made within ten minutes of the last build was treated as already built and the wheel shipped the previous bundle. The same rule rebuilt after any idle period even when nothing had changed, rewriting the committed `static/app` and leaving a dirty working tree. It now compares a content hash of the frontend inputs, so a rebuild happens when — and only when — something that affects the bundle actually changed. Release builds, which force a rebuild unconditionally, were never affected.
- **Clearing one agent's chat deleted your messages to every other agent.** The scoped reset dropped the named agent's replies *and* every message you had ever sent, across all threads — only the other agents' replies survived, leaving conversations that read as one-sided. It now removes just that thread: the agent's replies and the messages addressed to it.
- **Two agents replying at once merged into a single bubble** attributed to whichever started first, because stream text accumulated in one shared buffer. Buffers are now kept per agent, so concurrent replies stay separate and attributed correctly.
- **A message that failed to send stayed in the thread looking delivered.** The bubble is drawn before the send is attempted; when the send failed you got a warning but the bubble remained. It is now withdrawn. (The previous release fixed the same thing in the activity feed; the thread still lied.)
- **A chat history that failed to load stayed empty for the rest of the session.** A network failure was indistinguishable from an agent with genuinely no history, and either way the agent was marked as loaded — so reopening the thread never retried. Only a successful fetch marks it loaded now.
- **The bottom navigation and audio settings leaked listeners on every rebuild**, each one left holding a detached element. Rebuilds now retire the previous set. This is the same defect as the popover leak fixed in the previous release, in the two places that fix didn't reach.
- **Header popovers and the mobile "More" sheet ignored Escape** and left focus stranded. Both now close on Escape and return focus to the button that opened them.
- **The chat composer got stuck, or freed itself during someone else's turn.** The send/stop toggle reacted to any chat frame at all, so unrelated agent-to-agent traffic re-enabled Send and hid Stop while the user's own reply was still streaming. Conversely, the only ways out of the busy state were a stream-end or a reply frame, so if the backend went away mid-turn neither arrived: Send stayed disabled and Stop stayed visible but did nothing — its request went to the dead backend and the failure was discarded — leaving no working control until the backend came back or the page was reloaded. A turn now belongs to the agent it was sent to (keyed on the sender, since a genuine reply is addressed to the user just as a bystander's message may be), and it releases itself if the transport stops being live or the turn goes silent, without cutting off a slow model that is still sending. A stop that does not get through now says so.
- **A message that failed to send still appeared in the feed** as though it had been delivered, then vanished on the next refresh because nothing had persisted it. Only messages the transport accepted are shown now. Repeated backend failures are also reported once at warning level instead of only at debug, which production builds discard.
- **A rejected spend-limit change looked like it had worked.** Saving a limit or resetting spend ignored the response status and then re-rendered from the server, redisplaying the unchanged value as if it were the new one. Both now report a failure and leave the old value visible.
- **The image lightbox was a keyboard trap in the wrong direction.** It announced itself as a modal dialog but never moved focus into itself, so Tab walked into the page behind it and a keyboard user had no way to reach the only control that dismissed it. It now takes focus, keeps Tab within it, returns focus to whatever opened it, and has a visible close button. Opening a second preview no longer leaves the first still listening for keystrokes.
- **The header leaked a popover and a document listener on every rebuild.** The audio and reset popovers are attached to the page body rather than the header, so replacing the header (which happens whenever an extension registers a view) left the old ones behind, accumulating. Long-gone remote nodes are likewise no longer kept forever — nodes that merely went quiet are still shown as offline.
- **`gpt-4o-mini` was billed at `gpt-4o` rates** — model pricing falls back to a table keyed by name prefix, and the shorter `gpt-4o` key matched first, so the cheaper model was costed ~17x too high whenever the live price catalogue was unavailable (notably at startup, and offline). The same number drives reported spend, the budget check and the spend cap, so a limit could fire early. The lookup now prefers the longest matching prefix, and `pricing_info()` — which carried its own copy of the same lookup — shares it, so the reported rate always matches the rate charged. Dated variants such as `gpt-4o-2024-08-06` still inherit their family's price.
- **`POST /api/chat` was unusable** — the default agent name did not match the registered orchestrator, so a request without an explicit `agent_name` returned 404. Requests that did name a valid agent got further and then failed silently: the reply callback was a plain lambda where a coroutine was awaited, so the stream was abandoned after the tokens were billed and no reply was delivered.
- **Gemini completions froze every agent** — `GeminiProvider.complete` and `complete_with_tools` called the *synchronous* google-genai surface from inside `async def`, blocking the single shared event loop for the entire model round-trip. With a Gemini provider configured, one agent's LLM call stalled every other actor, delayed MQTT keepalive, and could trip the 35 s heartbeat watchdog into force-restarting healthy agents as "presumed crashed". Both paths now `await client.aio.…` instead. Streaming was already off-loop and is unchanged.
- **Silent HA misconfiguration in the add-on** — a custom `ha_url` with a blank `ha_token` (or a token set in supervisor mode) used to fail quietly: the Supervisor proxy only accepts the injected Supervisor token, so the custom URL was silently discarded. The add-on now logs a loud warning explaining what is ignored and why, in both `run.sh` variants.
- **Devices nav link dead in add-on supervisor mode** — `/api/config` exposes the backend's HA URL verbatim, which in supervisor mode is the container-internal `http://supervisor/core`; the browser cannot resolve it. The dashboard now rewrites container-internal HA URLs to the page's own origin (which under ingress *is* the Home Assistant UI).
- **Migration to a non-existent node made the agent disappear** — `migrate_agent` accepted any target name blindly: the source stopped the agent (deleting its state) and shipped it to a node topic nobody was listening on, so a typo'd or offline target destroyed the agent. The target node is now validated against live heartbeats before anything destructive happens; if it is unknown or offline the migration is refused with a message listing the nodes that are online, and the agent stays where it is.
- **Native catalog agents vanished after a restart** — `weather-agent`, `gmail-agent`, and `google-calendar-agent` (the `type: native` catalog agents) were spawned but never written to the spawn registry, so a process restart dropped them while code-recipe agents survived. They are now persisted on spawn (as a JSON-safe descriptor) and re-resolved to their class and restored on startup.
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

### Added

- **LLM spend limit enforcement** - hard cap on LLM API spend per period (daily, weekly, or monthly). Set via `LLM_COST_LIMIT_USD` / `LLM_COST_LIMIT_PERIOD` env vars or at runtime from the dashboard Settings tab without restart. When the limit is reached, further LLM calls are blocked and a "limit reached" message is delivered as a chat reply. Spend accumulates into all three period keys simultaneously so switching periods always shows real data. New REST endpoints: `GET /api/cost`, `POST /api/cost/limit`, `POST /api/cost/reset`. Env-var values are the startup default; GUI override persists in SQLite and takes priority.

### Changed

- **HomeAssistantAgent** — `create_automation` intent is temporarily disabled; requests are routed to `_recommend_hardware` instead.
- **HomeAssistantAgent** — Edit automation flow refactored into three focused helpers (`_identify_automation`, `_get_automation_config`, `_generate_modified_automation_config`) with `AutomationEditError` for internal error propagation.
- **HomeAssistantAgent** — All LLM system prompts extracted to `wactorz/agents/prompts/home_assistant_prompts.py`.
- **ha_helper** — Type hints modernised (`Optional[str]` → `str | None`, `List[Dict]` → `list[dict]`); URL helpers reorganised; `get_automations` rewritten.

### Fixed

- **HomeAssistantAgent** — Non-dict LLM response no longer crashes the delete/edit path (guard ordering corrected).
- **HomeAssistantAgent** — Stale `devices["devices"]` key corrected to `devices["data"]` throughout hardware recommendation and entity extraction helpers.
- **`monitor_server` stdio wrapping under pytest** - `monitor_server` no longer re-wraps `sys.stdout` / `sys.stderr` at import time when they have already been replaced by a test capture harness. Prevents `ValueError: I/O operation on closed file` during pytest teardown on Python 3.13 + Windows.

### Tests

- Comprehensive test suite added for `ha_helper` (`tests/test_ha_helper.py`) and `HomeAssistantAgent` (`tests/test_home_assistant_agent.py`).

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

## [0.4.1] - 2026-05-06

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

## [0.4.0] - 2026-04-25

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

## [0.3.0] - 2026-04-18

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

## [0.2.0] - 2026-03-13

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

## [0.1.0] - 2025-11-01

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

[Unreleased]: https://github.com/waldiez/wactorz/compare/v0.5.3...HEAD
[0.5.3]: https://github.com/waldiez/wactorz/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/waldiez/wactorz/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/waldiez/wactorz/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/waldiez/wactorz/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/waldiez/wactorz/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/waldiez/wactorz/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/waldiez/wactorz/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/waldiez/wactorz/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/waldiez/wactorz/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/waldiez/wactorz/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/waldiez/wactorz/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/waldiez/wactorz/releases/tag/v0.1.0
