# ha-addon

Home Assistant addon that packages Wactorz as a supervised addon for Home Assistant OS and Supervised installs.

> **Not for HA Container/Core.** Those variants don't have the Supervisor. Use the Docker deployment instead.

## Folder structure

```
ha-addon/
├── config.yaml   # Addon manifest: name, version, image:, ports, options schema
├── build.yaml    # Per-arch base images + WACTORZ_REF build arg (CI + Supervisor)
├── Dockerfile    # Alpine + Mosquitto + wactorz[all]
├── run.sh        # Entrypoint: reads options.json, exports env vars, starts services
├── DOCS.md       # User-facing install/options reference (rendered in HA UI)
├── icon.png      # 144×144 addon icon
└── logo.png      # Wactorz logo shown in store listing
```

## How it works

1. **HA Supervisor** **pulls** the prebuilt multi-arch image named by `image:` in `config.yaml` (`ghcr.io/waldiez/wactorz-addon-{arch}`, built + pushed by `.github/workflows/addon-image.yml`) and runs it as a container — so updates download with a progress bar instead of building on-device. For local source testing you can drop the `image:` key to make Supervisor build from `Dockerfile` instead (see `LOCAL_TESTING.md`).
2. **`/data/options.json`** — Supervisor writes user-configured values (from config.yaml `options:`) here at boot time.
3. **`run.sh`** reads `options.json` via `jq`, exports env vars (`WACTORZ_*`, `MQTT_*`, etc.), and then:
   - Optionally starts embedded Mosquitto (if `mosquitto_embedded: true`).
   - Launches `wactorz` (the main Wactorz server).
4. **Ingress** — HA proxies the addon UI through the Supervisor ingress tunnel on port 8888, so the UI is accessible directly from the HA sidebar without exposing a port.

### HA Supervisor token vs long-lived token

`run.sh` prefers the Supervisor-injected token (`$SUPERVISOR_TOKEN`) for HA API calls when the addon declares `hassio_api: true`. The user-provided `ha_token` option is a fallback for cases where a long-lived token is needed (e.g. a specific integration that bypasses the Supervisor API).

## Dockerfile details

| Layer | What it installs |
|---|---|
| Base image | `ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20` (or amd64 variant) |
| `apk add` | curl, git, jq, gcc, musl-dev, linux-headers, libffi-dev, openssl-dev, Mosquitto |
| Wactorz | `pip3 install 'wactorz[all] @ git+…@${WACTORZ_REF}'` (ref defaults to `main`; set via `build.yaml`) |
| Entrypoint | `run.sh` copied to `/run.sh` |

`BUILD_VERSION` ARG is passed by the Supervisor on each build — it busts the pip install layer cache when the addon version in `config.yaml` is bumped.

## Ports

| Port | Purpose | Exposed by default |
|---|---|---|
| 8000/tcp | Wactorz REST + WebSocket API | Yes |
| 8888/tcp | Wactorz Monitor UI (ingress) | Yes |
| 1883/tcp | MQTT TCP (embedded only) | No (mapped to `null`) |

## Embedded services

- **Mosquitto** (`/etc/mosquitto/`) — optionally started before Wactorz (`mosquitto_embedded: true`); data persisted to `/share/mosquitto`.

> Fuseki / SPARQL has been **removed** — no embedded server, no connection options, and the UI "Graph" tab is gone. Wactorz runs without a triplestore.

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

## Troubleshooting

### Voice input (mic button) doesn't work in the addon

The mic uses the browser Web Speech API + `getUserMedia`, which only work in a
**secure context** (HTTPS or `localhost`). Behind HA's **ingress** the UI is
served over plain HTTP inside an iframe, so `navigator.mediaDevices` is
`undefined` and the mic can't start — the dashboard hides the mic button there
and shows a one-time notice. This is a browser restriction, not an addon bug.

Voice works where the page is a secure context:
- the **desktop app** (loads `http://127.0.0.1`, treated as secure), or
- opening the dashboard over **HTTPS** in a standalone browser tab.

Note that even over HTTPS, mic access *inside the ingress iframe* may still be
blocked because HA's ingress iframe does not grant the `microphone` permission —
so the reliable place for voice is the desktop app or a standalone HTTPS tab,
not the embedded ingress view.

## Versioning

The addon version lives in `config.yaml` (`version: "x.y.z"`) and must match the **published image tag** — Supervisor pulls `{image}:{version}` (e.g. `ghcr.io/waldiez/wactorz-addon-{arch}:0.4.4`). Bumping it is what triggers Supervisor to offer users an update. On a release, push a `vX.Y.Z` tag: `addon-image.yml` builds + pushes the matching per-arch image (stripping the `v`), and `scripts/sync_versions.py` keeps all version sources in lockstep. (For a local source build with no `image:`, bumping `version` instead busts the Dockerfile's pip layer cache via `BUILD_VERSION`.)
