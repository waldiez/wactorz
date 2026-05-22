# External Integrations (Weather, Calendar, Gmail)

This page covers the three end-user integrations added in `feat/newmcp`:
`WeatherAgent`, `CalendarAgent`, and `GmailAgent`. It also documents the
larger architectural question they raise - how Wactorz should consume
external capabilities long-term - so future contributors have context
when choosing how to add the next one.

## What ships

| Agent           | Underlying service     | Auth                | Optional dep group   |
|-----------------|------------------------|---------------------|----------------------|
| `weather-agent` | Open-Meteo HTTP API    | None (no key)       | none (core `aiohttp`)|
| `calendar-agent`| Google Calendar API v3 | Google OAuth (user) | `wactorz[google-api]`|
| `gmail-agent`   | Gmail API v1           | Google OAuth (user) | `wactorz[google-api]`|

All three are regular `Actor` subclasses that publish a capability manifest
on start, so the orchestrator (`MainActor`) can discover them via
`agent.capabilities()` and delegate user intents to them without code
changes elsewhere.

## Quick start

```bash
# Install the Google extras (only needed for Gmail/Calendar)
pip install "wactorz[google-api]"

# Run as normal - WeatherAgent works immediately
wactorz
```

In the chat UI:

```
@weather-agent current Berlin
@weather-agent forecast Athens 5
@calendar-agent today
@gmail-agent unread
```

## Long-term design: how should Wactorz consume external capabilities?

The user-visible question - "what MCPs would be useful?" - hides a deeper
architectural choice. There are three ways Wactorz could plug into
third-party services. The agents on this branch use option A; the others
are documented here so we can move when the trade-offs change.

### Option A - One Actor per service (what this branch does)

Each integration is a hand-rolled `Actor` that talks to the upstream
service directly (HTTP, official SDK, etc.).

**Pros**
- Matches every other agent in the repo (`fuseki_agent`, `manual_agent`,
  `home_assistant_agent`). Zero new infrastructure.
- Manifests give the planner exact input/output schemas, which is what
  it needs to route intents.
- Works offline-first - `WeatherAgent` has no external broker dependency
  beyond the upstream API.
- Easy to read - one file per service.

**Cons**
- Each new service is hand-written code we have to maintain.
- No reuse of the growing ecosystem of MCP servers.

### Option B - Generic MCP client agent

A single `mcp_client_agent.py` that spawns child connections to any
MCP-compatible server (stdio or HTTP transport) and exposes the server's
tools as Wactorz capabilities. The user configures servers via env or
a config file.

**Pros**
- Adding a new service becomes a config change, not a code change.
- Wactorz already exposes itself as an MCP server
  ([wactorz/interfaces/mcp_server.py](../wactorz/interfaces/mcp_server.py)) -
  symmetry with the consumer side is nice.
- Inherits the security/sandbox story upstream MCP servers ship with.

**Cons**
- Each upstream server still has its own auth ceremony (Google OAuth for
  Gmail, etc.) - the MCP layer doesn't simplify that part, it just moves it.
- Tool descriptions from MCP servers are not always rich enough for the
  planner; we'd need a wrapping layer.
- Spawning subprocesses on edge hardware (Raspberry Pi) is more
  expensive than a single in-process actor.

### Option C - Direct tool calls from MainActor

Skip the actor boundary entirely and register Weather/Calendar/Gmail as
LLM tools directly on `MainActor`.

**Pros**
- Lowest latency, no message passing.

**Cons**
- Couples the orchestrator to every integration. Hard to reason about
  failure modes, hard to test in isolation, breaks the "everything is
  an actor" invariant from CLAUDE.md.
- No supervision, no manifest, no MQTT visibility on the dashboard.

### Recommendation

**Stay on Option A for now.** It is the cheapest in code complexity, fits
the existing patterns, and the dashboard already understands actor
manifests. Move to Option B when either (a) we have three or more
integrations that all need maintenance updates we keep forgetting, or
(b) a user asks for a service that already has a high-quality community
MCP server (Notion, Slack, GitHub) where re-implementing the client
would clearly be wasted effort.

If we adopt Option B later, the agents on this branch don't need to be
deleted - they remain the offline-capable, no-external-process versions.
A `McpClientAgent` would sit alongside them, not replace them.

## Google OAuth setup (Calendar + Gmail)

