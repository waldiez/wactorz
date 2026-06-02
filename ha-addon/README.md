# ha-addon

Home Assistant addon that packages Wactorz as a supervised addon for Home Assistant OS and Supervised installs.

> **Not for HA Container/Core.** Those variants don't have the Supervisor. Use the Docker deployment instead.

## Folder structure

```
ha-addon/
├── config.yaml   # Addon manifest: name, version, ports, options schema
├── Dockerfile    # Alpine + Java 17 + Mosquitto + Fuseki + wactorz[all]
├── run.sh        # Entrypoint: reads options.json, exports env vars, starts services
├── DOCS.md       # User-facing install/options reference (rendered in HA UI)
├── icon.png      # 144×144 addon icon
└── logo.png      # Wactorz logo shown in store listing
```

## How it works

1. **HA Supervisor** builds the image from `Dockerfile` and runs it as a container.
2. **`/data/options.json`** — Supervisor writes user-configured values (from config.yaml `options:`) here at boot time.
3. **`run.sh`** reads `options.json` via `jq`, exports env vars (`WACTORZ_*`, `MQTT_*`, etc.), and then:
   - Optionally starts embedded Mosquitto (if `mosquitto_embedded: true`).
   - Optionally starts embedded Fuseki (if `fuseki_embedded: true`).
   - Launches `wactorz` (the main Wactorz server).
4. **Ingress** — HA proxies the addon UI through the Supervisor ingress tunnel on port 8888, so the UI is accessible directly from the HA sidebar without exposing a port.

### HA Supervisor token vs long-lived token

`run.sh` prefers the Supervisor-injected token (`$SUPERVISOR_TOKEN`) for HA API calls when the addon declares `hassio_api: true`. The user-provided `ha_token` option is a fallback for cases where a long-lived token is needed (e.g. a specific integration that bypasses the Supervisor API).

## Dockerfile details

| Layer | What it installs |
|---|---|
| Base image | `ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20` (or amd64 variant) |
| `apk add` | curl, git, jq, gcc, musl-dev, linux-headers, libffi-dev, openssl-dev, OpenJDK 17 JRE, Mosquitto |
| Fuseki | Downloaded from Apache archives at build time; unpacked to `/opt/fuseki` |
| Wactorz | `pip3 install 'wactorz[all] @ git+…@main'` |
| Entrypoint | `run.sh` copied to `/run.sh` |

`BUILD_VERSION` ARG is passed by the Supervisor on each build — it busts the pip install layer cache when the addon version in `config.yaml` is bumped.

## Ports

| Port | Purpose | Exposed by default |
|---|---|---|
| 8000/tcp | Wactorz REST + WebSocket API | Yes |
| 8888/tcp | Wactorz Monitor UI (ingress) | Yes |
| 3030/tcp | Fuseki SPARQL (embedded only) | Yes (inactive unless enabled) |
| 1883/tcp | MQTT TCP (embedded only) | No (mapped to `null`) |

## Embedded services

Both optional services are bundled in the addon image and started on demand:

- **Mosquitto** (`/etc/mosquitto/`) — started before Wactorz; data persisted to `/share/mosquitto`.
- **Fuseki** (`/opt/fuseki/`) — started before Wactorz; data persisted to `/share/fuseki`. Auth credentials regenerated from `fuseki_user`/`fuseki_password` on every boot.

## Local development / testing

There is no local HA Supervisor to run the addon, but you can test `run.sh` directly:

```bash
# Simulate the options.json that HA Supervisor would write
cp ha-addon/config.yaml /tmp/options.json  # or hand-craft a minimal one:
cat > /tmp/options.json <<'EOF'
{
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-6",
  "llm_api_key": "sk-ant-...",
  "mqtt_host": "localhost",
  "mqtt_port": 1883,
  "mqtt_ws_port": 8083,
  "ha_url": "http://homeassistant.local:8123",
  "ha_token": "",
  "mosquitto_embedded": true,
  "fuseki_embedded": false,
  "fuseki_url": "http://localhost:3030",
  "fuseki_dataset": "wactorz",
  "fuseki_user": "admin",
  "fuseki_password": "admin",
  "discord_bot_token": "",
  "telegram_bot_token": "",
  "telegram_allowed_user_id": 0,
  "otel_endpoint": "",
  "otel_service_name": "wactorz",
  "influx_url": "",
  "influx_token": "",
  "influx_org": "wactorz",
  "influx_bucket": "wactorz"
}
EOF
OPTIONS_PATH=/tmp/options.json bash ha-addon/run.sh
```

For a full addon integration test use the [HA addon development environment](https://developers.home-assistant.io/docs/add-ons/testing).

## Versioning

The addon version lives in `config.yaml` (`version: "x.y.z"`). It must be bumped whenever a new release is cut — this is what triggers the Supervisor to offer an update to users and busts the Docker layer cache for the pip install step.
