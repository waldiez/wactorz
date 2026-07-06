# Google agents — setup & login

The [Google Calendar](catalogue-google-calendar.html) and [Gmail](catalogue-gmail.html)
agents share one Google Cloud OAuth client and this one-time setup. Do it once, then see
each agent's page for usage.

## How it works (and why it just works)

Each agent talks to Google's **hosted MCP** server first (`calendarmcp.googleapis.com`,
`gmailmcp.googleapis.com`). Those endpoints are currently **access-gated** — even with a
valid, fully-scoped token they answer tool *executions* with `PERMISSION_DENIED` for
projects that aren't on Google's allowlist. So Wactorz transparently **falls back to the
plain Google REST API** (Calendar v3 / Gmail v1) using the *same* token:

```text
agent request → hosted MCP → PERMISSION_DENIED → REST API (same token) → result
```

You get working agents today with no extra steps; if Google later allowlists your project
for the hosted MCP, it takes over automatically.

## Prerequisites

- A Google account and a Google Cloud project (free tier is fine).
- Wactorz running locally — the login flow opens a browser on this machine.
- Optional: `pip install "wactorz[mcp]"` for the hosted-MCP path and `wactorz-mcp` tools.
  The REST fallback works without the `mcp` extra.

## 1. Create a Cloud project & enable APIs

In the [API Library](https://console.cloud.google.com/apis/library) (or via `gcloud`),
enable the APIs for the agents you want:

```bash
# Calendar
gcloud services enable calendar-json.googleapis.com calendarmcp.googleapis.com --project=PROJECT_ID
# Gmail
gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com --project=PROJECT_ID
```

Enabling `*mcp.googleapis.com` is harmless if it's gated — the REST fallback covers it.

## 2. Configure the OAuth consent screen

Under **APIs & Services → OAuth consent screen**:

- User type **External** is fine for personal use.
- **Add yourself as a Test user.** Gmail's `gmail.readonly` / `gmail.compose` are Google
  **restricted scopes** — in *Testing* mode a test user can use them without formal
  verification (capped at 100 users). Publishing to *Production* with restricted scopes
  triggers a mandatory security assessment, so **stay in Testing**.
- At consent you'll see an "unverified app" warning — click **Advanced → Go to … (unsafe)**.
  That's just because it's your own dev app.

## 3. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID → Web application.**
Add this exact **Authorized redirect URI** (both agents use it):

```text
http://localhost:8765/oauth/callback
```

Copy the **Client ID** and **Client secret** — one client can serve both agents.

## 4. Configure `.env`

Fill in the Google section at the bottom of `.env` (see `.env.template`):

```bash
# Calendar
CALENDAR_MCP_CLIENT_ID=...apps.googleusercontent.com
CALENDAR_MCP_CLIENT_SECRET=GOCSPX-...

# Gmail (reuse the same client, or a separate one)
GMAIL_MCP_CLIENT_ID=...apps.googleusercontent.com
GMAIL_MCP_CLIENT_SECRET=GOCSPX-...
```

URL, redirect URI, scopes and token-file paths all have sensible defaults — override them
via the commented keys in `.env.template` only if needed. Leaving a section blank disables
that agent. **Never commit `.env` or real secrets.**

## 5. Log in once

Run the one-time login. It opens a browser for consent and stores the token under
`~/.wactorz/` (reused on every run, refreshed silently):

```bash
wactorz-google-login          # both configured agents
wactorz-google-login calendar # just one
wactorz-google-login gmail
```

This mints a token with exactly the scopes Wactorz needs — notably **without**
`gmail.metadata`, which would otherwise block free-text mail search.

> Normal agent requests never pop a browser: they authenticate silently and, if a fresh
> consent were ever required, fail fast rather than block a server. That's why the initial
> login is this explicit one-time step.

## Troubleshooting

- **"The caller does not have permission"** — expected from the hosted MCP (it's gated); the
  REST fallback serves the request. Confirm your token works with `wactorz-google-login`.
- **"Access blocked: … has not completed the Google verification process"** — you're not a
  test user (or the app is Production+unverified with restricted scopes). Add yourself as a
  **Test user** and retry.
- **`bind on address ('127.0.0.1', 8765)`** — a previous login is still holding the callback
  port; close it (or kill the stray process) and retry.

## Security notes

- Tokens live in `~/.wactorz/*_token.json` (user-only permissions). Secrets come only from
  environment variables and are never sent to the dashboard or logged.
- Calendar events and email content can contain untrusted text — treat them as data and
  review any create/delete/draft actions.
