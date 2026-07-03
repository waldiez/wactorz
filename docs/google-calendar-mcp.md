# Google Calendar MCP

> **Setting it up?** See the step-by-step **[Google Calendar & Gmail agents guide](google-agents.md)**
> (Cloud project, OAuth client, `wactorz-google-login`, example prompts). This page is the
> Calendar MCP reference.

Wactorz uses Google's hosted Calendar MCP server for Google Calendar access. The catalogue-backed `google-calendar-agent` calls MCP tools such as `list_events`, `create_event`, and `delete_event`. Google's hosted MCP is currently access-gated and denies tool execution for non-allowlisted projects, so Wactorz automatically falls back to the Google Calendar REST API (v3) using the same OAuth token — the agent keeps working, and the hosted MCP resumes automatically if your project is later granted access.

Google's hosted MCP endpoint is:

```bash
https://calendarmcp.googleapis.com/mcp/v1
```

## Google Cloud setup

In your Google Cloud project, enable both services:

```bash
gcloud services enable calendar-json.googleapis.com --project=PROJECT_ID
gcloud services enable calendarmcp.googleapis.com --project=PROJECT_ID
```

Then configure the OAuth consent screen and create an OAuth 2.0 Web application client. Add the redirect URI used by Wactorz:

```text
http://localhost:8765/oauth/callback
```

## Wactorz environment

Set these values before starting Wactorz or `wactorz-mcp`:

```bash
export CALENDAR_MCP_URL="https://calendarmcp.googleapis.com/mcp/v1"
export CALENDAR_MCP_CLIENT_ID="...apps.googleusercontent.com"
export CALENDAR_MCP_CLIENT_SECRET="..."
export CALENDAR_MCP_REDIRECT_URI="http://localhost:8765/oauth/callback"
export CALENDAR_MCP_TOKEN_FILE="$HOME/.wactorz/calendar_mcp_token.json"
export CALENDAR_MCP_TIMEZONE="Europe/Athens"
```

`CALENDAR_MCP_URL` defaults to Google's hosted endpoint, so you can omit it unless you want to be explicit.

Default scopes:

```text
https://www.googleapis.com/auth/calendar.calendarlist.readonly
https://www.googleapis.com/auth/calendar.events
https://www.googleapis.com/auth/calendar.events.freebusy
https://www.googleapis.com/auth/calendar.events.readonly
```

Override with `CALENDAR_MCP_SCOPES` only if your OAuth app uses a narrower or different scope set.

## Wactorz agent usage

Start Wactorz normally. The catalogue advertises `google-calendar-agent` as a spawnable recipe with capabilities such as `google_calendar`, `calendar`, `events`, and `create_event`. The first normal calendar request can auto-spawn it, or you can start it explicitly:

```text
@catalog spawn google-calendar-agent
```

Example user requests:

```text
what is on my calendar today?
show my calendar this week
create a calendar event called Dentist tomorrow from 15:00 to 16:00
```

For event creation, the agent resolves natural language into Google's MCP `create_event` arguments. Structured calls should use `operation=create_event`, `summary`, `start`, and `end`; the MCP server requires both `startTime` and `endTime`.

## Wactorz MCP tools

Install Wactorz with the MCP extra and run the MCP server:

```bash
pip install "wactorz[mcp]"
wactorz-mcp
```

The Wactorz MCP server registers convenience tools backed by Google Calendar MCP:

- `calendar_status` - sanitized Calendar MCP configuration status.
- `calendar_list` - calls Google MCP `list_events`.
- `calendar_today` - calls Google MCP `list_events` with today's time window.
- `calendar_week` - calls Google MCP `list_events` with the next seven days.
- `calendar_create_event` - calls Google MCP `create_event`.
- `calendar_delete_event` - calls Google MCP `delete_event`.
- `calendar_mcp_list_tools` - lists tools exposed by Google's Calendar MCP server.
- `calendar_mcp_call_tool` - calls any Calendar MCP tool with a JSON object string.

## Sanitized config resource

`wactorz://config` includes whether Calendar MCP is configured, but it never exposes tokens or client secrets.

## Security note

Calendar events can contain untrusted text. Treat event content as data, review creates/updates/deletes carefully, and connect only trusted MCP clients to your Google Calendar account.