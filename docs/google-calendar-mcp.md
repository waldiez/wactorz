# Google Calendar MCP Proxy

Wactorz can expose a configured Google Calendar MCP server through its own MCP server. This keeps Google integration at the MCP boundary: Wactorz does not add native Google agents or require the Google API client libraries for this path.

## Enable the proxy

Install Wactorz with the MCP extra and run the MCP server as usual:

```bash
pip install "wactorz[mcp]"
wactorz-mcp
```

Configure the remote Calendar MCP endpoint with environment variables:

```bash
export CALENDAR_MCP_URL="https://your-calendar-mcp.example/mcp"
```

For bearer-style auth, set one of:

```bash
export CALENDAR_MCP_TOKEN="..."
# or, for a complete header value:
export CALENDAR_MCP_AUTHORIZATION="Bearer ..."
```

For OAuth-capable Calendar MCP servers, set the client credentials and optional callback settings:

```bash
export CALENDAR_MCP_CLIENT_ID="...apps.googleusercontent.com"
export CALENDAR_MCP_CLIENT_SECRET="..."
export CALENDAR_MCP_REDIRECT_URI="http://localhost:8765/oauth/callback"
export CALENDAR_MCP_TOKEN_FILE="$HOME/.wactorz/calendar_mcp_token.json"
```

The token file is written with user-only permissions when the platform allows it.

## Exposed tools

The Wactorz MCP server registers these Calendar proxy tools:

- `calendar_status` - sanitized configuration status.
- `calendar_mcp_list_tools` - list tools exposed by the remote MCP server.
- `calendar_mcp_call_tool` - call any remote tool with a JSON object string.
- `calendar_list` - convenience wrapper for `list_events`.
- `calendar_today` - convenience wrapper for today's events.
- `calendar_week` - convenience wrapper for this week's events.
- `calendar_create_event` - convenience wrapper for `create_event`.
- `calendar_delete_event` - convenience wrapper for `delete_event`.

The convenience wrappers assume common remote tool names such as `list_events`, `create_event`, and `delete_event`. If your Calendar MCP server uses different names or schemas, use `calendar_mcp_list_tools` and `calendar_mcp_call_tool` directly.

## Sanitized config resource

`wactorz://config` includes whether Calendar MCP is configured and whether any auth path is enabled, but it never exposes tokens or client secrets.
