# Google Calendar agent

`google-calendar-agent` — read and manage your Google Calendar: list today's/upcoming
events, create events, and delete events.

> **Setup:** one-time OAuth — see **[Google agents — setup & login](catalogue-google-setup.html)**.

## Spawn

```text
@catalog spawn google-calendar-agent
```

The first calendar request can also auto-spawn it.

## Usage

Talk to it in plain language:

```text
what's on my calendar today?
show my events this week
list my upcoming events
create an event called Dentist tomorrow from 3pm to 4pm
delete event <event_id>
```

Times resolve against your local zone. Set `CALENDAR_MCP_TIMEZONE` (IANA name, e.g.
`Europe/Athens`) to override; otherwise it uses `TZ` / the system zone. Upcoming-event
listings are bounded to the next year so a yearly recurring event (a birthday) shows once
rather than flooding the list; cross-year dates include the year.

## Structured operations

For programmatic / A2A calls, send `operation` instead of free text:

| Field | Meaning |
|-------|---------|
| `operation` | `status`, `list_events`, `today`, `tomorrow`, `week`, `create_event`, `delete_event` |
| `summary` | event title (for `create_event`) |
| `start`, `end` | ISO-8601 datetimes (for `create_event`; both required) |
| `event_id` | event id (for `delete_event`) |

Returns `result` (human-readable), plus `events` for list ops and `event` for a create.

## Wactorz MCP tools

With the `mcp` extra installed, `wactorz-mcp` exposes convenience tools backed by this
agent: `calendar_status`, `calendar_list`, `calendar_today`, `calendar_week`,
`calendar_create_event`, `calendar_delete_event`, plus `calendar_mcp_list_tools` /
`calendar_mcp_call_tool` for the raw hosted MCP. The sanitized `wactorz://config` resource
reports whether Calendar is configured but never exposes tokens.

## Troubleshooting

- **"The caller does not have permission"** — expected from the hosted MCP; the REST
  fallback serves it. See the [setup page](catalogue-google-setup.html#troubleshooting).
- **Wrong times** — set `CALENDAR_MCP_TIMEZONE` to your IANA zone.
- **Auth errors after a while** — re-run `wactorz-google-login calendar`.