The Google APIs require OAuth - there is no API-key shortcut for user
mailboxes or calendars. Wactorz handles the flow with the standard
`google-auth-oauthlib` installed-app flow.

### Step 1 - create OAuth client credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
2. Pick or create a project.
3. Enable the **Gmail API** and **Google Calendar API** for the project.
4. Go to **Credentials → Create credentials → OAuth client ID**.
5. Application type: **Desktop app**. Give it any name.
6. Download the JSON.

If your project is still in "Testing" mode, add your Google account as
a test user (**OAuth consent screen → Test users**), otherwise the
flow will reject the login.

### Step 2 - point Wactorz at the JSON

```bash
# Default location - Wactorz looks here automatically
mkdir -p ~/.wactorz
mv ~/Downloads/client_secret_*.json ~/.wactorz/google_client.json

# Or override via env
export GOOGLE_OAUTH_CLIENT_JSON=/path/to/your/client.json
export GOOGLE_OAUTH_TOKEN_JSON=/path/to/where/refresh-token/lives.json
```

### Step 3 - first run

The first time you invoke `@calendar-agent` or `@gmail-agent`, a browser
window opens, you grant access, and the refresh token is written to
`~/.wactorz/google_token.json` (mode 0600). After that, both agents run
silently across restarts. Tokens refresh automatically until you revoke
access in your Google Account settings.

### Headless servers (Raspberry Pi, Docker, remote nodes)

The OAuth flow needs a browser. On a headless host:

1. Run the agent **once on a workstation** with a browser. Complete the
   consent flow.
2. Copy `~/.wactorz/google_token.json` to the headless host (same path,
   or `GOOGLE_OAUTH_TOKEN_JSON` env).
3. Set `GOOGLE_OAUTH_NONINTERACTIVE=1` on the headless host so the agent
   refuses to attempt a browser flow and fails fast instead.

### Security notes

| Concern | Mitigation |
|---|---|
| Refresh token at rest | Stored 0600 in `~/.wactorz/google_token.json`. Treat the file like an SSH private key. |
| Scopes | Calendar full read/write + Gmail read+send. Wider than the minimum some users want. To narrow, edit `SCOPES` in [_google_oauth.py](../wactorz/agents/_google_oauth.py) and delete the token to re-consent. |
| Revoking access | https://myaccount.google.com/permissions - revoke from there, delete the local token file, done. |
| Multi-user | Wactorz is single-user per install. If you need per-user tokens, run separate Wactorz instances or extend the helper to key the token path by user id. |
| Network egress | Both agents call `googleapis.com` directly. If the host runs in an air-gapped network, these agents will not work and `WeatherAgent` will not work either - all three need outbound HTTPS. |

## Weather agent

Open-Meteo is free, no signup, no key. It also exposes a geocoding endpoint
which the agent uses to turn `"Athens"` into `(lat, lon)` before fetching
forecasts. The agent caches geocoding results in memory per process to
avoid repeating the lookup.

### Commands

```
current [location]           # "current Berlin" - falls back to default
forecast [location] [days]   # "forecast London 5"
set-default <location>       # persisted across restarts
```

The default location comes from `WEATHER_DEFAULT_LOCATION` (default:
`London`). Once you call `set-default`, the new value is stored in the
agent's persistence layer and used on every subsequent restart.

## Calendar agent

```
list [N]                                  # next N upcoming events
today                                     # today's remaining events
week                                      # next 7 days
create "<title>" <start> [end]            # ISO-8601 datetimes
delete <event_id>                         # IDs come back in list output
status                                    # OAuth diagnostics
```

## Gmail agent

```
recent [N]                                # last N inbox messages
unread                                    # is:unread shortcut
search <gmail-query>                      # full Gmail search syntax
read <message_id>                         # full body (plain text preferred)
send <to> "<subject>" "<body>"            # plain-text only
status                                    # OAuth diagnostics
```

Gmail search syntax: https://support.google.com/mail/answer/7190

## Composing with other agents

Because each integration publishes a manifest, the planner can route
intents to them without code changes:

```
User: every morning at 7am, if it's going to rain today, email me a reminder
```

The planner sees `weather.forecast` (from `weather-agent`) and `gmail.send`
(from `gmail-agent`), produces a pipeline with `scheduled-agent` firing at
07:00 and a dynamic agent gluing the two together, and persists it via the
existing pipeline rules table. No new framework code is required - that's
the point of the manifest-driven discovery model.
