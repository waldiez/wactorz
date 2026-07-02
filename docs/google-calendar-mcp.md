# Google Calendar

Wactorz can access Google Calendar through the catalog-backed `google-calendar-agent`. The MCP server exposes the same calendar operations for MCP clients, and can still proxy a separate remote Calendar MCP server when `CALENDAR_MCP_URL` is configured.

## Native Google Calendar setup

For read and write access, configure OAuth credentials that can refresh an access token:

```bash
export GOOGLE_CALENDAR_CLIENT_ID="...apps.googleusercontent.com"
export GOOGLE_CALENDAR_CLIENT_SECRET="..."
export GOOGLE_CALENDAR_REFRESH_TOKEN="..."
export GOOGLE_CALENDAR_ID="primary"
export GOOGLE_CALENDAR_TIMEZONE="Europe/Athens"
```

For short manual tests, you can set an access token instead of refresh-token credentials:

```bash
export GOOGLE_CALENDAR_ACCESS_TOKEN="..."
```

Optional safety switch:

```bash
export GOOGLE_CALENDAR_READONLY="1"
```

Read-only mode allows listing events but blocks `create_event` and `delete_event`.

## Wactorz agent usage

Start Wactorz normally. The catalog advertises `google-calendar-agent` as a spawnable recipe with capabilities such as `google_calendar`, `calendar`, `events`, and `create_event`. The first normal calendar request can auto-spawn it, or you can start it explicitly with `@catalog spawn google-calendar-agent`.

Example user requests:

```text
what is on my calendar today?
show my calendar this week
create a calendar event called Dentist tomorrow at 15:00
```

For event creation, the spawned agent uses the configured LLM to resolve natural-language dates into ISO-8601 datetimes. Structured calls can bypass parsing by sending `operation=create_event`, `summary`, `start`, and optional `end`, `location`, or `description`.

## MCP tools

Install Wactorz with the MCP extra and run the MCP server as usual:

```bash
pip install "wactorz[mcp]"
wactorz-mcp
```

The Wactorz MCP server registers these Calendar tools:

- `calendar_status` - sanitized Google Calendar and remote MCP configuration status.
- `calendar_list` - list upcoming Google Calendar events.
- `calendar_today` - list today's Google Calendar events.
- `calendar_week` - list this week's Google Calendar events.
- `calendar_create_event` - create a Google Calendar event.
- `calendar_delete_event` - delete a Google Calendar event.
- `calendar_mcp_list_tools` - optional: list tools exposed by a remote Calendar MCP server.
- `calendar_mcp_call_tool` - optional: call any remote Calendar MCP tool with a JSON object string.

When native Google Calendar auth is configured, `calendar_list`, `calendar_today`, `calendar_week`, `calendar_create_event`, and `calendar_delete_event` use Google Calendar directly. If native auth is not configured, those tools fall back to the remote MCP proxy path.

## Optional remote Calendar MCP proxy

Configure a remote Calendar MCP endpoint only if you already run a separate Calendar MCP server:

```bash
export CALENDAR_MCP_URL="https://your-calendar-mcp.example/mcp"
```

For bearer-style auth, set one of:

```bash
export CALENDAR_MCP_TOKEN="..."
# or, for a complete header value:
export CALENDAR_MCP_AUTHORIZATION="Bearer ..."
```

For OAuth-capable remote MCP servers, set the client credentials and optional callback settings:

```bash
export CALENDAR_MCP_CLIENT_ID="...apps.googleusercontent.com"
export CALENDAR_MCP_CLIENT_SECRET="..."
export CALENDAR_MCP_REDIRECT_URI="http://localhost:8765/oauth/callback"
export CALENDAR_MCP_TOKEN_FILE="$HOME/.wactorz/calendar_mcp_token.json"
```

The remote MCP token file is written with user-only permissions when the platform allows it.

## Sanitized config resource

`wactorz://config` includes whether Google Calendar and remote Calendar MCP are configured, but it never exposes tokens, refresh tokens, or client secrets.
