# Interfaces

Wactorz supports multiple user-facing interfaces simultaneously. All of them funnel messages into the same `MainActor.process_user_input()` pipeline — switching interface only changes how you reach the system, not how it behaves.

## Overview

| Interface | Flag | Extra dep | Notes |
|-----------|------|-----------|-------|
| **CLI** | `--interface cli` | — | Default. Interactive terminal with streaming. |
| **REST** | `--interface rest` | — | HTTP API, suitable for programmatic access. |
| **MCP** | separate `wactorz-mcp` process | `wactorz[mcp]` | Model Context Protocol tools for MCP clients. |
| **Discord** | `--interface discord` | `wactorz[discord]` | Bot responds in channels and DMs. |
| **WhatsApp** | `--interface whatsapp` | `wactorz[whatsapp]` | Via Twilio Messaging. |
| **Telegram** | `--interface telegram` | `python-telegram-bot` | Bot API, polling mode. |
| **Web UI** | (always on) | — | Dashboard at `localhost:8888`. Disable with `--no-monitor`. |

> **💡 Multiple interfaces** — Only one chat interface can be active at a time (set via `--interface`), but the Web UI dashboard always runs alongside it unless disabled.

---

## CLI `[built-in]`

**Flag:** `--interface cli`

| | |
|---|---|
| **default** | yes |
| **streaming** | yes |
| **class** | `CLIInterface` |

The default interface. Starts an interactive terminal session where you type messages and receive streamed responses. Supports all Wactorz commands and `@agent-name` mentions.

```bash
wactorz
wactorz --interface cli --llm gemini
```

#### Special commands

```
/help                    — list available commands
/rules                   — show active pipeline rules
/rules delete <id>       — stop and remove a pipeline rule
/memory                  — show stored user facts
/memory clear            — wipe memory
/webhook discord <url>   — store a Discord webhook URL
@agent-name <payload>    — send directly to a named agent
```

---

## REST API `[built-in]`

**Flag:** `--interface rest`

| | |
|---|---|
| **default port** | `8000` |
| **class** | `RESTInterface` |
| **auth** | optional API key |

Exposes Wactorz as an HTTP API. Suitable for integrating with other services, running automated tests, or building custom frontends.

```bash
wactorz --interface rest --port 8000
```

#### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a message. Body: `{"message": "..."}`. Returns a buffered JSON response. |
| `GET` | `/agents` | List all registered agents with their status. |
| `GET` | `/health` | System health check. |
| `GET` | `/metrics` | Prometheus-format HTTP and actor metrics. |
| `GET` | `/ha-map` | Latest Home Assistant map snapshot, if available. |
| `GET` | `/actors` | Alias for `/agents`. |
| `GET` | `/actors/{actor_id}` | Get one actor's status payload. |
| `POST` | `/actors/{actor_id}/message` | Send a task payload to an actor. Body: `{"content": "..."}`. |
| `DELETE` | `/actors/{actor_id}` | Stop and unregister a non-protected actor. |
| `POST` | `/actors/{actor_id}/pause` | Pause a non-protected actor. |
| `POST` | `/actors/{actor_id}/resume` | Resume a paused non-protected actor. |
| `GET` | `/actors/{actor_id}/metrics` | Get one actor's metrics payload. |
| `POST` | `/agents/command` | Send `stop`, `pause`, or `resume` to a target actor. |

#### Authentication

Set `API_KEY` in your `.env` to require an API key on `/chat`:

```bash
API_KEY=my-secret-key
```

```bash
curl -X POST http://localhost:8000/chat \
  -H "X-API-Key: my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "turn off the lights"}'
```

---

## MCP Server `[requires extra]`

**Command:** `wactorz-mcp`

| | |
|---|---|
| **dep** | `pip install wactorz[mcp]` |
| **transport** | stdio |
| **class** | `FastMCP` server in `wactorz.interfaces.mcp_server` |
| **backend** | Wactorz REST API at `WACTORZ_URL` |

The MCP server exposes Wactorz as tools and resources for MCP-compatible clients such as Claude Desktop, Cursor, Zed, and the MCP Inspector. It is a separate process from the Wactorz runtime: start Wactorz with the REST interface first, then start the MCP server from your MCP client.

```bash
# Terminal 1: start Wactorz REST
wactorz --interface rest --port 8000

# Terminal 2: smoke-test MCP tool discovery
python -c "import asyncio, wactorz.interfaces.mcp_server as s; print([t.name for t in asyncio.run(s.mcp.list_tools())])"
```

If your editable install does not have a `wactorz-mcp` script yet, use the module form:

```bash
python -m wactorz.interfaces.mcp_server
```

#### Configuration

```env
WACTORZ_URL=http://localhost:8000
WACTORZ_API_KEY=              # optional; sent as X-API-Key to Wactorz REST
HA_URL=http://homeassistant.local:8123
HA_TOKEN=                     # optional; enables direct HA tools
```

#### Tools

| Tool | Description |
|---|---|
| `ask_wactorz(message)` | Send a message to the main orchestrator through `/chat`. |
| `ask_agent(agent_name, message)` | Send a message through `/chat` with `agent_name` included in the payload. |
| `list_agents()` | List currently registered agents from `/agents`. |
| `list_capabilities(keyword)` | Ask main for the running and spawnable capability catalog. |
| `stop_agent(agent_id)` | Stop and unregister a non-protected actor via REST. |
| `pause_agent(agent_id)` | Pause a non-protected actor. |
| `resume_agent(agent_id)` | Resume a paused actor. |
| `ha_list_entities(domain)` | List Home Assistant entities directly from HA REST. |
| `ha_get_state(entity_id)` | Read one Home Assistant entity state. |
| `ha_call_service(domain, service, entity_id, data_json)` | Call a Home Assistant service directly. |

