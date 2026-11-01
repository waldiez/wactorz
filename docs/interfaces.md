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

Every slash command main answers works here, and `@agent-name <message>` sends straight to
an agent; the [reference's command table](reference.md#13-interfaces) lists them. The
terminal also takes `/deploy-pkg <node> <package> …` and `quit` or `exit`, and its own
`/help` is a short summary.

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
| `GET` | `/health`, `/healthz`, `/livez` | Liveness: 200 whenever the process can answer. |
| `GET` | `/ready`, `/readyz` | Readiness: 200 once agents, broker and database are up, 503 naming what is not. |
| `GET` | `/metrics` | Prometheus-format HTTP and actor metrics. |
| `GET` | `/ha-map` | Latest Home Assistant map snapshot, if available. |
| `GET` | `/actors` | Alias for `/agents`. |
| `GET` | `/actors/{actor_id}` | Get one actor's status payload. |
| `POST` | `/actors/{actor_id}/message` | Send a task payload to an actor. Body: `{"content": "..."}`. |
| `DELETE` | `/actors/{actor_id}` | Stop and unregister a non-protected actor. |
| `GET` | `/actors/{actor_id}/metrics` | Get one actor's metrics payload. |
| `POST` | `/agents/command` | Send `start`, `stop` or `delete` to a target actor. |

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
| `ask_agent(agent_name, message)` | Send a message to one agent through `/chat`: main hands it over as an `@<name>` mention and returns that agent's reply. |
| `list_agents()` | List currently registered agents from `/agents`. |
| `list_capabilities(keyword)` | Ask main for the running and spawnable capability catalog. |
| `stop_agent(agent_id)` | Stop an actor via REST, leaving it registered so it can be started again. Refused for an essential actor. |
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

Runs Wactorz as a Discord bot. The bot replies when mentioned in a channel it has access to, and supports DMs.

Discord is a **restricted** channel: conversation, Home Assistant queries and everyday device control work, but spawning agents, deleting agents, running code, pipelines and admin slash commands do not. Use the dashboard for those.

```bash
wactorz --interface discord --discord-token $DISCORD_BOT_TOKEN
```

#### Setup

1. Go to [discord.com/developers](https://discord.com/developers/applications) → New Application → Bot
2. Enable **Message Content Intent** under Bot → Privileged Gateway Intents
3. Copy the bot token and set it as `DISCORD_BOT_TOKEN` in your `.env`
4. Enable **Developer Mode** (Settings → Advanced), right-click your name → **Copy User ID**, and set it as `DISCORD_ALLOWED_USER_IDS`
5. Invite the bot to your server using the OAuth2 URL generator with `bot` scope and `Send Messages` + `Read Message History` permissions

#### Configuration

```bash
DISCORD_BOT_TOKEN=MTI4...
DISCORD_ALLOWED_USER_IDS=123456789012345678   # required — comma-separate for several people
```

> **The allow-list is required.** Without it the bot logs an error and does not log in: anyone who could mention it would otherwise be able to control your devices and spend your LLM budget. Senders are also rate-limited (`SOCIAL_RATE_LIMIT_PER_MIN`, default 12/min).

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

Like Discord and Telegram this is a **restricted** channel — conversation, HA queries and everyday device control only.

```bash
wactorz --interface whatsapp
```

#### Setup

1. Create a [Twilio](https://www.twilio.com) account and enable the WhatsApp sandbox (or a production number)
2. Set the webhook URL in the Twilio console to your public endpoint (e.g. via ngrok during development): `https://your-host/whatsapp/incoming`
3. Add credentials and the sender allow-list to `.env`

#### Configuration

```bash
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token               # required — the webhook checks Twilio's signature with it
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
WHATSAPP_ALLOWED_NUMBERS=+306912345678        # required — comma-separate for several people
```

The webhook will not start without both. It answers Twilio at once and replies
to the message when the model has, so a slow answer is never delivered twice.

> **The allow-list is required.** The webhook is a public HTTP endpoint, so without it the interface refuses to start. Messages from other numbers are dropped before reaching the LLM.

---

## Telegram `[requires package]`

**Flag:** `--interface telegram`

| | |
|---|---|
| **class** | `TelegramInterface` |
| **mode** | long polling |

Runs Wactorz as a Telegram bot using the Bot API in long-polling mode — no public webhook endpoint required. Works behind NAT and firewalls out of the box.

Telegram is a **restricted** channel: conversation, Home Assistant queries and everyday device control only — no spawning, deleting, code, pipelines or admin commands.

```bash
wactorz --interface telegram --telegram-token $TELEGRAM_BOT_TOKEN
```

#### Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the token and set it as `TELEGRAM_BOT_TOKEN` in your `.env`
3. Start Wactorz and send `/start` to the bot. With no allow-list configured it runs in **setup mode** and answers only that one command, with your numeric user id
4. Put that id in `TELEGRAM_ALLOWED_USER_IDS` and restart — the bot is now reachable, by you

#### Configuration

```bash
TELEGRAM_BOT_TOKEN=1234567890:AAF...
TELEGRAM_ALLOWED_USER_IDS=123456789   # required — comma-separate for a household
```

> **The allow-list is required.** Telegram bots are publicly discoverable by username, so an unrestricted bot is an open door to your home and your LLM budget. Until it is set the bot stays in setup mode. `TELEGRAM_ALLOWED_USER_ID` (singular) is still accepted and folded into the list.

---

## Web UI Dashboard `[built-in]`

**URL:** `http://localhost:8888`

| | |
|---|---|
| **default port** | `8888` |
| **server** | `aiohttp` (`wactorz/web/`) |

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

## Messages from an agent, and the answer back

The channels above answer a person who writes first. An agent can also write first: say something to the operator outside any turn, and offer a choice.

```python
message_id = await me.notify_user(
    "Every reading since 14:02 looks like an anomaly. Did something change?",
    options=["maintenance", "real problem", "not sure"],
    channel="telegram",
    user="123456789",
)
```

- `channel` is where to send it: `dashboard` (the default), `telegram`, `discord` or `whatsapp`.
- `user` is who to reach there: a Telegram or Discord user id, a WhatsApp number. The dashboard has one operator and needs none.
- `options` is the choice. Telegram and the dashboard show buttons; Discord and WhatsApp show a numbered list, and the person replies with the number or the option's words. Up to ten options of a few words each.

The message goes on the agent's own `agents/{id}/chat` topic, wherever the agent runs, so the dashboard shows it whatever channel it names, as a card with the buttons and a note of where else it went. The channel delivers it if it is running; one that is not, or one that cannot reach the person, logs the message's words rather than losing them.

The pick comes back on `agents/{id}/answer`, from whichever channel the person answered on, as `{"id": <the message id>, "option": "maintenance", "index": 0, "channel": "telegram"}`. An agent that wants it subscribes to its own topic:

```python
class HvacWatch(wactorz.Actor):
    async def on_start(self):
        self.subscribe(f"agents/{self.actor_id}/answer", self.on_answer)

    async def on_answer(self, answer: dict) -> None:
        self.persist("last_answer", answer["option"])
```

A generated agent has the same call, `await agent.notify_user(...)`, and `agent.subscribe("agents/<its id>/answer", callback)`. Every open dashboard marks the question answered once any channel has the pick, and the pick is a row on the activity feed.

### Asking and waiting: `ask_operator`

Most of the time the agent wants the answer, not the topic. `ask_operator` sends the same message and waits for the pick:

```python
answer = await me.ask_operator(
    "Every reading since 14:02 looks like an anomaly. Did something change?",
    options=["maintenance", "real problem", "not sure"],
    channel="telegram",
    user="123456789",
    timeout=3600,
)
if answer == "maintenance":
    me.persist("regime_change_at", "14:02")
```

- The question is written into the agent's state until it is answered, so a restart in between loses nothing. When the agent starts it listens again, and a pick that arrives while nothing waits for it reaches `on_operator_answer(question, option)` on an `Actor`, or `async def on_answer(agent, question, option)` in a generated agent's program. `me.open_questions()` lists what is still unanswered, and `await me.wait_for_answer(question_id)` waits for one of them by id.
- A pick may come with a note, what the person added in their own words; it is kept as `question.answer_note`, empty when there is none.
- `context="..."` says why the question is being asked: what the agent saw, what the answer will change. It is kept in the book and shown with the question in the dashboard's Inbox; chat channels are sent the question alone. It is redacted and cut to a thousand characters.
- The agent publishes its questions, retained, on `agents/{id}/questions` whenever one is asked or answered, and again when it starts. The monitor's `GET /api/questions` lists them from there, so one asked while the monitor was down is not missed.
- The first pick on any channel is the answer. The dashboard shows every question whatever channel it went to, in its Inbox (`#/inbox`), so the operator can answer there as well, with a note if they want; a question sent with `notify_user` alone keeps no book, so it shows as a notice and in the agent's thread but not in the Inbox.
- The answer is not authenticated. It arrives on the agent's `agents/{id}/answer` topic, and anything that can publish to the broker, any node included, can publish one there. Read it as what someone picked, never as proof that the operator did, and do not make it the approval for an action you would not let the agent take unasked.
- `timeout` is seconds; `None` waits for as long as the agent runs. When it passes on a question sent to a chat channel, the question is shown again on the dashboard, saying where it was asked, and waited for once more for the same time. Then `asyncio.TimeoutError` is raised, and the question stays open: a later pick still reaches `on_operator_answer`.

#### Answers that become labels

Pass `label=` and the answer becomes a label: a record that a source was doing something over a period, which models train on, the controller reads and a simulator scores against.

```python
answer = await me.ask_operator(
    "Every reading on AHU-1 looks unusual since 14:02. Did something change?",
    options=["Maintenance", "Real problem", "Not sure"],
    label={
        "source": "sensors/ahu-1",          # the topic or entity it is about
        "start": shift_ts,                  # event time, Unix seconds or ISO 8601 with an offset
        "values": {"Maintenance": "maintenance", "Real problem": "fault"},
    },
)
```

- `values` maps an option to the label value it stands for; an option left out, such as "Not sure", makes no label. `end` closes the window; left out, it stays open, "until further notice". `model` names the model a label is meant for, if any.
- The spec is checked before the question is sent, and a spec that cannot be used raises a `ValueError` (`LabelError`) without sending anything.
- Main writes the label once the agent's question book says the question was answered, for an agent on main or on a node. It takes a book only from where its agent is, as it does a manifest.
- A later label on the same source overrides an earlier one where their windows overlap. The earlier one is kept, marked superseded; `GET /api/labels/effective` says what held when.
- A label from an answer the dashboard sent is marked verified; one from an answer heard on the broker is not, since anything there could have published it.
- An agent on main can also write a label of its own: `await me.label("sensors/ahu-1", "maintenance", start=ts)`. On a node, use a question.

See `docs/design/labels-and-provenance.md` for the design, and `GET /api/labels` in the API reference.

A rule can ask without any code, with the `ask_operator` action:

```python
RuleConfig(
    triggers=("anomalies/hvac",),
    conditions=(RuleCondition("score", "gt", 20),),
    actions=(
        RuleAction(
            "ask_operator",
            text="HVAC score {score} since {since}. Did something change?",
            options=("maintenance", "real problem", "not sure"),
            channel="telegram",
            user="123456789",
            timeout=3600,
            topic="labels/hvac",
            context="Above 20 for ten minutes on {sensor}.",
        ),
    ),
    cooldown_seconds=3600,
)
```

The text and `context` may name trigger fields in braces, as a payload may. The rule keeps evaluating while the person thinks; when they pick, `{"question", "option", "index", "trigger"}` is published on `topic`, or on `answers/<rule name>` when none is given, which is where a label store or a controller listens.

A rule can also record a label itself, with the `label` action: `{"type": "label", "source": "sensors/{entity_id}", "value": "maintenance", "text": "maintenance switch on"}` records that the source was under maintenance from when the trigger says it happened (its `ts` or `timestamp`, else now), as the rule's own label. `source` and `value` may name trigger fields in braces, and `text` is kept as the label's note.

Two things a channel cannot do:

- **WhatsApp lets a business write first only inside a day of the person's last message**, or through a template Meta approved. Outside that window Twilio refuses the send, and the refusal is logged with the message's words. Telegram and the dashboard have no such limit.
- **A question offered before a restart is answered on the dashboard, not by number.** Discord and WhatsApp match a bare "2" to the latest question they sent that person, and keep that in memory; after a restart the reply reaches the model as a message like any other. The dashboard card still answers it.

A chat channel object also has the call directly, `await interface.send(user, text, options)`, for a host program that holds one.

---

## Adding a custom interface

All interfaces implement the same minimal pattern — hand the message to the orchestrator, saying which channel it came in on, and stream or return the answer. The orchestrator is main unless the deployment installed another (see *Your own orchestrator* in the library guide), so an interface never names main. The simplest possible interface:

```python
from wactorz.orchestration import SOCIAL


class MyInterface:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def run(self):
        async for sender, message in self._receive_messages():
            # A public channel: the orchestrator answers with the restrictions
            # that go with one. "dashboard", "cli" and "rest" are the operator's own.
            async for chunk in self.orchestrator.handle_turn_stream(
                message, channel=SOCIAL, user=sender
            ):
                await self._send(chunk)


# Register in app.py alongside the other interfaces
elif interface == "my-interface":
    iface = MyInterface(orchestrator)
    await asyncio.gather(iface.run(), system.run_forever())
```

Both `process_user_input(text)` (returns full reply string) and `process_user_input_stream(text)` (async generator of chunks) are available on MainActor.
