# Google Calendar & Gmail agents

Wactorz ships two catalog-backed native agents that connect to your Google account:

- **`google-calendar-agent`** — list today/upcoming events, create events, delete events.
- **`gmail-agent`** — search and read mail, list labels and drafts, and create drafts.
  It is **draft-first**: it never sends email, only prepares drafts for you to send.

This is a single guide because both agents share one OAuth client and the same setup.

## How it works (and why it just works)

Each agent talks to Google's **hosted MCP** server first
(`calendarmcp.googleapis.com`, `gmailmcp.googleapis.com`). Those endpoints are
currently **access-gated** — even with a valid, fully-scoped token they answer tool
*executions* with `PERMISSION_DENIED` for projects that aren't on Google's allowlist.

So Wactorz transparently **falls back to the plain Google REST API** (Calendar v3 /
Gmail v1) using the *same* OAuth token. You get working agents today; if Google later
allowlists your project for the hosted MCP, it takes over automatically with no changes.
You don't need to do anything to enable the fallback — it's the default.

```
agent request → hosted MCP → PERMISSION_DENIED → REST API (same token) → result
```

## Prerequisites

- A Google account and a Google Cloud project (free tier is fine).
- Wactorz running locally (the login flow opens a browser on this machine).
- Optional: `pip install "wactorz[mcp]"` if you want the hosted-MCP path and the
  `wactorz-mcp` tools. The REST fallback works without the `mcp` extra.

## 1. Create a Google Cloud project & enable APIs

Pick or create a project, then enable the APIs for whichever agents you want. In the
[API Library](https://console.cloud.google.com/apis/library) (or via `gcloud`):

```bash
# Calendar
gcloud services enable calendar-json.googleapis.com calendarmcp.googleapis.com --project=PROJECT_ID
# Gmail
gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com --project=PROJECT_ID
```

Enabling `*mcp.googleapis.com` is harmless if it's gated — the REST fallback covers it.

## 2. Configure the OAuth consent screen

Under **APIs & Services → OAuth consent screen** (a.k.a. *Google Auth Platform*):

- User type **External** is fine for personal use.
- **Add yourself as a Test user.** Gmail's `gmail.readonly` / `gmail.compose` are
  Google **restricted scopes** — in *Testing* mode a test user can use them without
  Google's formal verification (capped at 100 users). Publishing to *Production* with
  restricted scopes triggers a mandatory security assessment, so **stay in Testing**.
- At consent you'll see an "unverified app" warning — click **Advanced → Go to … (unsafe)**.
  That's just because it's your own dev app.

## 3. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID → Web application.**
Add this exact **Authorized redirect URI** (both agents use it):

```text
http://localhost:8765/oauth/callback
```

Copy the **Client ID** and **Client secret**. One client can serve both agents.

## 4. Configure Wactorz

Copy the template and fill in the Google section at the bottom
(`cp .env.template .env`). Minimum needed:

```bash
# Calendar
CALENDAR_MCP_CLIENT_ID=...apps.googleusercontent.com
CALENDAR_MCP_CLIENT_SECRET=GOCSPX-...

# Gmail (reuse the same client, or a separate one)
GMAIL_MCP_CLIENT_ID=...apps.googleusercontent.com
GMAIL_MCP_CLIENT_SECRET=GOCSPX-...
```

URL, redirect URI, scopes and token-file paths all have sensible defaults — see the
commented lines in `.env.template` if you need to override them. Leaving a section blank
disables that agent. **Never commit `.env` or real secrets.**

## 5. Log in once

Run the one-time login. It opens a browser for consent and stores the token under
`~/.wactorz/` (reused on every later run, and refreshed silently):

```bash
wactorz-google-login          # both configured agents
wactorz-google-login calendar # just one
wactorz-google-login gmail
```

Sign in as your account, click through the unverified-app warning, and you're done.
This mints a token with exactly the scopes Wactorz needs — notably **without**
`gmail.metadata`, which would otherwise block free-text mail search.

> Normal agent requests never pop a browser: they authenticate silently and, if a fresh
> consent were ever required, fail fast rather than block a server. That's why the initial
> login is this explicit one-time step.

## 6. Use the agents

Start Wactorz. The catalog advertises both agents; the first relevant request can
auto-spawn one, or spawn it explicitly:

```text
@catalog spawn google-calendar-agent
@catalog spawn gmail-agent
```

**Calendar prompts**

```text
what's on my calendar today?
show my events this week
list my upcoming events
create an event called Dentist tomorrow from 3pm to 4pm
```

**Gmail prompts**

```text
any unread email?
show my inbox
emails from stripe
find invoices
what does the trello one say?           # opens & reads that email
content of the latest vodafone bill     # with an LLM, answers "what do I owe?"
list my labels
draft an email to sam@example.com about lunch
```

With an LLM configured, `read` returns a concise answer to your question (quoting exact
amounts/dates) instead of dumping the raw email.

## Wactorz MCP tools (optional)

With the `mcp` extra installed, `wactorz-mcp` exposes convenience tools backed by the
Calendar agent: `calendar_status`, `calendar_list`, `calendar_today`, `calendar_week`,
`calendar_create_event`, `calendar_delete_event`, plus `calendar_mcp_list_tools` /
`calendar_mcp_call_tool` for the raw hosted MCP. The sanitized `wactorz://config`
resource reports whether Calendar/Gmail are configured but never exposes tokens.

## Troubleshooting

- **"The caller does not have permission"** — expected from the hosted MCP (it's gated).
  It is not an error you need to fix; the REST fallback serves the request. Confirm your
  token works at all with `wactorz-google-login`.
- **"Access blocked: … has not completed the Google verification process"** — you're not a
  test user, or the app is Production+unverified with restricted scopes. Add yourself as a
  **Test user** on the consent screen and retry.
- **Free-text Gmail search fails ("metadata scope does not support 'q'")** — your token was
  minted with `gmail.metadata`. Re-run `wactorz-google-login gmail`; Wactorz requests a
  scope set without it. (`unread` / `inbox` / label filters work regardless.)
- **`bind on address ('127.0.0.1', 8765)`** — a previous login is still holding the callback
  port. Close it (or kill the stray process) and retry.
- **Wrong timezone in Calendar** — set `CALENDAR_MCP_TIMEZONE` (IANA name, e.g.
  `Europe/Athens`); otherwise it uses `TZ` / the system local zone.

## Security notes

- Tokens live in `~/.wactorz/*_token.json` (user-only permissions). Secrets come only from
  environment variables and are never sent to the dashboard or logged.
- Calendar events and email content can contain untrusted text — treat them as data, and
  review any create/delete/draft actions.
- Gmail is draft-first by design: the agent cannot send mail.