#### Resources

| URI | Description |
|---|---|
| `wactorz://agents` | Current running agents as JSON. |
| `wactorz://capabilities` | Capability catalog from main. |
| `wactorz://ha-map` | Latest HA map snapshot from `/ha-map`. |
| `wactorz://config` | Sanitized MCP server configuration. |

#### MCP Inspector

```bash
npx @modelcontextprotocol/inspector python -m wactorz.interfaces.mcp_server
```

Run `list_agents`, `ask_wactorz` with `/agents`, and read `wactorz://config` first. Try `ha_list_entities` only after `HA_URL` and `HA_TOKEN` are configured and reachable.

#### Client config example

```json
{
  "mcpServers": {
    "wactorz": {
      "command": "python",
      "args": ["-m", "wactorz.interfaces.mcp_server"],
      "env": {
        "WACTORZ_URL": "http://localhost:8000"
      }
    }
  }
}
```

> **Security:** Treat MCP clients as privileged. The server can route prompts into Wactorz, manage agents, and, when configured, call Home Assistant services. Do not expose the stdio MCP server directly to the internet.

---

## Discord `[requires extra]`

**Flag:** `--interface discord`

| | |
|---|---|
| **dep** | `pip install wactorz[discord]` |
| **class** | `DiscordInterface` |

Runs Wactorz as a Discord bot. The bot responds to messages in any channel it has access to, and supports DMs. All Wactorz commands and `@agent-name` routing work identically to the CLI.

```bash
wactorz --interface discord --discord-token $DISCORD_BOT_TOKEN
```

#### Setup

1. Go to [discord.com/developers](https://discord.com/developers/applications) → New Application → Bot
2. Enable **Message Content Intent** under Bot → Privileged Gateway Intents
3. Copy the bot token and set it as `DISCORD_BOT_TOKEN` in your `.env`
4. Invite the bot to your server using the OAuth2 URL generator with `bot` scope and `Send Messages` + `Read Message History` permissions

#### Configuration

```bash
DISCORD_BOT_TOKEN=MTI4...
```

> **💡 Webhook notifications** — Pipelines can post to Discord independently of the bot using webhook URLs. Store a webhook with `/webhook discord https://discord.com/api/webhooks/...` and the planner will inject it into generated notification agents automatically.

---

## WhatsApp `[requires extra]`

**Flag:** `--interface whatsapp`

| | |
|---|---|
| **dep** | `pip install wactorz[whatsapp]` |
| **class** | `WhatsAppInterface` |
| **provider** | Twilio |

Connects Wactorz to WhatsApp via the Twilio Messaging API. Incoming WhatsApp messages are forwarded to MainActor and replies are sent back to the user's number.

```bash
wactorz --interface whatsapp
```

#### Setup

1. Create a [Twilio](https://www.twilio.com) account and enable the WhatsApp sandbox (or a production number)
2. Set the webhook URL in the Twilio console to your public endpoint (e.g. via ngrok during development): `https://your-host/whatsapp/incoming`
3. Add credentials to `.env`

#### Configuration

```bash
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
```

---

## Telegram `[requires package]`

**Flag:** `--interface telegram`

| | |
|---|---|
| **class** | `TelegramInterface` |
| **mode** | long polling |

Runs Wactorz as a Telegram bot using the Bot API in long-polling mode — no public webhook endpoint required. Works behind NAT and firewalls out of the box.

```bash
wactorz --interface telegram --telegram-token $TELEGRAM_BOT_TOKEN
```

#### Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the token and set it as `TELEGRAM_BOT_TOKEN` in your `.env`
3. Start Wactorz — the bot is immediately reachable in any chat or group it's added to

#### Configuration

```bash
TELEGRAM_BOT_TOKEN=1234567890:AAF...
TELEGRAM_ALLOWED_USER_ID=123456789   # optional: restrict to a single user ID
```

---

## Web UI Dashboard `[built-in]`

**URL:** `http://localhost:8888`

| | |
|---|---|
| **default port** | `8888` |
| **server** | `aiohttp` (`monitor_server.py`) |

The web dashboard starts automatically alongside whichever chat interface is active. It provides a real-time view of the running system and a chat interface accessible from any browser — no CLI needed.

#### Features

- **Agent status** — live heartbeat grid showing every registered actor, its state, and last-seen time
- **Log stream** — real-time log output from all agents via WebSocket
- **Chat** — full conversation interface, equivalent to the CLI
- **Docs** — this documentation, served at `/docs/`

```bash
# Change dashboard port
wactorz --monitor-port 9000

# Disable dashboard entirely
wactorz --no-monitor
```

---

## Adding a custom interface

All interfaces implement the same minimal pattern — call `process_user_input()` and stream or return the result. The simplest possible interface:

```python
class MyInterface:
    def __init__(self, main_actor):
        self.main = main_actor

    async def run(self):
        async for message in self._receive_messages():
            # Streaming response
            async for chunk in self.main.process_user_input_stream(message):
                await self._send(chunk)


# Register in cli.py alongside the other interfaces
elif interface == "my-interface":
    iface = MyInterface(main_actor)
    await asyncio.gather(iface.run(), system.run_forever())
```

Both `process_user_input(text)` (returns full reply string) and `process_user_input_stream(text)` (async generator of chunks) are available on MainActor.
