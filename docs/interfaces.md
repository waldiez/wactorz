# Interfaces

Wactorz supports multiple user-facing interfaces simultaneously. All of them funnel messages into the same `MainActor.process_user_input()` pipeline — switching interface only changes how you reach the system, not how it behaves.

## Overview

| Interface | Flag | Extra dep | Notes |
|-----------|------|-----------|-------|
| **CLI** | `--interface cli` | — | Default. Interactive terminal with streaming. |
| **REST** | `--interface rest` | — | HTTP API, suitable for programmatic access. |
| **Discord** | `--interface discord` | `wactorz[discord]` | Bot responds in channels and DMs. |
| **WhatsApp** | `--interface whatsapp` | `wactorz[whatsapp]` | Via Twilio Messaging. |
| **Telegram** | `--interface telegram` | — | Bot API, polling mode. |
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
| **default port** | `8080` |
| **class** | `RESTInterface` |
| **auth** | optional API key |

Exposes Wactorz as an HTTP API. Suitable for integrating with other services, running automated tests, or building custom frontends.

```bash
wactorz --interface rest --port 8080
```

#### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a message. Body: `{"message": "..."}`. Returns streamed or buffered response. |
| `GET` | `/agents` | List all registered agents with their status. |
| `GET` | `/health` | System health check. |

#### Authentication

Set `API_KEY` in your `.env` to require a bearer token on all requests:

```bash
API_KEY=my-secret-key
```

```bash
curl -X POST http://localhost:8080/chat \
  -H "Authorization: Bearer my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "turn off the lights"}'
```

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
DISCORD_PREFIX=!        # optional command prefix, default: none (mention or DM)
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

## Telegram `[built-in]`

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
TELEGRAM_ALLOWED_USERS=123456789,987654321   # optional: restrict to user IDs
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
