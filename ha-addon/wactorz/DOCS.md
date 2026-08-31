# Wactorz — Home Assistant Addon

Actor-model multi-agent AI framework. Spawn, coordinate, and monitor AI agents that can read and control your Home Assistant.

> **Requires Home Assistant OS or Supervised.**
> The Supervisor (which runs addons) is not available on Home Assistant Container or Core installs.
> If you are running Home Assistant in Docker, use the [Docker deployment](https://hub.docker.com/r/waldiez/wactorz) instead.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Click **⋮ (menu) → Repositories** and add `https://github.com/waldiez/wactorz`, then click **Add**.
3. Find **Wactorz** in the store and click **Install**.
4. Start the addon and open the Web UI from the addon page.
5. Configure your LLM key under **Options** (see below).

## Options

| Option | Default | Description |
| --- | --- | --- |
| `api_key` | *(blank)* | Only consulted if you publish a port (see below). The Wactorz panel does not use it. |
| `llm_provider` | `anthropic` | LLM backend: `anthropic`, `openai`, `gemini`, `ollama`, `nim` |
| `llm_model` | `claude-sonnet-4-6` | Model name for the chosen provider |
| `llm_api_key` | *(blank)* | API key for the chosen provider |
| `llm_cost_limit_usd` | `0` | Spend cap in USD. `0` disables enforcement; resets automatically each period. |
| `llm_cost_limit_period` | `monthly` | Period for the spend cap: `daily`, `weekly`, or `monthly`. |
| `ollama_url` | `http://localhost:11434` | Ollama base URL (only used when `llm_provider: ollama`) |
| `openai_url` | *(blank)* | OpenAI-compatible base URL (only used when `llm_provider: openai`). Leave blank for the real OpenAI API, or point to a LiteLLM proxy, Groq, Together, vLLM, LM Studio, etc. |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname — use `core-mosquitto` for the official Mosquitto addon |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | *(blank)* | Broker username (optional). Leave blank for an anonymous broker; **required for the official Mosquitto addon** (it disables anonymous access). |
| `mqtt_password` | *(blank)* | Broker password (optional). |
| `mosquitto_embedded` | `false` | Start a bundled Mosquitto broker inside the addon (no external addon needed) |
| `ha_connection` | `auto` | `auto`: use the Supervisor proxy when `ha_token` is blank, your `ha_url` otherwise. `supervisor`/`custom`: force a mode explicitly. |
| `ha_url` | `http://homeassistant.local:8123` | Home Assistant base URL seen from inside the addon container (only used in `custom` mode) |
| `ha_token` | *(blank)* | Long-lived access token (HA → Profile → Security → Long-Lived Access Tokens). Blank = Supervisor proxy mode, `ha_url` ignored |
| `discord_bot_token` | *(blank)* | Discord bot token (optional). Requires `discord_allowed_user_ids`. |
| `discord_allowed_user_ids` | *(blank)* | **Required with the token** — comma-separated Discord user IDs allowed to talk to the bot. Without it the bot will not start. Enable Developer Mode, right-click your name, Copy User ID. |
| `telegram_bot_token` | *(blank)* | Telegram bot token (optional). Requires `telegram_allowed_user_ids`. |
| `telegram_allowed_user_ids` | *(blank)* | **Required with the token** — comma-separated Telegram user IDs. Without it the bot only answers `/start` with your user ID, so you can fill this in and restart. |
| `telegram_allowed_user_id` | `0` | Older single-ID form of the above; still honoured. `0` means unset. |
| `social_rate_limit_per_min` | `12` | Max messages per minute per sender on the bots. `0` disables the limit. |
| `deploy_targets` | `[]` | Remote machines `/deploy <name>` may bootstrap over SSH. A list of objects; each node needs a broker it can reach over the network — see [Remote edge nodes](#remote-edge-nodes) below. |

> **`api_key` and publishing a port.** Nothing is published to your network by
> default: the panel reaches Wactorz through ingress, where Home Assistant has
> already signed you in and the request is verified as coming from the
> Supervisor. On that path the key is never consulted, which is why setting one
> changes nothing for panel users.
>
> It matters in one case. If you assign a host port to `8000` or `8888` under
> the add-on's **Network** settings, the API and dashboard land on your network
> directly, and anything that can reach them can delete agents, read the chat
> log and spend your LLM budget. Outside the add-on, Wactorz refuses to start in
> that configuration — but the add-on declares its exposure already handled,
> which is true right up until you publish a port, and that declaration switches
> the refusal off. **Set `api_key` before publishing a port**, and reach the API
> with `X-API-Key: <your key>` or `Authorization: Bearer <your key>`. Something
> like `openssl rand -hex 32` gives a key nobody has to remember.
>
> **The bots are capability-restricted.** Discord and Telegram allow conversation, Home Assistant
> questions, and everyday device control (lights, switches, climate, covers, media players). They
> cannot spawn or delete agents, run code, create automations, or reach Home Assistant service
> domains like `shell_command`, `python_script` or `hassio` — use the dashboard for those. The
> allow-lists are required because a bot that answers strangers would let them control your home
> and spend your LLM budget.

## Remote edge nodes

Wactorz can bootstrap a Raspberry Pi or other machine as an edge node over SSH, running agents there that appear in the dashboard alongside local ones. The machines it may connect to are listed in `deploy_targets`, and each entry carries its own credentials:

```yaml
deploy_targets:
  - name: rpi-kitchen
    host: 192.168.1.50
    user: pi
    key: /config/ssh/rpi_kitchen      # preferred over a password
    broker: 192.168.1.10              # broker address as seen FROM the Pi
  - name: rpi-garage
    host: 192.168.1.51
    user: pi
    password: "…"                     # only if the node has no key auth
    broker: 192.168.1.10
```

Per-entry fields: `name` and `host` (omit `host` to resolve `<name>.local` over mDNS), plus optional `user` (default `pi`), `key`, `password`, `broker`, `broker_port` (default `1883`), `broker_user`, `broker_password` and `ssh_port` (default `22`).

`user`, `key` and `password` are the **SSH** login. `broker_user` and
`broker_password` are the node's **broker** account, and are separate on
purpose — see below.

Private keys go under `/config` or `/share` — both are mapped into the addon — and the path is given as the addon sees it, e.g. `/config/ssh/rpi_kitchen`. Then, from the chat:

```text
/deploy rpi-kitchen
```

### The broker has to be reachable from the node

A remote node is not inside the addon — it runs on its own machine and connects
back over the network. It therefore needs an MQTT broker it can both **reach**
and **connect to**, and not every setup provides one:

| `mqtt_host` setting | Remote nodes |
| --- | --- |
| `mosquitto_embedded: true` | **Supported, but only if you publish port `1883`.** The broker runs inside the addon container, so nothing outside can reach it until you assign a host port under the addon's **Network** settings. It requires a password, which is generated once, kept across restarts and updates, and delivered to each node by `/deploy`. |
| `core-mosquitto` (official Mosquitto addon) | **Supported.** Set `mqtt_username` and `mqtt_password` to an account that addon accepts; `/deploy` delivers them to the node. |
| An external broker on your network | **Supported**, with or without credentials. Set `mqtt_host` to its address, and set each target's `broker` to the address the *node* should use to reach it. |

Credentials reach a node out of band. `/deploy` writes them to `~/wactorz/.env`
there (mode `0600`) over the SSH connection it already has, and the node's
runner sources that file rather than taking them on a command line — so they
appear in no process listing. They cannot travel over the broker itself, which
is the one channel that is unauthenticated until they arrive.

A node uses its own `broker_user` / `broker_password` when you set them, and
this addon's own broker account otherwise. That default is the workable one for
a single broker with one account, but it means **a stolen edge device holds full
broker access** — and the broker carries the code spawned agents run. Give a
node its own account when that matters:

```yaml
deploy_targets:
  - name: rpi-garage
    host: 192.168.1.51
    key: /config/ssh/rpi_garage
    broker: 192.168.1.10
    broker_user: rpi-garage
    broker_password: "…"
```

The account has to exist on the broker already — this sets what the node
presents, it does not create anything. With the **official Mosquitto addon**,
add it as a Home Assistant user. With **`mosquitto_embedded`** you cannot yet:
the addon generates a single `wactorz` account and rewrites its password file on
every start, so an account added by hand does not survive a restart.

If your broker accepts anonymous connections, nothing is sent and nothing needs
to be. If you only need agents on the machine running Home Assistant, leave
`deploy_targets` empty — everything else works unchanged.

> **Credentials never go through chat.** `/deploy` takes a node name and nothing else, and the installer ignores credentials supplied in a task payload. Anything typed into chat is written to the conversation history and the chat log, where it stays long after the deploy finished.

SSH host keys are verified. A machine that has not been connected to before has its key recorded on first contact (stored under `/data/state/known_hosts`, which survives addon updates); a later change to that key fails the connection instead of being accepted silently.

## MQTT

**Option A — use the official Mosquitto addon:**
Install the [Mosquitto broker addon](https://github.com/home-assistant/addons/tree/master/mosquitto), leave `mqtt_host` as `core-mosquitto` and `mqtt_port` as `1883`.

**Option B — embedded broker (no extra addon):**
Set `mosquitto_embedded: true`. Wactorz starts its own Mosquitto instance inside the container. Change `mqtt_host` to `localhost`. MQTT data is persisted to `/data/mosquitto`.

## Embedded services

Setting `mosquitto_embedded` to `true` bundles a Mosquitto broker inside the Wactorz container — no separate addon required.

| Option | Port | Data path |
| --- | --- | --- |
| `mosquitto_embedded: true` | `1883` TCP (exposed as addon port) | `/data/mosquitto` |

## Home Assistant integration

Two connection modes exist — the Supervisor token only authenticates against the internal proxy, so no other combinations are valid:

- **Supervisor proxy (default, zero-config):** leave `ha_token` blank (and `ha_connection: auto`). Wactorz connects through `http://supervisor/core` with the injected Supervisor token. `ha_url` is **ignored** in this mode — the add-on logs a warning if you set a custom one without a token.
- **Custom URL:** set `ha_url` to your HA instance (e.g. `http://homeassistant.local:8123`) **and** generate a long-lived access token in HA → Profile → Security → Long-Lived Access Tokens, then paste it into `ha_token`.

Set `ha_connection` to `supervisor` or `custom` only if you want to force a mode explicitly; `auto` infers it from `ha_token` presence as above.

On startup the add-on probes the connection and logs one line with the mode, URL, and auth result (e.g. `HA connection OK — mode=supervisor ...` or `HA auth FAILED (401) ...`) — check the add-on log first if HA integration misbehaves.

## Support

- Documentation: <https://docs.waldiez.io/wactorz/>
- Issues: <https://github.com/waldiez/wactorz/issues>
